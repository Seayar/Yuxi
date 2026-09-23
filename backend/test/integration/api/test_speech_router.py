"""语音识别路由 integration 测试：真实 HTTP 上传 + 进程内桩上游。

上游桩在测试进程内以真实 HTTP 服务运行，通过管理员配置接口把语音识别服务指向它，
因此断言的是本项目的线上协议（multipart 转发、SSE 事件投影、认证），而不是桩的行为。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator
from copy import deepcopy

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.config.options import get_option
from yuxi.services.speech_service import MAX_AUDIO_BYTES
from yuxi.storage.postgres.models_business import ConfigOption

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

OPTION_KEY = "speech_recognition_opts"
TRANSCRIPTION_URL = "/api/speech/transcriptions"
AUDIO_BYTES = b"RIFF-stub-audio-bytes"
# 不用 127.0.0.1：容器内它会经 get_docker_safe_url 改写成 host.docker.internal，
# 指向宿主机而不是本进程内的桩上游。127.0.0.2 仍是回环地址，可被同网络命名空间的 API 访问。
STUB_HOST = "127.0.0.2"
DEAD_SERVICE_URL = f"http://{STUB_HOST}:1"


def _chunk_line(content: str) -> str:
    """构造 vLLM 风格的一段增量 SSE 数据行。"""

    chunk = {"id": "trsc-1", "object": "transcription.chunk", "choices": [{"delta": {"content": content}}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _finish_line() -> str:
    """构造 vLLM 结束流时带 finish_reason 的数据行与 `[DONE]` 标记。"""

    return 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


def _build_stub_app(
    *,
    status_code: int = 200,
    chunks: tuple[str, ...] = ("变压", "器"),
    received: dict,
    gate: asyncio.Event | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/audio/transcriptions")
    async def transcribe(request: Request):
        form = await request.form()
        upload = form.get("file")
        received.update(
            {
                "model": form.get("model"),
                "stream": form.get("stream"),
                "filename": getattr(upload, "filename", None),
                "size": len(await upload.read()) if upload is not None else 0,
            }
        )
        if status_code != 200:
            return JSONResponse({"error": {"message": "stub upstream failure"}}, status_code=status_code)

        async def stream() -> AsyncIterator[str]:
            for content in chunks:
                yield _chunk_line(content)
            if gate is not None:
                await gate.wait()
            yield _finish_line()

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


@contextlib.asynccontextmanager
async def _serve(app: FastAPI) -> AsyncIterator[str]:
    """在测试进程内提供一个真实 HTTP 上游，返回其基地址。"""

    with socket.socket() as listener:
        listener.bind((STUB_HOST, 0))
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(0.01)
            yield f"http://{STUB_HOST}:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)


@pytest_asyncio.fixture
async def speech_service_option() -> AsyncIterator[None]:
    """快照并在用例结束后恢复语音识别配置，包括敏感字段的原始值。

    配置含 sensitive 字段，`Option.cacheable` 为假，读取路径每次回源 PostgreSQL，因此恢复
    数据库值即可生效，不需要额外失效缓存。
    """

    engine = create_async_engine(os.environ["POSTGRES_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            record = await get_option(db, OPTION_KEY)
            assert record is not None, "语音识别配置项未随启动同步到 config_options"
            previous = (
                deepcopy(record.value),
                record.updated_by,
                record.updated_at,
            )
        try:
            yield
        finally:
            async with session_factory() as db:
                await db.execute(
                    update(ConfigOption)
                    .where(ConfigOption.key == OPTION_KEY)
                    .values(value=previous[0], updated_by=previous[1], updated_at=previous[2])
                )
                await db.commit()
    finally:
        await engine.dispose()


async def _point_service_at(client: httpx.AsyncClient, admin_headers: dict[str, str], server_url: str) -> None:
    response = await client.put(
        f"/api/system/config/options/{OPTION_KEY}",
        json={"value": {"server_url": server_url, "model": "stub-asr", "api_key": ""}},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text


def _audio_files() -> dict:
    """构造上传用的 multipart 文件字段。"""

    return {"file": ("record.webm", AUDIO_BYTES, "audio/webm")}


async def _upload_audio(client: httpx.AsyncClient, headers: dict[str, str]) -> list[tuple[str, dict]]:
    """上传音频并读完整条 SSE 流，返回解析后的事件序列。"""

    async with client.stream("POST", TRANSCRIPTION_URL, files=_audio_files(), headers=headers) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])
    return _parse_events(body)


def _parse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = block.strip().splitlines()
        events.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return events


async def test_transcription_streams_deltas_then_completed(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    speech_service_option: AsyncIterator[None],
):
    received: dict = {}
    async with _serve(_build_stub_app(received=received)) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        events = await _upload_audio(test_client, admin_headers)

    assert [event for event, _ in events] == ["delta", "delta", "completed"]
    assert [payload["text"] for event, payload in events if event == "delta"] == ["变压", "器"]
    assert events[-1] == ("completed", {"text": "变压器"})
    # 上传的音频与识别参数必须原样到达上游，否则识别结果与请求无关。
    assert received == {"model": "stub-asr", "stream": "true", "filename": "record.webm", "size": len(AUDIO_BYTES)}


async def test_transcription_delivers_delta_while_stream_is_open(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    speech_service_option: AsyncIterator[None],
):
    """增量必须在识别结束前就能读到，否则 SSE 只是被缓冲后的整段结果。"""

    release = asyncio.Event()
    async with _serve(_build_stub_app(chunks=("变压",), received={}, gate=release)) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        try:
            async with test_client.stream(
                "POST", TRANSCRIPTION_URL, files=_audio_files(), headers=admin_headers
            ) as response:
                assert response.status_code == 200
                body = ""
                saw_first_event = False
                async for chunk in response.aiter_text():
                    body += chunk
                    if not saw_first_event and "\n\n" in body:
                        # 上游仍被 gate 拦住：首个事件已经在识别结束前到达。
                        assert not release.is_set()
                        assert body.startswith("event: delta")
                        assert "completed" not in body
                        saw_first_event = True
                        release.set()
                assert saw_first_event
        finally:
            release.set()

    events = _parse_events(body)
    assert [event for event, _ in events] == ["delta", "completed"]
    assert events[-1][1]["text"] == "变压"


async def test_transcription_rejects_oversized_upload_without_calling_upstream(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    speech_service_option: AsyncIterator[None],
):
    """超过上限的上传以 error 事件结束，不会截断后送去识别。"""

    received: dict = {}
    async with _serve(_build_stub_app(received=received)) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        response = await test_client.post(
            TRANSCRIPTION_URL,
            files={"file": ("big.wav", b"\0" * (MAX_AUDIO_BYTES + 1), "audio/wav")},
            headers=admin_headers,
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_events(response.text)
    assert [event for event, _ in events] == ["error"]
    assert "20 MB" in events[0][1]["message"]
    assert received == {}


async def test_transcription_reports_upstream_error_status(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    speech_service_option: AsyncIterator[None],
):
    async with _serve(_build_stub_app(status_code=500, received={})) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        events = await _upload_audio(test_client, admin_headers)

    assert [event for event, _ in events] == ["error"]
    assert "500" in events[0][1]["message"]


async def test_transcription_reports_unreachable_service(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    speech_service_option: AsyncIterator[None],
):
    await _point_service_at(test_client, admin_headers, DEAD_SERVICE_URL)

    events = await _upload_audio(test_client, admin_headers)

    assert [event for event, _ in events] == ["error"]
    assert "无法连接" in events[0][1]["message"]


async def test_transcription_requires_authentication(test_client: httpx.AsyncClient):
    response = await test_client.post(
        TRANSCRIPTION_URL,
        files={"file": ("record.webm", AUDIO_BYTES, "audio/webm")},
    )

    assert response.status_code == 401
