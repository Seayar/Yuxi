"""kg4 分支判定单元测试：假 MGET，不连接 Redis。"""

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

    monkeypatch.setattr(branch_module, "_mget", fake_mget)
    result = asyncio.run(lookup_branch(DEVICE, SCENE))

    assert result.state == expected_state
    assert result.branch == expected_branch


def test_unavailable_when_mget_returns_none(monkeypatch):
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6379/0")

    async def fake_mget(url, keys, *, db, prefix):
        return None

    monkeypatch.setattr(branch_module, "_mget", fake_mget)
    result = asyncio.run(lookup_branch(DEVICE, SCENE))

    assert result.state == "unavailable"
