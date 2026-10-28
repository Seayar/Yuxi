"""故障图谱 kg seed 导入单元测试：结构校验、schema 辅助与写入编排。"""

from __future__ import annotations

import json

import pytest
from yuxi.knowledge.fault_trace import seed as seed_module
from yuxi.knowledge.fault_trace.seed import (
    LABEL_CAUSE,
    LABEL_DEVICE,
    LABEL_FAULT_MODE,
    LABEL_MEASURE,
    LABEL_PHENOMENON,
    LABEL_TRIGGER,
    KgSeedError,
    load_fault_seed,
)

pytestmark = pytest.mark.unit


def _valid_seed() -> dict:
    return {
        "metadata": {"source": "人工录入", "version": 1},
        "devices": [
            {"name": "1号主变", "type": "变压器", "voltage": "110kV", "spec": "SSZ11-50000"},
            {"name": "10kV开闭所", "type": "开闭所", "voltage": "10kV"},
        ],
        "fault_modes": [
            {
                "id": "fm1",
                "device": "1号主变",
                "name": "冷却系统故障",
                "phenomena": ["冷却器风扇无运转声音", "顶层油温升高"],
                "causes": ["冷却器风扇电机停转", "散热片积尘严重"],
                "measures": {
                    "冷却器风扇电机停转": ["检查风扇电机电源与控制回路", "恢复冷却器风扇运行"],
                    "散热片积尘严重": ["清洗散热片积尘"],
                },
                "triggers": ["冷却器风扇运行电流 = 0A 且顶层油温 > 85℃ → 冷却器风扇电机停转"],
            }
        ],
        "topology": [
            {"source": "35kV母线", "target": "1号主变"},
            {"source": "1号主变", "target": "10kV开闭所"},
        ],
    }


def test_load_fault_seed_valid_json(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(_valid_seed(), ensure_ascii=False), encoding="utf-8")

    seed = load_fault_seed(path)

    assert len(seed["devices"]) == 2
    assert len(seed["fault_modes"]) == 1
    assert len(seed["topology"]) == 2


def test_load_fault_seed_rejects_bad_structure(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"devices": [], "fault_modes": []}), encoding="utf-8")

    with pytest.raises(KgSeedError, match="devices"):
        load_fault_seed(path)


def test_load_fault_seed_rejects_unknown_device_reference(tmp_path):
    seed = _valid_seed()
    seed["fault_modes"][0]["device"] = "不存在的设备"
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(KgSeedError, match="不存在的设备"):
        load_fault_seed(path)


def test_load_fault_seed_rejects_empty_topology_endpoint(tmp_path):
    seed = _valid_seed()
    seed["topology"][0]["target"] = "  "
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(KgSeedError, match="topology"):
        load_fault_seed(path)


def test_node_id_is_deterministic():
    assert seed_module._node_id(LABEL_FAULT_MODE, "冷却系统故障") == seed_module._node_id(
        LABEL_FAULT_MODE, "冷却系统故障"
    )
    assert seed_module._node_id(LABEL_FAULT_MODE, "冷却系统故障") != seed_module._node_id(
        LABEL_FAULT_MODE, "其他故障"
    )


def test_label_constants_match_adapter_schema():
    """历史 kg 迁移写入的标签必须与迁移目标一致（运行时读取 kg3，见 graph_kg3.py）。"""

    from yuxi.knowledge.fault_trace.contracts import KG_ENTITY_LABELS, KG_RELATION_TYPES

    assert {
        LABEL_DEVICE,
        LABEL_FAULT_MODE,
        LABEL_PHENOMENON,
        LABEL_CAUSE,
        LABEL_MEASURE,
        LABEL_TRIGGER,
    } <= set(KG_ENTITY_LABELS)
    assert {
        seed_module.REL_HAS_FAULT_MODE,
        seed_module.REL_PHENOMENON,
        seed_module.REL_CAUSE,
        seed_module.REL_MEASURE,
        seed_module.REL_TRIGGER,
        seed_module.REL_TOPOLOGY,
    } <= set(KG_RELATION_TYPES)


class _FakeResult:
    def __init__(self, rows=None) -> None:
        self._rows = rows or []

    def __iter__(self):
        return iter(self._rows)

    def consume(self):
        return None

    def single(self):
        return self._rows[0] if self._rows else None

    def data(self):
        return self._rows


