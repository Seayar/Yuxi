"""语音识别用例：把上传的音频交给本地 vLLM ASR 服务并以 SSE 增量返回。

上游是 vLLM 的 OpenAI 兼容 `/v1/audio/transcriptions`：`stream=true` 时它返回 SSE，
增量文字位于 `choices[0].delta.content`，以 `finish_reason="stop"` 的 chunk 和
`data: [DONE]` 结束。本模块只做协议转译与失败投影，增量转成 `delta` 事件，正常结束
转成 `completed`，任何失败转成 `error`，不把失败伪装成空结果或截断结果。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from yuxi.config.options import speech_recognition_opts
from yuxi.utils import get_docker_safe_url, logger
from yuxi.utils.sse_utils import format_sse

TRANSCRIPTION_PATH = "/v1/audio/transcriptions"
STREAM_CONTENT_TYPE = "text/event-stream"
# 直连 API 时的音频上限；反向代理另有 client_max_body_size，两者按各自边界独立生效。
MAX_AUDIO_BYTES = 20 * 1024 * 1024
# 识别耗时体现在 read；连接与写入保持短超时，避免上游僵死连接长期占用请求。
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
SSE_EVENT_DELTA = "delta"
SSE_EVENT_COMPLETED = "completed"
SSE_EVENT_ERROR = "error"
_ERROR_DETAIL_LIMIT = 300
# Qwen3-ASR 的原始输出以 `language <语言><asr_text>` 开头：非流式响应会剥掉这段前缀，
# 流式响应不会，因此只在开头可能是这段前缀时缓冲，其余情况立即转发以保留增量。
_ASR_PREFIX = "language"
_ASR_TEXT_TAG = "<asr_text>"
# 前缀长度上限：超出就按无前缀处理并转发原文，避免正文以 language 开头时一直缓冲。
_PREFIX_BUFFER_LIMIT = 64


class TranscriptionFailed(Exception):
    """语音识别失败，携带可直接展示给用户的说明。"""


@dataclass(frozen=True)
class SpeechConfig:
    """当前生效的语音识别服务配置。"""

    base_url: str
    model: str
    api_key: str


async def stream_transcription(
    *,
    audio: bytes,
    filename: str | None,
    content_type: str | None,
) -> AsyncIterator[str]:
    """产出语音识别的 SSE 事件流：若干 `delta`，最后一个 `completed` 或 `error`。

    本函数只读配置并发起一次上游请求，不写业务状态、不重试，因此重放不会产生副作用。
    """

    if not audio:
        yield _error_event("音频内容为空，请重新录制")
        return
    if len(audio) > MAX_AUDIO_BYTES:
        yield _error_event(f"音频超过 {MAX_AUDIO_BYTES // (1024 * 1024)} MB 上限")
        return

    parts: list[str] = []
    try:
        config = await _resolve_config()
        if config is None:
            logger.warning("语音识别服务未配置：缺少服务地址或模型（SPEECH_ASR_BASE_URL / SPEECH_ASR_MODEL）")
            yield _error_event("语音识别服务未配置，请联系管理员")
            return

        async for piece in _iter_deltas(config=config, audio=audio, filename=filename, content_type=content_type):
            parts.append(piece)
            yield format_sse({"text": piece}, SSE_EVENT_DELTA)
    except TranscriptionFailed as exc:
        logger.warning(f"语音识别失败: {exc}")
        yield _error_event(str(exc))
        return
    except httpx.HTTPError as exc:
        logger.warning(f"语音识别服务不可达: {exc}")
        yield _error_event("无法连接语音识别服务，请检查服务地址与服务运行状态")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"语音识别调用异常: {exc}")
        yield _error_event("语音识别服务调用失败，请稍后重试")
        return

    yield format_sse({"text": "".join(parts)}, SSE_EVENT_COMPLETED)


async def _resolve_config() -> SpeechConfig | None:
    """读取管理员配置；服务地址或模型缺失时返回 None，由调用方投影为 `error`。"""

    resolved = await speech_recognition_opts.get()
    base_url = str(resolved.get("server_url") or "").strip()
    model = str(resolved.get("model") or "").strip()
    if not base_url or not model:
        return None
    return SpeechConfig(
        base_url=get_docker_safe_url(base_url.rstrip("/")),
        model=model,
        api_key=str(resolved.get("api_key") or "").strip(),
    )


async def _iter_deltas(
    *,
    config: SpeechConfig,
    audio: bytes,
    filename: str | None,
    content_type: str | None,
) -> AsyncIterator[str]:
    """调用上游转写接口，按上游顺序逐段产出增量文字。

    上游必须按 vLLM 的流式转写协议返回 SSE，并以 `finish_reason="stop"` 的 chunk 和
    `data: [DONE]` 结束。响应不是 SSE、流在结束标记之前结束、或以上游截断原因
    （如 `length`）结束时都按失败处理，避免把非流式响应或截断结果当成一次完整识别。
    """

    files = {"file": (filename or "audio", audio, content_type or "application/octet-stream")}
    data = {"model": config.model, "response_format": "json", "stream": "true"}
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}

    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{config.base_url}{TRANSCRIPTION_PATH}",
            data=data,
            files=files,
            headers=headers,
        ) as response:
            if response.status_code != 200:
                detail = (await response.aread()).decode("utf-8", "ignore")
                logger.warning(f"语音识别服务返回 {response.status_code}: {_shorten(detail)}")
                raise TranscriptionFailed(f"语音识别服务返回 {response.status_code}，请检查服务运行状态")

            response_type = response.headers.get("content-type", "")
            if not response_type.startswith(STREAM_CONTENT_TYPE):
                detail = (await response.aread()).decode("utf-8", "ignore")
                logger.warning(f"语音识别服务未返回流式响应（content-type={response_type}）: {_shorten(detail)}")
                raise TranscriptionFailed("语音识别服务未返回流式响应，请确认服务地址与模型支持流式转写")

            finished = False
            pending = ""
            prefix_resolved = False
            async for line in response.aiter_lines():
                piece, finished = _parse_stream_line(line)
                if piece:
                    if prefix_resolved:
                        yield piece
                    else:
                        pending += piece
                        if _ASR_TEXT_TAG in pending and pending.lstrip().startswith(_ASR_PREFIX):
                            prefix_resolved = True
                            text = pending.split(_ASR_TEXT_TAG, 1)[1]
                            pending = ""
                            if text:
                                yield text
                        elif not _could_be_language_prefix(pending) or len(pending) > _PREFIX_BUFFER_LIMIT:
                            prefix_resolved = True
                            yield pending
                            pending = ""
                if finished:
                    break
            if not finished:
                raise TranscriptionFailed("语音识别流在结束标记之前中断，请重试")
            if pending:
                yield pending


def _parse_stream_line(line: str) -> tuple[str | None, bool]:
    """解析一行上游 SSE，返回（增量文字, 是否已正常结束）。

    控制行、空增量与 usage 行返回 `(None, False)`；`[DONE]` 或 `finish_reason="stop"`
    的 chunk 返回 `(None, True)`。上游把错误写进流、写出不可解析内容、或以截断原因
    （如 `length`）结束时招错，避免静默截断后仍报 `completed`。
    """

    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None, False
    payload = stripped[len("data:") :].strip()
    if not payload:
        return None, False
    if payload == "[DONE]":
        return None, True

    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TranscriptionFailed("语音识别服务返回了无法解析的流式数据") from exc

    if not isinstance(chunk, dict):
        return None, False
    if chunk.get("error"):
        logger.warning(f"语音识别服务在流中返回错误: {_shorten(str(chunk['error']))}")
        raise TranscriptionFailed("语音识别服务识别失败，请检查音频内容或服务运行状态")

    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, False

    choice = choices[0]
    delta = choice.get("delta")
    content = delta.get("content") if isinstance(delta, dict) else None
    text = content if isinstance(content, str) and content else None

    finish_reason = choice.get("finish_reason")
    if finish_reason is None:
        return text, False
    if finish_reason != "stop":
        logger.warning(f"语音识别被上游以 {finish_reason} 结束，结果可能不完整")
        raise TranscriptionFailed("语音识别结果被上游截断，请重试")
    return text, True


def _could_be_language_prefix(text: str) -> bool:
    """判断已缓冲内容是否仍可能是 Qwen3-ASR 的 `language <语言><asr_text>` 前缀。"""

    stripped = text.lstrip()
    return _ASR_PREFIX.startswith(stripped) or stripped.startswith(_ASR_PREFIX)


def _error_event(message: str) -> str:
    """构造 `error` 事件，作为识别失败对客户端的唯一投影。"""

    return format_sse({"message": message}, SSE_EVENT_ERROR)


def _shorten(text: str) -> str:
    """压缩上游错误详情，只写进日志，不进入对客户端的 `error` 事件。"""

    collapsed = " ".join(text.split())
    if len(collapsed) <= _ERROR_DETAIL_LIMIT:
        return collapsed
    return f"{collapsed[:_ERROR_DETAIL_LIMIT]}…"
