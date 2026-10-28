"""故障台账（MinIO JSON）设备 ID 解析单元测试。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace.ledger import LedgerRepository

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
async def test_find_device_prefers_row_with_attributes(repository):
    device = await repository.find_device("#1主变压器")

    assert device is not None
    assert device.attribute_codes == ("winding_local_temp",)


@pytest.mark.asyncio
async def test_find_device_returns_none_for_unknown_name(repository):
    assert await repository.find_device("不存在的设备") is None


@pytest.mark.asyncio
async def test_find_device_returns_none_when_ledger_unavailable(monkeypatch):
    repo = LedgerRepository()

    async def fail_load(*, force: bool = False):
        del force
        raise RuntimeError("minio down")

    monkeypatch.setattr(repo, "load", fail_load)

    assert await repo.find_device("#1主变压器") is None


@pytest.fixture
def dirty_repository(monkeypatch):
    """台账里存在空名称行与 ``type`` 为 null/对象的脏行。"""

    repo = LedgerRepository()
    rows = [
        {"id": "blank", "name": "", "pid": None, "attributes": None},
        {"id": "blank-2", "name": "   ", "pid": None, "attributes": None},
        {"id": "t1", "name": "#2主变压器", "pid": None, "type": None},
        {"id": "t2", "name": "#3主变压器", "pid": None, "type": {"name": "油浸变压器"}},
        {"id": "t3", "name": "#4主变压器", "pid": None, "type": "干式变压器"},
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
    device = await dirty_repository.find_device("#2主变压器")
    assert device is not None
    assert device.device_id == "t1"


@pytest.mark.asyncio
async def test_type_null_or_object_does_not_break_lookup(dirty_repository):
    null_type = await dirty_repository.find_device("#2主变压器")
    object_type = await dirty_repository.find_device("#3主变压器")
    str_type = await dirty_repository.find_device("#4主变压器")

    assert null_type is not None and null_type.device_type is None
    assert object_type is not None and object_type.device_type == "油浸变压器"
    assert str_type is not None and str_type.device_type == "干式变压器"
