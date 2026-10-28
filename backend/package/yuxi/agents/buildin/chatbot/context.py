from dataclasses import dataclass, field

from yuxi.agents.context import BaseContext


@dataclass(kw_only=True)
class ChatBotContext(BaseContext):
    subagents: list[str] | None = field(
        default=None,
        metadata={
            "name": "子智能体",
            "options": [],
            "description": "可选子智能体列表，为空表示启用当前用户可见的全部子智能体。",
            "type": "list",
            "kind": "subagents",
        },
    )

    image_input_mode: str = field(
        default="model",
        metadata={
            "name": "图片输入方式",
            "description": "model：图片随消息交给模型；server：由智能体工具自行处理上传图片，模型只接收文字。",
            "hide": True,
        },
    )
