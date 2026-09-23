"""语音识别服务单元测试：SSE 事件投影、上游失败投影与配置解析。"""

from __future__ import annotations

import io
import json

import httpx
import pytest
from starlette.datastructures import UploadFile

from server.routers.speech_router import create_transcription
from yuxi.services import speech_service

pytestmark = pytest.mark.unit


class _FakeOption:
    """替身配置项，让配置解析路径不依赖数据库。"""

    def __init__(self, value: dict) -> None:
        self._value = value

    async def get(self) -> dict:
        return self._value


def _use_config(monkeypatch: pytest.MonkeyPatch, **fields: str) -> None:
    monkeypatch.setattr(speech_service, "speech_recognition_opts", _FakeOption(dict(fields)))


def _stream_body(*contents: str) -> bytes:
    """构造 vLLM 风格的流式响应体，含结束标记。"""

    lines = []
    for content in contents:
        chunk = {"id": "trsc-1", "object": "transcription.chunk", "choices": [{"delta": {"content": content}}]}
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    lines.append('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _register_upstream(httpx_mock, body: bytes, *, status_code: int = 200, content_type: str | None = None) -> None:
    """注册一次上游响应；默认按 vLLM 的流式响应头返回。"""

    headers = {"content-type": content_type or speech_service.STREAM_CONTENT_TYPE}
    httpx_mock.add_response(
        url="http://asr.local/v1/audio/transcriptions",
        content=body,
        status_code=status_code,
        headers=headers,
    )


async def _collect(**kwargs) -> list[str]:
    return [event async for event in speech_service.stream_transcription(**kwargs)]


def _events(stream: list[str]) -> list[tuple[str, dict]]:
    parsed: list[tuple[str, dict]] = []
    for block in stream:
        lines = block.strip().splitlines()
        event = lines[0].removeprefix("event: ")
        parsed.append((event, json.loads(lines[1].removeprefix("data: "))))
    return parsed


def test_parse_stream_line_extracts_delta_content():
    chunk = {"choices": [{"delta": {"content": "变压器"}}]}

    assert speech_service._parse_stream_line(f"data: {json.dumps(chunk, ensure_ascii=False)}") == ("变压器", False)


@pytest.mark.parametrize(
    "line",
    [
        "",
        ": keep-alive",
        "event: message",
        "data:",
        'data: {"choices":[],"usage":{"total_tokens":3}}',
    ],
)
def test_parse_stream_line_ignores_non_delta_lines(line: str):
    assert speech_service._parse_stream_line(line) == (None, False)


@pytest.mark.parametrize(
    "line",
    ['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', "data: [DONE]"],
)
def test_parse_stream_line_marks_end_of_stream(line: str):
    assert speech_service._parse_stream_line(line) == (None, True)


def test_parse_stream_line_rejects_truncated_finish_reason():
    """上游以 length 之类原因结束时结果可能被截断，不能当成正常结束。"""

    with pytest.raises(speech_service.TranscriptionFailed):
        speech_service._parse_stream_line('data: {"choices":[{"delta":{},"finish_reason":"length"}]}')


def test_parse_stream_line_rejects_unparsable_payload():
    with pytest.raises(speech_service.TranscriptionFailed):
        speech_service._parse_stream_line("data: {not json")


def test_parse_stream_line_rejects_stream_error():
    """上游把错误写进流时必须失败，且不把上游原文回给客户端。"""

    with pytest.raises(speech_service.TranscriptionFailed) as excinfo:
        speech_service._parse_stream_line('data: {"error":{"message":"audio decode failed"}}')

    assert "audio decode failed" not in str(excinfo.value)


async def test_stream_transcription_rejects_empty_audio():
    events = _events(await _collect(audio=b"", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["error"]
    assert "音频内容为空" in events[0][1]["message"]


async def test_stream_transcription_rejects_oversized_audio():
    oversized = b"\0" * (speech_service.MAX_AUDIO_BYTES + 1)

    events = _events(await _collect(audio=oversized, filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["error"]
    assert "20 MB" in events[0][1]["message"]


async def test_stream_transcription_reports_missing_config(monkeypatch: pytest.MonkeyPatch):
    _use_config(monkeypatch, server_url="", model="")

    events = _events(await _collect(audio=b"audio", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["error"]
    assert "未配置" in events[0][1]["message"]


async def test_stream_transcription_streams_deltas_then_completed(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch, server_url="http://asr.local", model="Qwen3-ASR-1.7B", api_key="")
    _register_upstream(httpx_mock, _stream_body("变压", "器油温"))

    events = _events(await _collect(audio=b"audio-bytes", filename="record.webm", content_type="audio/webm"))

    assert [event for event, _ in events] == ["delta", "delta", "completed"]
    assert [payload["text"] for event, payload in events if event == "delta"] == ["变压", "器油温"]
    assert events[-1][1]["text"] == "变压器油温"

    request = httpx_mock.get_request()
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'name="model"' in request.content and b"Qwen3-ASR-1.7B" in request.content
    assert b'name="stream"' in request.content and b"true" in request.content
    assert "authorization" not in request.headers


async def test_stream_transcription_sends_api_key_when_configured(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="secret-key")
    _register_upstream(httpx_mock, _stream_body("ok"))

    await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav")

    assert httpx_mock.get_request().headers["authorization"] == "Bearer secret-key"


async def test_stream_transcription_rewrites_localhost_inside_docker(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """容器内配置 localhost 时必须改写成宿主机地址，否则会打到 API 容器自身。"""

    monkeypatch.setenv("RUNNING_IN_DOCKER", "true")
    _use_config(monkeypatch, server_url="http://localhost:8004/", model="stub-asr", api_key="")
    httpx_mock.add_response(
        url="http://host.docker.internal:8004/v1/audio/transcriptions",
        content=_stream_body("ok"),
        headers={"content-type": speech_service.STREAM_CONTENT_TYPE},
    )

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["delta", "completed"]


async def test_stream_transcription_reports_upstream_status(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    httpx_mock.add_response(
        url="http://asr.local/v1/audio/transcriptions",
        status_code=500,
        json={"error": {"message": "EngineCore encountered an issue at /opt/vllm/engine.py"}},
    )

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["error"]
    assert "500" in events[0][1]["message"]
    # 上游内部细节只进日志，不能回给任何登录用户。
    assert "EngineCore" not in events[0][1]["message"]


async def test_stream_transcription_reports_non_stream_response(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """上游忽略 stream=true 返回普通 JSON 时，必须报错而不是报一个空识别结果。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, b'{"text":"transcribed"}', content_type="application/json")

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["error"]
    assert "未返回流式响应" in events[0][1]["message"]


async def test_stream_transcription_reports_stream_without_end_marker(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """流在结束标记之前结束说明结果可能被截断，不能报 completed。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, 'data: {"choices":[{"delta":{"content":"变压"}}]}\n\n'.encode())

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["delta", "error"]
    assert "结束标记" in events[1][1]["message"]


async def test_stream_transcription_reports_unparsable_stream(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, b"data: {oops\n\n")

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["error"]
    assert "无法解析" in events[0][1]["message"]


async def test_stream_transcription_reports_unreachable_service(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["error"]
    assert "无法连接" in events[0][1]["message"]


async def test_stream_transcription_reports_failure_after_deltas(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """中途失败时已发出的增量仍然有效，但流以 error 结束而不是 completed。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, 'data: {"choices":[{"delta":{"content":"变压"}}]}\n\ndata: {oops\n\n'.encode())

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["delta", "error"]
    assert events[0][1]["text"] == "变压"


async def test_stream_transcription_strips_language_prefix(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """Qwen3-ASR 流式输出带 `language <语言><asr_text>` 前缀，增量只能含识别文字。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    # 前缀跨多个 chunk，必须缓冲到标记出现后再转发（真实上游的分块方式）。
    _register_upstream(httpx_mock, _stream_body("language", " English", "<asr_text>", "变压", "器"))

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["delta", "delta", "completed"]
    assert [payload["text"] for event, payload in events if event == "delta"] == ["变压", "器"]
    assert events[-1][1]["text"] == "变压器"


async def test_stream_transcription_keeps_text_when_prefix_is_absent(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """上游没有语言前缀时按原样转发，不能吞掉内容。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, _stream_body("hello", " world"))

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [payload["text"] for event, payload in events if event == "delta"] == ["hello", " world"]
    assert events[-1] == ("completed", {"text": "hello world"})


async def test_stream_transcription_strips_prefix_only_when_it_is_one(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """只有前缀时才丢弃：正文里出现同名标记不能截断识别结果。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, _stream_body("language Chinese<asr_text>按<asr_text>标记", "分断"))

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert events[-1] == ("completed", {"text": "按<asr_text>标记分断"})


async def test_stream_transcription_keeps_text_starting_with_language(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """正文以 language 开头且没有标记时，必须把缓冲的内容完整交出，不能吞掉。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, _stream_body("language model", " 的定义"))

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["delta", "completed"]
    assert events[-1] == ("completed", {"text": "language model 的定义"})


async def test_stream_transcription_strips_long_language_prefix(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """语言名较长时前缀会超出初始缓冲窗口，仍要等到标记再剥离。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, _stream_body("language Chinese (Simplified)", "<asr_text>", "变压器"))

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["delta", "completed"]
    assert events[-1] == ("completed", {"text": "变压器"})


async def test_stream_transcription_forwards_text_past_buffer_limit(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """缓冲超过上限仍未出现标记时按无前缀处理，转发原文并恢复增量。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(httpx_mock, _stream_body("language " + "X" * 70, " 后文"))

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["delta", "delta", "completed"]
    assert events[-1] == ("completed", {"text": "language " + "X" * 70 + " 后文"})


async def test_stream_transcription_reports_truncated_stream(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    """上游以 length 截断时不能报 completed。"""

    _use_config(monkeypatch, server_url="http://asr.local", model="stub-asr", api_key="")
    _register_upstream(
        httpx_mock,
        'data: {"choices":[{"delta":{"content":"变压"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n'.encode(),
    )

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["delta", "error"]
    assert "截断" in events[1][1]["message"]


async def test_stream_transcription_reports_config_read_failure(monkeypatch: pytest.MonkeyPatch):
    """配置读取本身失败也要投影成 error 事件，不能在响应头已发出后截断连接。"""

    class _BrokenOption:
        async def get(self) -> dict:
            raise RuntimeError("postgres unavailable")

    monkeypatch.setattr(speech_service, "speech_recognition_opts", _BrokenOption())

    events = _events(await _collect(audio=b"audio-bytes", filename="a.wav", content_type="audio/wav"))

    assert [event for event, _ in events] == ["error"]
    assert "调用失败" in events[0][1]["message"]


async def test_router_rejects_oversized_upload_without_calling_upstream():
    """路由按上限多读一个字节：超限音频被拒绝，不会截断后送去识别。"""

    upload = UploadFile(file=io.BytesIO(b"\0" * (speech_service.MAX_AUDIO_BYTES + 1)), filename="big.wav")

    response = await create_transcription(file=upload, current_user=None)
    events = _events([chunk async for chunk in response.body_iterator])

    assert [event for event, _ in events] == ["error"]
    assert "20 MB" in events[0][1]["message"]


def test_option_env_fallback(monkeypatch: pytest.MonkeyPatch):
    """数据库没有值时按环境变量解析服务地址、模型与密钥。"""

    monkeypatch.setenv("SPEECH_ASR_BASE_URL", "http://localhost:8004")
    monkeypatch.setenv("SPEECH_ASR_MODEL", "Qwen3-ASR-1.7B")
    monkeypatch.setenv("SPEECH_ASR_API_KEY", "env-key")

    resolved = speech_service.speech_recognition_opts.resolve({})

    assert resolved == {
        "server_url": "http://localhost:8004",
        "model": "Qwen3-ASR-1.7B",
        "api_key": "env-key",
    }
