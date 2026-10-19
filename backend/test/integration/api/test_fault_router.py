"""故障图片识别路由 integration 测试：真实 HTTP 上传 + 进程内桩上游。

上游桩在测试进程内以真实 HTTP 服务运行，通过管理员配置接口把故障识别服务指向它，
因此断言的是本项目的线上协议（multipart 转发、字段投影、认证），而不是桩的行为。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator
from copy import deepcopy

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.config.options import get_option
from yuxi.storage.postgres.models_business import ConfigOption

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

OPTION_KEY = "fault_image_opts"
DETECT_URL = "/api/fault/images"
HEALTH_URL = "/api/fault/health"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\nfake-image-payload"
# 不用 127.0.0.1：容器内它会经 get_docker_safe_url 改写成 host.docker.internal，
# 指向宿主机而不是本进程内的桩上游。127.0.0.2 仍是回环地址，可被同网络命名空间的 API 访问。
STUB_HOST = "127.0.0.2"
DEAD_SERVICE_URL = f"http://{STUB_HOST}:1"


def _build_stub_app(*, detect_status: int = 200) -> FastAPI:
    """构造与 YOLO 接口文档一致的桩上游：按图片内容是否含 FAULT 决定识别结果。"""

    app = FastAPI()

    @app.post("/api/fault/detect")
    async def detect(request: Request):
        form = await request.form()
        upload = form.get("file")
        data = await upload.read() if upload is not None else b""
        if detect_status != 200:
            return JSONResponse({"detail": "stub upstream failure"}, status_code=detect_status)
        if b"FAULT" in data:
            return JSONResponse(
                {
                    "code": 200,
                    "message": "success",
                    "fault": True,
                    "fault_type": "insulator_damage",
                    "fault_name": "绝缘子破损",
                }
            )
        return JSONResponse({"code": 200, "message": "success", "fault": False, "fault_type": None, "fault_name": None})

    @app.get("/api/fault/health")
    async def health():
        if detect_status != 200:
            return JSONResponse({"detail": "stub upstream failure"}, status_code=detect_status)
        return JSONResponse(
            {
                "code": 200,
                "message": "success",
                "model": "YOLO",
                "model_path": "/app/models/best.pt",
                "status": "running",
            }
        )

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
async def fault_image_option() -> AsyncIterator[None]:
    """快照并在用例结束后恢复故障识别配置的数据库原始值。"""

    engine = create_async_engine(os.environ["POSTGRES_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            record = await get_option(db, OPTION_KEY)
            assert record is not None, "故障识别配置项未随启动同步到 config_options"
            previous = (deepcopy(record.value), record.updated_by, record.updated_at)
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
        json={"value": {"server_url": server_url}},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text


async def test_detect_with_fault(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    fault_image_option: AsyncIterator[None],
):
    async with _serve(_build_stub_app()) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        response = await test_client.post(
            DETECT_URL,
            files={"file": ("fault.png", IMAGE_BYTES + b"FAULT", "image/png")},
            headers=admin_headers,
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["fault"] is True
    assert payload["fault_type"] == "insulator_damage"
    assert payload["fault_name"] == "绝缘子破损"


async def test_detect_without_fault(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    fault_image_option: AsyncIterator[None],
):
    async with _serve(_build_stub_app()) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        response = await test_client.post(
            DETECT_URL,
            files={"file": ("clean.png", IMAGE_BYTES, "image/png")},
            headers=admin_headers,
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["fault"] is False
    assert payload["fault_type"] is None
    assert payload["fault_name"] is None


async def test_detect_rejects_non_image(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    fault_image_option: AsyncIterator[None],
):
    async with _serve(_build_stub_app()) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        response = await test_client.post(
            DETECT_URL,
            files={"file": ("note.txt", b"not an image", "text/plain")},
            headers=admin_headers,
        )
        # 本地校验在调用上游前完成：不管上游状态如何都返回 400。
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "只能上传图片"


async def test_detect_missing_file_is_422(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    fault_image_option: AsyncIterator[None],
):
    response = await test_client.post(DETECT_URL, headers=admin_headers)

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert any(item.get("loc") == ["body", "file"] for item in detail)


async def test_detect_maps_upstream_failure_to_502(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    fault_image_option: AsyncIterator[None],
):
    async with _serve(_build_stub_app(detect_status=500)) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        response = await test_client.post(
            DETECT_URL,
            files={"file": ("fault.png", IMAGE_BYTES, "image/png")},
            headers=admin_headers,
        )

    assert response.status_code == 502, response.text
    assert "故障识别服务" in response.json()["detail"]


async def test_detect_maps_unreachable_upstream_to_502(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    fault_image_option: AsyncIterator[None],
):
    # 指向必然连接失败的端口：代理层投出可展示的错误而不是“未识别到故障”。
    await _point_service_at(test_client, admin_headers, DEAD_SERVICE_URL)

    response = await test_client.post(
        DETECT_URL,
        files={"file": ("fault.png", IMAGE_BYTES, "image/png")},
        headers=admin_headers,
    )

    assert response.status_code == 502, response.text
    assert "无法连接故障识别服务" in response.json()["detail"]


async def test_health_passthrough(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    fault_image_option: AsyncIterator[None],
):
    async with _serve(_build_stub_app()) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        response = await test_client.get(HEALTH_URL, headers=admin_headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model"] == "YOLO"
    assert payload["status"] == "running"


async def test_health_maps_upstream_failure_to_502(
    test_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    fault_image_option: AsyncIterator[None],
):
    async with _serve(_build_stub_app(detect_status=500)) as stub_url:
        await _point_service_at(test_client, admin_headers, stub_url)

        response = await test_client.get(HEALTH_URL, headers=admin_headers)

    assert response.status_code == 502, response.text
    assert "故障识别服务" in response.json()["detail"]
