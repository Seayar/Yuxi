"""按智能体配置决定上传图片是否进入模型输入。

平台只提供机制：``context.image_input_mode == "server"`` 时，模型请求中的图片块被
替换为占位文本，原始图片仍保留在会话状态里，由该智能体的工具自行读取和处理。
这样 ``chat_service`` 不需要知道任何具体智能体，图片用途完全由智能体配置与工具决定。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse

_IMAGE_BLOCK_TYPES = {"image", "image_url", "input_image"}
_IMAGE_PLACEHOLDER = "（用户上传了设备图片，已由智能体自行处理。）"
SERVER_IMAGE_INPUT_MODE = "server"


class AgentImageInputMiddleware(AgentMiddleware[Any, Any, Any]):
    """图片输入方式为 ``server`` 时不把图片交给模型。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if not _server_side_image_input(request):
            return handler(request)
        return handler(request.override(messages=[strip_image_blocks(item) for item in request.messages]))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not _server_side_image_input(request):
            return await handler(request)
        return await handler(request.override(messages=[strip_image_blocks(item) for item in request.messages]))


def strip_image_blocks(message: Any) -> Any:
    """把图片块替换成占位文本；没有图片块时原样返回。"""

    blocks = getattr(message, "content_blocks", None)
    if not blocks:
        return message
    if not any(isinstance(block, dict) and block.get("type") in _IMAGE_BLOCK_TYPES for block in blocks):
        return message
    text = " ".join(
        str(block.get("text") or "").strip()
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    return message.model_copy(update={"content": text or _IMAGE_PLACEHOLDER})


def _server_side_image_input(request: ModelRequest) -> bool:
    context = getattr(getattr(request, "runtime", None), "context", None)
    if isinstance(context, dict):
        mode = context.get("image_input_mode")
    else:
        mode = getattr(context, "image_input_mode", None)
    return str(mode or "").strip().lower() == SERVER_IMAGE_INPUT_MODE
