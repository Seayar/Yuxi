"""故障现象简述与推理依据的模型生成证据门单元测试。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace.contracts import CauseEvaluation, TriggerResult
from yuxi.knowledge.fault_trace.report_text import (
    compose_location_reasoning,
    compose_summary_text,
)

pytestmark = pytest.mark.unit


class _Model:
    def __init__(self, text: str | Exception) -> None:
        self.text = text
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


def _cause() -> CauseEvaluation:
    return CauseEvaluation(
        cause_id="CA-04",
        name="通道内树木未及时砍伐或修剪，遇大风倒伏压线",
        status="established",
        true_count=1,
        triggers=[
            TriggerResult(
                "TC-R03",
                "泄漏电流超1mA",
                "leakage_current > 1",
                "true",
                "ok",
                {"leakage_current": 1.2},
            )
        ],
    )


async def _summary(model, *, phenomena=("树木倒伏",), image=None):
    return await compose_summary_text(
        query_text="3B-7Y 通道内树木倒伏压覆导线",
        device_name="3B-7Y",
        device_type="架空线路",
        phenomena=list(phenomena),
        image_fault_name=image,
        model_call=model,
    )


@pytest.mark.asyncio
async def test_summary_accepts_grounded_text():
    model = _Model("3B-7Y 通道内树木倒伏压覆导线，线路存在明显放电痕迹。")

    assert await _summary(model) == "3B-7Y 通道内树木倒伏压覆导线，线路存在明显放电痕迹。"
    assert "3B-7Y" in model.prompts[0]


@pytest.mark.asyncio
async def test_summary_rejects_missing_device_or_phenomenon():
    assert await _summary(_Model("通道内树木倒伏压覆导线，线路存在放电痕迹。")) is None
    assert await _summary(_Model("3B-7Y 设备出现异常情况。")) is None


@pytest.mark.asyncio
async def test_summary_rejects_advice_and_model_failure():
    assert await _summary(_Model("3B-7Y 通道内树木倒伏压覆导线，建议立即检修。")) is None
    assert await _summary(_Model(RuntimeError("timeout"))) is None


@pytest.mark.asyncio
async def test_summary_accepts_image_phenomenon_without_text_phenomenon():
    model = _Model("3B-7Y 图片识别到异物搭挂。")

    assert await _summary(model, phenomena=(), image="异物搭挂") == "3B-7Y 图片识别到异物搭挂。"


@pytest.mark.asyncio
async def test_reasoning_accepts_grounded_text_and_includes_evidence():
    model = _Model("通道内树木未及时砍伐或修剪，遇大风倒伏压线；泄漏电流实测1.2mA超过1mA判据，定位该原因。")
    causes = [_cause()]

    text = await compose_location_reasoning(
        query_text="3B-7Y 通道内树木倒伏",
        device_name="3B-7Y",
        located_causes=[causes[0]],
        causes=causes,
        model_call=model,
    )

    assert text is not None
    assert "leakage_current > 1" in model.prompts[0]
    assert "leakage_current=1.2" in model.prompts[0]


@pytest.mark.asyncio
async def test_reasoning_rejects_missing_located_cause_or_advice():
    causes = [_cause()]
    assert (
        await compose_location_reasoning(
            query_text="3B-7Y 通道内树木倒伏",
            device_name="3B-7Y",
            located_causes=[causes[0]],
            causes=causes,
            model_call=_Model("泄漏电流超过判据，线路存在放电痕迹。"),
        )
        is None
    )
    assert (
        await compose_location_reasoning(
            query_text="3B-7Y 通道内树木倒伏",
            device_name="3B-7Y",
            located_causes=[causes[0]],
            causes=causes,
            model_call=_Model("通道内树木未及时砍伐或修剪，遇大风倒伏压线，建议更换导线。"),
        )
        is None
    )


@pytest.mark.asyncio
async def test_reasoning_requires_supporting_phenomenon():
    causes = [_cause()]

    assert (
        await compose_location_reasoning(
            query_text="3B-7Y 通道内树木倒伏",
            device_name="3B-7Y",
            located_causes=[causes[0]],
            causes=causes,
            phenomena=["接头放电痕迹"],
            model_call=_Model("通道内树木未及时砍伐或修剪，遇大风倒伏压线；泄漏电流超过判据。"),
        )
        is None
    )

    text = "通道内树木未及时砍伐或修剪，遇大风倒伏压线；接头放电痕迹明显，泄漏电流超过判据。"
    assert (
        await compose_location_reasoning(
            query_text="3B-7Y 通道内树木倒伏",
            device_name="3B-7Y",
            located_causes=[causes[0]],
            causes=causes,
            phenomena=["接头放电痕迹"],
            model_call=_Model(text),
        )
        == text
    )


@pytest.mark.asyncio
async def test_reasoning_rejected_when_nothing_located():
    """未定位任何原因时没有可核对判据，模型文本不得替换确定性推理依据。"""

    model = _Model("泄漏电流超过判据，判断为通道内树木倒伏压线。")

    assert (
        await compose_location_reasoning(
            query_text="3B-7Y 通道内树木倒伏",
            device_name="3B-7Y",
            located_causes=[],
            causes=[_cause()],
            model_call=model,
        )
        is None
    )
    assert model.prompts == []
