import pytest

from yuxi.knowledge.power_spec.ingestion import parse_power_spec_text
from yuxi.repositories.power_spec_repository import PowerSpecRepository


class FakeKnowledgeFile:
    def __init__(self, processing_params=None):
        self.processing_params = processing_params
        self.updated_by = None


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, knowledge_file):
        self.knowledge_file = knowledge_file
        self.flushed = False

    async def execute(self, _statement):
        return FakeResult(self.knowledge_file)

    async def flush(self):
        self.flushed = True


@pytest.fixture
def document():
    return parse_power_spec_text(
        "DL/T 572—2021 电力变压器运行规程\n6.2.1 变压器巡检周期\n有人值班变电站每日巡视。",
        document_id="file-1",
    )


@pytest.mark.asyncio
async def test_repository_merges_power_spec_metadata_without_overwriting_other_processing_params(document):
    knowledge_file = FakeKnowledgeFile({"parser": {"name": "existing"}})
    session = FakeSession(knowledge_file)

    await PowerSpecRepository().save_power_spec_extraction(
        session,
        kb_id="kb-1",
        file_id="file-1",
        document=document,
        metadata={"schema": "power_spec.v1", "document_id": "file-1"},
    )

    assert knowledge_file.processing_params["parser"] == {"name": "existing"}
    assert knowledge_file.processing_params["power_spec"]["standard_no"] == "DL/T 572-2021"
    assert knowledge_file.processing_params["power_spec"]["clauses"][0]["clause_no"] == "6.2.1"
    assert knowledge_file.updated_by == "power_spec"
    assert session.flushed is True


@pytest.mark.asyncio
async def test_repository_rejects_missing_or_wrong_knowledge_file(document):
    session = FakeSession(None)

    with pytest.raises(ValueError, match="知识库文件不存在"):
        await PowerSpecRepository().save_power_spec_extraction(
            session,
            kb_id="kb-1",
            file_id="file-1",
            document=document,
            metadata={"schema": "power_spec.v1"},
        )
