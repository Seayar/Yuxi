"""故障台账（MinIO JSON）设备 ID 解析单元测试。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace import ledger as ledger_module
from yuxi.knowledge.fault_trace.ledger import LedgerDevice, LedgerRepository, match_ledger_candidate

pytestmark = pytest.mark.unit

ROWS = [
    {"id": "root", "name": "变电设施", "pid": None, "attributes": None},
    {"id": "sub1", "name": "35kV#1变电站", "pid": "root", "attributes": None},
    {"id": "bay1", "name": "#1主变间隔", "pid": "sub1", "attributes": None},
    {
        "id": "f531",
        "name": "#1主变压器",
        "pid": "bay1",
        "classifyName": "SF6变压器",
        "attributes": [{"name": "绕组温度", "code": "winding_local_temp", "unit": "℃"}],
    },
    {"id": "sub2", "name": "35kV#2变电站", "pid": "root", "attributes": None},
    {
        "id": "other",
        "name": "#1主变压器",
        "pid": "sub2",
        "classifyName": "SF6变压器",
        "attributes": [{"name": "绕组温度", "code": "winding_local_temp", "unit": "℃"}],
    },
]


@pytest.fixture
def repository(monkeypatch):
    repo = LedgerRepository()
    repo.reset()

    async def fake_load(*, force: bool = False):
        del force
        return ROWS

    monkeypatch.setattr(repo, "load", fake_load)
    return repo


@pytest.mark.asyncio
async def test_find_device_uses_ancestor_path_for_same_name(repository):
    device = await repository.find_device("#1主变压器", ancestors=["35kV#1变电站"])

    assert device is not None
    assert device.device_id == "f531"
    assert device.device_type == "SF6变压器"
    assert device.voltage_level == "35kV"
    assert device.path == ("变电设施", "35kV#1变电站", "#1主变间隔")


@pytest.mark.asyncio
async def test_find_device_without_ancestors_rejects_ambiguous_same_name(repository):
    """没有层级线索时，同名台账行不能按数据顺序静默选第一条。"""

    assert await repository.find_device("#1主变压器") is None


@pytest.mark.asyncio
async def test_find_device_returns_none_for_unknown_name(repository):
    assert await repository.find_device("不存在的设备") is None


@pytest.mark.asyncio
async def test_find_device_returns_none_when_ledger_unavailable(monkeypatch):
    repo = LedgerRepository()
    repo.reset()

    async def fail_load(*, force: bool = False):
        del force
        raise RuntimeError("minio down")

    monkeypatch.setattr(repo, "load", fail_load)

    assert await repo.find_device("#1主变压器") is None


@pytest.fixture
def dirty_repository(monkeypatch):
    """台账里存在空名称行与 ``type`` 为 null/对象的脏行。"""

    repo = LedgerRepository()
    repo.reset()
    rows = [
        {"id": "site", "name": "35kV#1变电站", "pid": None, "attributes": None},
        {"id": "blank", "name": "", "pid": None, "attributes": None},
        {"id": "blank-2", "name": "   ", "pid": None, "attributes": None},
        {"id": "t1", "name": "#2主变压器", "pid": "site", "type": None},
        {"id": "t2", "name": "#3主变压器", "pid": "site", "type": {"name": "油浸变压器"}},
        {"id": "t3", "name": "#4主变压器", "pid": "site", "type": "干式变压器"},
    ]

    async def fake_load(*, force: bool = False):
        del force
        return rows

    monkeypatch.setattr(repo, "load", fake_load)
    return repo


@pytest.mark.asyncio
async def test_empty_query_and_blank_rows_never_match(dirty_repository):
    """空查询与空行名不能让 ``"" in name`` 命中无关设备。"""

    assert await dirty_repository.find_device("   ") is None
    device = await dirty_repository.find_device("#2主变压器", ancestors=["35kV#1变电站"])
    assert device is not None
    assert device.device_id == "t1"


@pytest.mark.asyncio
async def test_type_null_or_object_does_not_break_lookup(dirty_repository):
    null_type = await dirty_repository.find_device("#2主变压器", ancestors=["35kV#1变电站"])
    object_type = await dirty_repository.find_device("#3主变压器", ancestors=["35kV#1变电站"])
    str_type = await dirty_repository.find_device("#4主变压器", ancestors=["35kV#1变电站"])

    assert null_type is not None and null_type.device_type is None
    assert object_type is not None and object_type.device_type == "油浸变压器"
    assert str_type is not None and str_type.device_type == "干式变压器"


@pytest.mark.asyncio
async def test_search_devices_returns_all_similar_rows_with_path_and_type(repository):
    """名称近似候选不因同名精确行存在而丢失，并保留层级供用户区分。"""

    devices = await repository.search_devices("一号主变压器", limit=10)

    assert [device.device_id for device in devices[:2]] == ["f531", "other"]
    assert all(device.device_type == "SF6变压器" for device in devices[:2])
    assert devices[0].path == ("变电设施", "35kV#1变电站", "#1主变间隔")


@pytest.mark.asyncio
async def test_search_devices_uses_location_to_rank_same_name(repository):
    devices = await repository.search_devices(
        "#1主变压器",
        location_hint="35kV#2变电站",
        limit=10,
    )

    assert devices[0].device_id == "other"


@pytest.mark.asyncio
async def test_load_repairs_historical_json_syntax_error(monkeypatch):
    """数据团队导出的轻微 JSON 语法瑕疵不能让整个台账召回静默失效。"""

    class _FakeMinio:
        async def adownload_file(self, bucket, object_name):
            assert (bucket, object_name) == (ledger_module.LEDGER_BUCKET, ledger_module.LEDGER_OBJECT)
            return '{"data":[{"id":"ok","name":"#1主变","pid":null,},]}'.encode()

    LedgerRepository.reset()
    monkeypatch.setattr(ledger_module, "get_minio_client", lambda: _FakeMinio())

    rows = await LedgerRepository().load()

    assert rows == [{"id": "ok", "name": "#1主变", "pid": None}]


@pytest.mark.asyncio
async def test_search_devices_does_not_match_only_shared_voltage_text(monkeypatch):
    """公共 ``kV`` 文本不能让无关母线进入线路候选项。"""

    rows = [
        {"id": "line", "name": "银开线", "pid": None},
        {"id": "bus", "name": "10kVⅠ段母线", "pid": None},
    ]
    repo = LedgerRepository()
    repo.reset()

    async def fake_load(*, force: bool = False):
        del force
        return rows

    monkeypatch.setattr(repo, "load", fake_load)

    devices = await repo.search_devices("110kV银开线", limit=10)

    assert [device.device_id for device in devices] == ["line"]


def test_match_ledger_candidate_uses_graph_ancestors_for_same_name():
    """同名设备用图谱层级选区；层级不足时 fail-closed，不把台账 ID 串到别的站。"""

    candidates = [
        LedgerDevice("station-1", "#1主变", path=("变电设施", "35kV#1变电站")),
        LedgerDevice("station-2", "#1主变", path=("变电设施", "35kV#2变电站")),
    ]

    first = match_ledger_candidate(
        name="#1主变",
        path="变电设施 > 35kV#1变电站 > 主变区域",
        candidates=candidates,
    )
    second = match_ledger_candidate(
        name="#1主变",
        path="变电设施 > 35kV#2变电站 > 主变区域",
        candidates=candidates,
    )

    assert first is not None and first.device_id == "station-1"
    assert second is not None and second.device_id == "station-2"
    assert match_ledger_candidate(name="#1主变", path="变电设施 > 未知站", candidates=candidates) is None


def test_match_ledger_candidate_rejects_single_same_name_with_conflicting_path():
    """台账只有一条同名行时，层级冲突仍必须 fail-closed。"""

    candidate = LedgerDevice(
        "station-a",
        "#1主变",
        path=("变电设施", "35kV#1变电站", "主变区域"),
    )

    assert (
        match_ledger_candidate(
            name="#1主变",
            path="变电设施 > 35kV#2变电站 > 主变区域",
            candidates=[candidate],
        )
        is None
    )


def test_match_ledger_candidate_rejects_single_same_name_without_graph_path():
    """图谱层级缺失时，台账中唯一同名行也不能被当作确定匹配。"""

    candidate = LedgerDevice("station-a", "#1主变", path=("变电设施", "35kV#1变电站"))

    assert match_ledger_candidate(name="#1主变", path="", candidates=[candidate]) is None