class _FakeSession:
    """记录所有 Cypher 调用的替身会话。"""

    def __init__(self, recorder: list, lookup: dict) -> None:
        self._recorder = recorder
        self._lookup = lookup

    def run(self, cypher: str, **params):
        self._recorder.append((cypher, params))
        if "a.名称 = $nm" in cypher and self._lookup.get("device_id"):
            return _FakeResult([{"id": self._lookup["device_id"]}])
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeDriver:
    """记录所有写入调用的替身驱动。"""

    def __init__(self, lookup: dict | None = None) -> None:
        self.calls: list = []
        self._lookup = lookup or {}

    def session(self):
        return _FakeSession(self.calls, self._lookup)

    def close(self):
        return None


class _FakeGraphDatabase:
    """替身 GraphDatabase.driver 工厂。"""

    def __init__(self, driver: _FakeDriver) -> None:
        self._driver = driver

    def driver(self, *args, **kwargs):
        return self._driver


def test_seed_writes_schema_then_nodes_and_edges(monkeypatch):
    """导入编排：先注册 schema，再按主键写节点与边（MERGE 幂等）。"""

    fake = _FakeDriver()
    monkeypatch.setattr(seed_module, "GraphDatabase", _FakeGraphDatabase(fake))

    result = seed_module.seed_fault_graph_kg(
        uri="bolt://stub:7688",
        graph="kg3",
        username="admin",
        password="secret",
        seed=load_fault_seed_from_dict(_valid_seed()),
    )

    assert result["status"] == "success"
    assert result["devices"] == 2  # 1号主变 + 10kV开闭所（拓扑引入的 35kV母线不计入 devices 计数）
    assert result["fault_modes"] == 1
    assert result["phenomena"] == 2
    assert result["causes"] == 2
    assert result["measures"] == 3
    assert result["triggers"] == 1
    assert result["relations"] == 11  # 1 存在故障模式 + 2 表现为 + 2 起因 + 3 处理 + 1 诱发条件 + 2 电连接

    cyphers = [call[0] for call in fake.calls]
    # schema 注册（读取既有标签）
    assert any("db.vertexLabels" in c for c in cyphers)
    assert any("db.edgeLabels" in c for c in cyphers)
    # 故障节点按 名称 主键 MERGE（不带反引号）
    assert any("MERGE (n:故障模式 {名称: $name})" in c for c in cyphers)
    # 设备节点按 id 主键 MERGE
    assert any("MERGE (n:设备实例 {id: $id})" in c for c in cyphers)
    # 边按两端主键 MATCH + MERGE
    assert any("MERGE (a)-[r:存在故障模式]->(b)" in c for c in cyphers)
    assert any("MERGE (a)-[r:电连接]->(b)" in c for c in cyphers)


def test_seed_reuses_existing_ledger_device(monkeypatch):
    """设备已存在时按台账 id 复用，不新建节点。"""

    fake = _FakeDriver(lookup={"device_id": "LGR#existing"})
    monkeypatch.setattr(seed_module, "GraphDatabase", _FakeGraphDatabase(fake))

    seed_module.seed_fault_graph_kg(
        uri="bolt://stub:7688",
        graph="kg3",
        username="admin",
        password="secret",
        seed=load_fault_seed_from_dict(_valid_seed()),
    )

    # 已有台账设备不新建节点
    assert not [call for call in fake.calls if "MERGE (n:设备实例 {id: $id})" in call[0]]
    # 设备→故障模式边使用台账 id
    device_edges = [call for call in fake.calls if "MERGE (a)-[r:存在故障模式]->(b)" in call[0]]
    assert device_edges and device_edges[0][1].get("sid") == "LGR#existing"


def load_fault_seed_from_dict(seed: dict) -> dict:
    """绕过文件读取，直接构造 load_fault_seed 的返回形状（复用其校验）。"""

    devices = seed["devices"]
    fault_modes = seed["fault_modes"]
    topology = seed["topology"]
    seed_module._validate(devices, fault_modes, topology)
    return {
        "metadata": seed.get("metadata") or {},
        "devices": devices,
        "fault_modes": fault_modes,
        "topology": topology,
    }
