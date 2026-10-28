"""智能体图片输入方式中间件单元测试。"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from langchain.messages import HumanMessage
from yuxi.agents.middlewares.image_recognition import AgentImageInputMiddleware, strip_image_blocks
from yuxi.services.input_message_service import build_chat_input_message

pytestmark = pytest.mark.unit


def _multimodal_message() -> HumanMessage:
    image = base64.b64encode(b"jpeg-bytes").decode()
    return build_chat_input_message("主变冒烟", image).require_langchain_message()


class _Request:
    def __init__(self, messages, mode: str) -> None:
        self.messages = messages
        self.runtime = SimpleNamespace(context=SimpleNamespace(image_input_mode=mode))

    def override(self, *, messages):
        self.messages = messages
        return self


def _has_image(message) -> bool:
    return any(block.get("type") in {"image", "image_url", "input_image"} for block in message.content_blocks)


def test_strip_image_blocks_keeps_text():
    stripped = strip_image_blocks(_multimodal_message())

    assert stripped.content == "主变冒烟"
    assert not _has_image(stripped)


def test_strip_image_blocks_keeps_plain_message():
    message = HumanMessage(content="纯文本")

    assert strip_image_blocks(message) is message


@pytest.mark.asyncio
async def test_middleware_strips_images_only_for_server_mode():
    server_request = _Request([_multimodal_message()], "server")
    seen: list = []

    async def server_handler(request):
        seen.append(request.messages)
        return "server"

    assert await AgentImageInputMiddleware().awrap_model_call(server_request, server_handler) == "server"
    assert not _has_image(seen[0][0])

    model_request = _Request([_multimodal_message()], "model")

    async def model_handler(request):
        seen.append(request.messages)
        return "model"

    assert await AgentImageInputMiddleware().awrap_model_call(model_request, model_handler) == "model"
    assert _has_image(seen[1][0])
