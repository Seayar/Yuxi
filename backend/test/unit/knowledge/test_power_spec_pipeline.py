import pytest

from yuxi.knowledge.power_spec.ingestion import PowerSpecIngestionService, extract_from_source


POWER_SPEC_TEXT = """DL/T 572—2021 电力变压器运行规程
6.2.1 变压器巡检周期
有人值班变电站每日巡视。
"""


class FakeStore:
    def __init__(self):
        self.saved = None

    async def save_power_spec_extraction(self, **payload):
        self.saved = payload


@pytest.mark.asyncio
async def test_ingestion_parses_before_saving_and_passes_versioned_metadata():
    store = FakeStore()
    document = await PowerSpecIngestionService(store).extract_and_store(
        kb_id="kb-1",
        file_id="file-1",
        text=POWER_SPEC_TEXT,
        source_file="DL_T_572-2021.pdf",
    )

    assert document.standard_no == "DL/T572—2021"
    assert store.saved["kb_id"] == "kb-1"
    assert store.saved["file_id"] == "file-1"
    assert store.saved["metadata"]["schema"] == "power_spec.v1"


@pytest.mark.asyncio
async def test_ingestion_does_not_save_when_parsing_fails():
    store = FakeStore()

    with pytest.raises(ValueError, match="规范编号"):
        await PowerSpecIngestionService(store).extract_and_store(
            kb_id="kb-1",
            file_id="file-1",
            text="没有标准编号的文本",
        )

    assert store.saved is None


@pytest.mark.asyncio
async def test_extract_from_source_keeps_text_source_outside_yuxi_core():
    store = FakeStore()
    calls = []

    async def source(*, kb_id: str, file_id: str):
        calls.append((kb_id, file_id))
        return POWER_SPEC_TEXT

    document = await extract_from_source(
        source=source,
        ingestion=PowerSpecIngestionService(store),
        kb_id="kb-1",
        file_id="file-1",
    )

    assert calls == [("kb-1", "file-1")]
    assert document.document_id == "file-1"
    assert store.saved is not None
