"""处置方案分支判定单元测试：假 MGET/SCAN，不连接 Redis。"""

from __future__ import annotations

import asyncio

import pytest

from yuxi.knowledge.maintenance_plan import branch as branch_module
from yuxi.knowledge.maintenance_plan.branch import branch_keys, lookup_branch
from yuxi.knowledge.maintenance_plan.contracts import DeviceCandidate, Scenario

pytestmark = pytest.mark.unit


DEVICE = DeviceCandidate(
    "LGR#3k18",
    "3k18",
    "开关柜",
    "10kV",
    "#3开闭所 > 3k18",
    ledger_ids=("L1", "L2"),
)
SCENE = Scenario(
    "SCENE_A2",
    "开关柜整柜更换",
    branch_dimension="布置位置",
    branch1_condition="站内开关室",
    branch2_condition="箱变内",
)


def test_branch_keys_use_each_ledger_id():
    assert branch_keys(DEVICE, SCENE) == ["L1:布置位置", "L2:布置位置"]


def test_no_branch_dimension_returns_not_required(monkeypatch):
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6379/0")
    scene = Scenario("S", "无分支场景")

    result = asyncio.run(lookup_branch(DEVICE, scene))

    assert result.state == "not_required"


def test_not_configured_returns_explicit_state(monkeypatch):
    monkeypatch.delenv("FAULT_TRACE_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    result = asyncio.run(lookup_branch(DEVICE, SCENE))

    assert result.state == "not_configured"


@pytest.mark.parametrize(
    ("values", "expected_state", "expected_branch"),
    [
        (["站内开关室", None], "ok", "分支1"),
        (["别的条件", "站内开关室"], "ok", "分支1"),
        ([" 箱变内 ", "箱变内"], "ok", "分支2"),
        ([None, None], "missing", ""),
        (["站内开关室", "箱变内"], "invalid", ""),
        (["别的条件", None], "invalid", ""),
    ],
)
def test_lookup_states(monkeypatch, values, expected_state, expected_branch):
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6379/0")

    async def fake_mget(url, keys, *, db, prefix):
        return values

    async def fake_scan(url, ledger_ids, *, db, prefix):
        return []

    monkeypatch.setattr(branch_module, "_mget", fake_mget)
    monkeypatch.setattr(branch_module, "_scan_values", fake_scan)
    result = asyncio.run(lookup_branch(DEVICE, SCENE))

    assert result.state == expected_state
    assert result.branch == expected_branch


def test_dimension_alias_uses_value_match(monkeypatch):
    """精确维度没有 key 时，设备其他中文维度 key 的值匹配场景条件也可自动判定。"""

    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6379/0")
    scene = Scenario(
        "SCENE_A3",
        "主变母线带电检修",
        branch_dimension="停电范围",
        branch1_condition="母线可停",
        branch2_condition="母线带电",
    )

    async def fake_mget(url, keys, *, db, prefix):
        return [None, None]

    async def fake_scan(url, ledger_ids, *, db, prefix):
        return [("母线带电", "L1")]

    monkeypatch.setattr(branch_module, "_mget", fake_mget)
    monkeypatch.setattr(branch_module, "_scan_values", fake_scan)
    result = asyncio.run(lookup_branch(DEVICE, scene))

    assert result.state == "ok"
    assert result.branch == "分支2"
    assert result.value == "母线带电"
    assert result.ledger_id == "L1"


def test_dimension_alias_conflicting_values_is_invalid(monkeypatch):
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6379/0")
    scene = Scenario(
        "SCENE_A3",
        "主变母线带电检修",
        branch_dimension="停电范围",
        branch1_condition="母线可停",
        branch2_condition="母线带电",
    )

    async def fake_mget(url, keys, *, db, prefix):
        return [None, None]

    async def fake_scan(url, ledger_ids, *, db, prefix):
        return [("母线带电", "L1"), ("母线可停", "L2")]

    monkeypatch.setattr(branch_module, "_mget", fake_mget)
    monkeypatch.setattr(branch_module, "_scan_values", fake_scan)
    result = asyncio.run(lookup_branch(DEVICE, scene))

    assert result.state == "invalid"


def test_dimension_alias_value_not_matching_is_invalid(monkeypatch):
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6379/0")
    scene = Scenario(
        "SCENE_A3",
        "主变母线带电检修",
        branch_dimension="停电范围",
        branch1_condition="母线可停",
        branch2_condition="母线带电",
    )

    async def fake_mget(url, keys, *, db, prefix):
        return [None, None]

    async def fake_scan(url, ledger_ids, *, db, prefix):
        return [("别的条件", "L1")]

    monkeypatch.setattr(branch_module, "_mget", fake_mget)
    monkeypatch.setattr(branch_module, "_scan_values", fake_scan)
    result = asyncio.run(lookup_branch(DEVICE, scene))

    assert result.state == "invalid"


def test_dimension_alias_scan_failure_is_unavailable(monkeypatch):
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6379/0")

    async def fake_mget(url, keys, *, db, prefix):
        return [None, None]

    async def fake_scan(url, ledger_ids, *, db, prefix):
        return None

    monkeypatch.setattr(branch_module, "_mget", fake_mget)
    monkeypatch.setattr(branch_module, "_scan_values", fake_scan)
    result = asyncio.run(lookup_branch(DEVICE, SCENE))

    assert result.state == "unavailable"


def test_unavailable_when_mget_returns_none(monkeypatch):
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6379/0")

    async def fake_mget(url, keys, *, db, prefix):
        return None

    monkeypatch.setattr(branch_module, "_mget", fake_mget)
    result = asyncio.run(lookup_branch(DEVICE, SCENE))

    assert result.state == "unavailable"
