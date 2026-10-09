import pytest

from yuxi.knowledge.power_spec.ingestion import (
    PowerSpecRebuildFile,
    PowerSpecRebuildService,
    is_power_spec_metadata_ready,
)


GOOD_TEXT = """DL/T 572—2021 电力变压器运行规程
适用范围：适用于电力变压器运行巡视。
1.1 总则
本条款用于说明电力变压器运行管理的基本要求和适用条件。
1.2 巡视要求
运行人员应按照规定周期完成设备巡视并记录异常情况。
"""

BAD_TEXT = """GB/T 6451—2023 电力变压器技术参数和要求
1.1
短文本。
1.2
短文本。
"""


class FakeStore:
    def __init__(self):
        self.saved = []

    async def save_power_spec_extraction(self, **payload):
        self.saved.append(payload)


@pytest.mark.asyncio
async def test_rebuild_dry_run_reports_quality_without_writing():
    store = FakeStore()
    service = PowerSpecRebuildService(store)

    report = await service.rebuild(
        [PowerSpecRebuildFile(kb_id="kb-1", file_id="file-1", source_file="one.pdf")],
        source=lambda _file: _async_text(GOOD_TEXT),
        dry_run=True,
    )

    assert report.accepted[0].clause_count == 2
    assert store.saved == []


@pytest.mark.asyncio
async def test_rebuild_rejects_low_quality_ocr():
    store = FakeStore()
    report = await PowerSpecRebuildService(store).rebuild(
        [PowerSpecRebuildFile(kb_id="kb-1", file_id="file-1")],
        source=lambda _file: _async_text(BAD_TEXT),
        dry_run=False,
    )

    assert report.rejected[0].reason == "未识别到数字条款"
    assert store.saved == []


async def _async_text(value: str) -> str:
    return value


def test_power_spec_metadata_ready_matches_rebuild_quality_gate():
    assert is_power_spec_metadata_ready(
        {
            "clauses": [
                {
                    "clause_no": "1.1",
                    "clause_title": "总则",
                    "clause_body": "这是用于验证质量门的足够长条款正文内容，包含完整的运行要求。",
                }
            ]
        }
    )
    assert not is_power_spec_metadata_ready(
        {"clauses": [{"clause_no": "1.1", "clause_title": "1.1", "clause_body": "短"}]}
    )


def test_untitled_clause_with_substantive_body_is_ready():
    """无标题条款用编号作标题，正文充分时写入与读取都必须放行。

    标准里本来就有不带标题的条款；读取侧若因其标题等于编号而拒绝，就会出现
    「重建成功但检索说未完成条款级重建」的落差。
    """

    metadata = {
        "clauses": [
            {
                "clause_no": "7.3.5.2",
                "clause_title": "7.3.5.2",
                "clause_body": "停电操作应按照“断路器一负荷侧隔离开关一电源侧隔离开关”的顺序依次进行。",
            }
        ]
    }

    assert is_power_spec_metadata_ready(metadata)


def test_power_spec_metadata_ready_rejects_heading_only_extraction():
    """负向：标题只剩编号且正文过短的伪条款仍要被读取侧拒绝。"""

    metadata = {
        "clauses": [
            {"clause_no": "1.1", "clause_title": "1.1", "clause_body": "短"},
            {"clause_no": "1.2", "clause_title": "1.2", "clause_body": "也短"},
        ]
    }

    assert not is_power_spec_metadata_ready(metadata)
