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