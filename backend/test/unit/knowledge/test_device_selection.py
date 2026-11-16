"""设备名索引与设备候选模型选择的单元测试。"""

from __future__ import annotations

import json

import pytest

from yuxi.knowledge import device_index
from yuxi.knowledge.device_index import (
    DeviceNameIndex,
    device_recall_tokens,
    device_tokens,
    fallback_score,
    get_device_name_index,
    graph_key,
    normalize_device_text,
    reset_device_name_index,
)
from yuxi.knowledge.device_selection import (
    STATE_ERROR,
    STATE_INVALID_OUTPUT,
    STATE_MODEL_UNAVAILABLE,
    STATE_NO_CANDIDATES,
    STATE_NO_SELECTION,
    STATE_SELECTED,
    DeviceOption,
    build_candidate_payload,
    build_selection_prompt,
    parse_selection,
    select_device,
)

pytestmark = pytest.mark.unit

DEVICES = [
    {"id": "LGR#SUPLINEYK", "名称": "银开线", "层级路径": "变电设施 > 110kV变电站 > 银开线", "设备分类": "输电线路"},
    {"id": "LGR#SUPLINELK", "名称": "岭开线", "层级路径": "变电设施 > 110kV变电站 > 岭开线", "设备分类": "输电线路"},
    {"id": "LGR#kbs1", "名称": "#1开闭所", "层级路径": "变电设施 > 110kV变电站 > #1开闭所", "设备分类": "开闭所"},
    {"id": "LGR#kbs2", "名称": "#2开闭所", "层级路径": "变电设施 > 35kV#2变电站 > #2开闭所", "设备分类": "开闭所"},
    {"id": "LGR#B110LK", "名称": "岭开线进线间隔_1101", "层级路径": "变电设施 > 110kV变电站 > 岭开线进线间隔_1101", "设备分类": "间隔"},
]


class _FakeIndexClient:
    def __init__(self, records=None, *, fail: bool = False) -> None:
        self.records = records if records is not None else [{"n": item} for item in DEVICES]
        self.fail = fail
        self.calls = 0

    def run(self, cypher: str, **params):
        self.calls += 1
        if self.fail:
            raise RuntimeError("graph unavailable")
        return self.records


def _index() -> DeviceNameIndex:
    return DeviceNameIndex.from_records([{"n": item} for item in DEVICES], name_props=("名称",))


def test_normalize_and_tokens_handle_voltage_and_ordinals():
    assert normalize_device_text(" 110kV 银开线#1 ") == "110kv银开线1"
    tokens = device_tokens("35kV联络一线")
    assert "35kv" in tokens and "联络" in tokens and "一线" in tokens
    # 单字词元区分度低，不进入召回。
    assert "1" not in device_tokens("1号主变")
    assert "主变" in device_tokens("1号主变")


def test_recall_tokens_keep_case_and_fallback_score_prefers_hits():
    """Cypher CONTAINS 区分大小写：图查询词元要保留原始大小写。"""

    assert device_recall_tokens("3B-7Y 故障") == ["3B", "7Y", "故障"]
    assert device_recall_tokens("SVG 柜")[0] == "SVG"
    # 命中词元越多分越高；索引命中至少排在纯词元命中之上。
    assert fallback_score("3B-7Y", ["3B", "7Y"]) > fallback_score("3B-7Y", ["7Y"])
    assert fallback_score("银开线", ["银开"]) > 1.0
    assert fallback_score("无关设备", ["银开"]) == 1.0
    assert fallback_score("无关设备", [], index_rank=0) > fallback_score("无关设备", [])


def test_index_search_matches_exact_contains_and_fuzzy():
    index = _index()

    names = [item.name for item in index.search("银开线")]
    assert names[0] == "银开线"
    assert "#1开闭所" in [item.name for item in index.search("110kV变电站 #1开闭所")]
    assert "岭开线进线间隔_1101" in [item.name for item in index.search("岭开线进线间隔1101")]
    assert index.search("完全无关的甲设备") == []


def test_index_cache_reuses_result_until_reset(monkeypatch):
    client = _FakeIndexClient()
    monkeypatch.setattr(device_index, "DEVICE_INDEX_TTL_SECONDS", 600)
    reset_device_name_index()

    first = get_device_name_index(client, graph_key="kg5", label="设备实例", name_props=("名称",))
    second = get_device_name_index(client, graph_key="kg5", label="设备实例", name_props=("名称",))

    assert first is second
    assert client.calls == 1
    reset_device_name_index()
    assert graph_key(type("Config", (), {"uri": "bolt://x", "graph": "kg5"})()) == "bolt://x|kg5"


def test_index_build_failure_returns_none():
    client = _FakeIndexClient(fail=True)
    reset_device_name_index()

    assert get_device_name_index(client, graph_key="kg5", label="设备实例", name_props=("名称",)) is None


def test_candidate_payload_trims_path_and_dedupes():
    options = [
        DeviceOption("a", "设备A", "开闭所", "10kV", "站1 > 站2 > 站3 > 设备A", "开闭所"),
        DeviceOption("a", "设备A", "开闭所", "10kV", "重复"),
        DeviceOption("b", "设备B"),
    ]

    payload = build_candidate_payload(options, limit=5, path_segments=2)

    assert [item["id"] for item in payload] == ["a", "b"]
    assert payload[0]["path"] == "站3 > 设备A"
    assert "type" not in payload[1]


def test_selection_prompt_contains_rules_and_candidates():
    options = [DeviceOption("a", "银开线", "架空线路", "110kV")]
    payload = build_candidate_payload(options)

    prompt = build_selection_prompt("110kV银开线故障", {"device_name": "银开线"}, payload)

    assert "只能从候选的 id 中选择" in prompt
    assert "null" in prompt
    assert '"id": "a"' in prompt


def test_parse_selection_accepts_only_known_ids():
    candidates = [{"id": "a", "name": "设备A"}, {"id": "b", "name": "设备B"}]

    assert parse_selection(json.dumps({"device_id": "b"}), candidates) == ("b", STATE_SELECTED)
    assert parse_selection('```json\n{"device_id": null}\n```', candidates) == (None, STATE_NO_SELECTION)
    assert parse_selection(json.dumps({"device_id": "c"}), candidates) == (None, STATE_INVALID_OUTPUT)
    assert parse_selection("不是 JSON", candidates) == (None, STATE_INVALID_OUTPUT)


@pytest.mark.asyncio
async def test_select_device_states(monkeypatch):
    options = [DeviceOption("a", "银开线", "架空线路", "110kV")]
    monkeypatch.setattr("yuxi.knowledge.device_selection.DEVICE_SELECTION_ENABLED", True)

    async def choosing(_prompt):
        return json.dumps({"device_id": "a"})

    selected, state = await select_device(options, user_text="银开线", model_call=choosing)
    assert (selected, state) == ("a", STATE_SELECTED)

    assert await select_device([], user_text="x", model_call=choosing) == (None, STATE_NO_CANDIDATES)
    assert await select_device(options, user_text="x", model_call=None) == (None, STATE_MODEL_UNAVAILABLE)

    async def failing(_prompt):
        raise RuntimeError("timeout")

    assert await select_device(options, user_text="x", model_call=failing) == (None, STATE_ERROR)

    async def garbage(_prompt):
        return "no json"

    assert await select_device(options, user_text="x", model_call=garbage) == (None, STATE_INVALID_OUTPUT)

    monkeypatch.setattr("yuxi.knowledge.device_selection.DEVICE_SELECTION_ENABLED", False)
    assert await select_device(options, user_text="x", model_call=choosing) == (None, STATE_MODEL_UNAVAILABLE)
