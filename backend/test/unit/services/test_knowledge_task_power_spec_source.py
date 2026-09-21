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
