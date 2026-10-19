import pytest

from yuxi.services import knowledge_task_service

pytestmark = pytest.mark.asyncio


class _FakeKnowledgeBase:
    """按窗口返回带行号前缀的内容，用 window 模拟 open_file_content 的行数上限。"""

    def __init__(self, lines: list[str], window: int = 2):
        self._lines = lines
        self._window = window
        self.calls: list[int] = []

    async def open_file_content(self, kb_id: str, file_id: str, offset: int = 0, limit: int = 800) -> dict:
        self.calls.append(offset)
        start = min(max(offset, 0), len(self._lines))
        selected = self._lines[start : start + min(limit, self._window)]
        end = start + len(selected)
        return {
            "has_more_after": end < len(self._lines),
            "next_offset": end if end < len(self._lines) else None,
            "content": "\n".join(f"{start + idx + 1:6d}\t{line}" for idx, line in enumerate(selected)),
        }


async def test_read_power_spec_source_reads_all_windows(monkeypatch):
    fake = _FakeKnowledgeBase(["第一行", "第二行", "第三行", "第四行", "第五行"])
    monkeypatch.setattr(knowledge_task_service, "knowledge_base", fake)

    text = await knowledge_task_service._read_power_spec_source("kb_1", "file_1")

    assert text.splitlines() == ["第一行", "第二行", "第三行", "第四行", "第五行"]
    assert fake.calls == [0, 2, 4]


async def test_read_power_spec_source_stops_when_offset_does_not_advance(monkeypatch):
    class StuckKnowledgeBase(_FakeKnowledgeBase):
        async def open_file_content(self, kb_id: str, file_id: str, offset: int = 0, limit: int = 800) -> dict:
            return {"has_more_after": True, "next_offset": offset, "content": "     1\t第一行"}

    monkeypatch.setattr(knowledge_task_service, "knowledge_base", StuckKnowledgeBase(["第一行"]))

    text = await knowledge_task_service._read_power_spec_source("kb_1", "file_1")

    assert text == "第一行"


class _FakeTaskContext:
    def __init__(self, payload: dict):
        self.task_id = "task_1"
        self.worker_id = "worker_1"
        self.payload = payload
        self.result = None

    async def set_progress(self, progress: float, message: str | None = None) -> None:
        return None

    async def set_result(self, result: dict) -> None:
        self.result = result

    async def raise_if_cancelled(self) -> None:
        return None


class _FakeIndexKnowledgeBase:
    """只记录参数，供入库路径断言使用。"""

    def __init__(self):
        self.updated_params: list[dict] = []
        self.indexed: list[str] = []

    async def update_file_params(self, kb_id, file_id, params, operator_id=None) -> None:
        self.updated_params.append(params)

    async def index_file(self, kb_id, file_id, operator_id=None, params=None, **kwargs) -> dict:
        self.indexed.append(file_id)
        return {"file_id": file_id, "status": "indexed"}


async def test_run_knowledge_index_rebuilds_power_spec_metadata(monkeypatch):
    """负向：重新入库也要重建条款元数据。

    只改分块策略不会产生 `processing_params.power_spec`，缺少它会让电力规范检索把整个
    知识库判为未完成条款级重建，所以入库路径必须与解析路径一样带上这个钩子。
    """

    fake = _FakeIndexKnowledgeBase()
    rebuilt: list[str] = []

    async def fake_attach(kb_id: str, file_id: str, result: dict) -> None:
        rebuilt.append(file_id)

    monkeypatch.setattr(knowledge_task_service, "knowledge_base", fake)
    monkeypatch.setattr(knowledge_task_service, "attach_power_spec_rebuild", fake_attach)

    context = _FakeTaskContext({"kb_id": "kb_1", "file_ids": ["file_1", "file_2"], "params": {}, "operator_id": "u_1"})
    await knowledge_task_service.run_knowledge_index(context)

    assert fake.indexed == ["file_1", "file_2"]
    assert rebuilt == ["file_1", "file_2"]
