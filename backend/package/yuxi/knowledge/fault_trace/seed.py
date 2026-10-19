"""故障图谱数据导入独立 TuGraph ``kg`` 图（中文 schema）。

把维护好的 JSON 故障图谱数据以中文标签幂等写入外部 ``kg`` 图，供 ``graph_kg``
适配器读取。写入使用 Cypher MERGE（按 名称+标签 归并节点、按类型+两端名称归并
关系），重复导入不会产生重复记录。语义正确性由人工维护与真实图谱回读校验。

JSON 文件结构：:

    {
      "metadata": {"source": "人工录入", "version": 1},
      "devices": [{"name": "1号主变", "type": "变压器", "voltage": "110kV"}],
      "fault_modes": [{
        "id": "fm1",
        "device": "1号主变",
        "name": "冷却系统故障",
        "phenomena": ["冷却器风扇无运转声音", "顶层油温升高"],
        "causes": ["冷却器风扇电机停转", "散热片积尘严重"],
        "measures": {"冷却器风扇电机停转": ["检查风扇电机电源与控制回路", "恢复冷却器风扇运行"]},
        "triggers": ["冷却器风扇运行电流 = 0A 且顶层油温 > 85℃ → 冷却器风扇电机停转"]
      }],
      "topology": [
        {"source": "35kV母线", "target": "1号主变"},
        {"source": "1号主变", "target": "10kV开闭所"}
      ]
    }
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from yuxi.utils import logger

# 与 graph_kg 适配器保持一致的中文标签/关系
LABEL_DEVICE = "设备实例"
LABEL_FAULT_MODE = "故障模式"
LABEL_PHENOMENON = "故障现象"
LABEL_CAUSE = "故障原因"
LABEL_MEASURE = "处置措施"
LABEL_TRIGGER = "诱发因素"

REL_HAS_FAULT_MODE = "存在故障模式"
REL_PHENOMENON = "表现为"
REL_CAUSE = "起因"
REL_MEASURE = "处理"
REL_TRIGGER = "诱发条件"
REL_TOPOLOGY = "电连接"


class KgSeedError(Exception):
    """kg 故障图谱 seed 数据或导入失败。"""


def load_fault_seed(path: str | Path) -> dict[str, Any]:
    """读取并校验 seed 文件的基础结构（devices 至少一条）。"""

    seed_path = Path(path)
    if not seed_path.is_file():
        raise KgSeedError(f"seed 文件不存在: {seed_path}")
    try:
        with seed_path.open(encoding="utf-8") as handle:
            seed = json.load(handle)
    except json.JSONDecodeError as exc:
        raise KgSeedError(f"seed 文件不是合法 JSON: {exc}") from exc
    if not isinstance(seed, dict):
        raise KgSeedError("seed 顶层必须是 JSON 对象")
    devices = seed.get("devices") or []
    fault_modes = seed.get("fault_modes") or []
    topology = seed.get("topology") or []
    if not devices:
        raise KgSeedError("seed 必须包含 devices")
    if not fault_modes:
        raise KgSeedError("seed 必须包含 fault_modes")
    _validate(devices, fault_modes, topology)
    return {
        "metadata": seed.get("metadata") or {},
        "devices": devices,
        "fault_modes": fault_modes,
        "topology": topology,
    }


def _validate(devices: list[Any], fault_modes: list[Any], topology: list[Any]) -> None:
    if not all(isinstance(item, dict) for item in devices + fault_modes + topology):
        raise KgSeedError("devices/fault_modes/topology 的元素必须是对象")
    device_names = {str(device.get("name") or "").strip() for device in devices if isinstance(device, dict)}
    for mode in fault_modes:
        device = str(mode.get("device") or "").strip()
        if device and device not in device_names:
            raise KgSeedError(f"fault_mode {mode.get('id', mode.get('name'))} 引用了 devices 不存在的设备: {device}")
    for edge in topology:
        if not str(edge.get("source") or "").strip() or not str(edge.get("target") or "").strip():
            raise KgSeedError("topology 边的 source/target 不能为空")


def _node_id(label: str, name: str) -> str:
    return f"{label}#{hashlib.md5(name.encode('utf-8')).hexdigest()[:16]}"


_NODE_LABEL_FIELDS = {
    LABEL_DEVICE: ("名称", "显示名", "id"),
    LABEL_FAULT_MODE: ("名称", "显示名", "id"),
    LABEL_PHENOMENON: ("名称", "显示名", "id"),
    LABEL_CAUSE: ("名称", "显示名", "id"),
    LABEL_MEASURE: ("名称", "显示名", "id"),
    LABEL_TRIGGER: ("名称", "显示名", "id"),
}

_EDGE_CONSTRAINTS = {
    REL_HAS_FAULT_MODE: (LABEL_DEVICE, LABEL_FAULT_MODE),
    REL_PHENOMENON: (LABEL_FAULT_MODE, LABEL_PHENOMENON),
    REL_CAUSE: (LABEL_FAULT_MODE, LABEL_CAUSE),
    REL_MEASURE: (LABEL_CAUSE, LABEL_MEASURE),
    REL_TRIGGER: (LABEL_FAULT_MODE, LABEL_TRIGGER),
    REL_TOPOLOGY: (LABEL_DEVICE, LABEL_DEVICE),
}


def _ensure_kg_schema(driver) -> None:
    """幂等注册故障溯源所需的中文点/边标签；已存在时跳过。

    TuGraph pattern 中中文标签只有注册后才可写（不带反引号的 ``MERGE (n:故障模式)``）。
    """

    with driver.session() as session:
        registered_vertex = {record["label"] for record in session.run("CALL db.vertexLabels()")}
        registered_edge = {record.get("label") for record in session.run("CALL db.edgeLabels()")}
        for label, primary in _NODE_LABEL_FIELDS.items():
            if label in registered_vertex:
                continue
            session.run(
                f"CALL db.createVertexLabel('{label}', '{primary}', "
                f"'{primary}', 'STRING', false, '显示名', 'STRING', true, 'id', 'STRING', true)"
            ).consume()
        for label, (source, target) in _EDGE_CONSTRAINTS.items():
            if label in registered_edge:
                continue
            session.run(
                f"CALL db.createEdgeLabel('{label}', '[[\"{source}\",\"{target}\"]]', "
                f"'source', 'STRING', true, 'target', 'STRING', true)"
            ).consume()


def _find_device_id(driver, name: str) -> str | None:
    """按名称查找既有设备实例（台账）的 id。"""

    with driver.session() as session:
        row = session.run(
            "MATCH (a:设备实例) WHERE a.名称 = $nm OR a.显示名 = $nm RETURN a.id AS id LIMIT 1",
            nm=name,
        ).single()
    return str(row["id"]) if row and row.get("id") else None


def _ensure_device(driver, name: str) -> str:
    """返回设备实例的 id：优先复用台账节点，不存在时新建。"""

    device_id = _find_device_id(driver, name)
    if device_id:
        return device_id
    device_id = _node_id(LABEL_DEVICE, name)
    with driver.session() as session:
        session.run(
            "MERGE (n:设备实例 {id: $id}) SET n.名称 = $nm, n.显示名 = $nm",
            id=device_id,
            nm=name,
        )
    return device_id


def _merge_fault_node(driver, *, label: str, name: str) -> None:
    """MERGE 故障节点（主键为 名称，不带反引号）；已存在时仅补齐 id/显示名。"""

    with driver.session() as session:
        session.run(
            f"MERGE (n:{label} {{名称: $name}}) SET n.显示名 = $display, n.id = $nid",
            name=name,
            display=name,
            nid=_node_id(label, name),
        )


def _merge_edge_by_id(
    driver,
    *,
    source_id: str,
    rel: str,
    target_id: str,
) -> None:
    """设备↔设备边（电连接）：两端按主键 id 匹配。"""

    with driver.session() as session:
        session.run(
            f"MATCH (a:设备实例 {{id: $sid}}) MATCH (b:设备实例 {{id: $tid}}) MERGE (a)-[r:{rel}]->(b)",
            sid=source_id,
            tid=target_id,
        )


def _merge_edge_from_device(
    driver,
    *,
    device_id: str,
    rel: str,
    target_label: str,
    target_name: str,
) -> None:
    """设备→故障节点边：设备按 id 匹配，故障节点按名称主键匹配。"""

    with driver.session() as session:
        session.run(
            f"MATCH (a:设备实例 {{id: $sid}}) MATCH (b:{target_label} {{名称: $tn}}) MERGE (a)-[r:{rel}]->(b)",
            sid=device_id,
            tn=target_name,
        )


def _merge_edge_fault(
    driver,
    *,
    source_label: str,
    source_name: str,
    rel: str,
    target_label: str,
    target_name: str,
) -> None:
    """故障节点→故障节点边：两端按名称主键匹配。"""

    with driver.session() as session:
        session.run(
            f"MATCH (a:{source_label} {{名称: $sn}}) MATCH (b:{target_label} {{名称: $tn}}) MERGE (a)-[r:{rel}]->(b)",
            sn=source_name,
            tn=target_name,
        )


def seed_fault_graph_kg(
    *,
    uri: str,
    graph: str,
    username: str,
    password: str,
    seed: dict[str, Any],
) -> dict[str, Any]:
    """把 seed 幂等写入外部 kg 图，返回累计实体/关系计数。"""

    counters = {"devices": 0, "fault_modes": 0, "phenomena": 0, "causes": 0, "measures": 0, "triggers": 0}
    edge_count = 0
    driver = GraphDatabase.driver(uri, auth=(username, password), database=graph)
    try:
        _ensure_kg_schema(driver)
        for device in seed["devices"]:
            name = str(device.get("name") or "").strip()
            _ensure_device(driver, name)
            counters["devices"] += 1

        for mode in seed["fault_modes"]:
            device = str(mode.get("device") or "").strip()
            mode_name = str(mode.get("name") or "").strip()
            if not mode_name:
                raise KgSeedError("fault_mode.name 不能为空")
            _merge_fault_node(driver, label=LABEL_FAULT_MODE, name=mode_name)
            counters["fault_modes"] += 1
            if device:
                device_id = _ensure_device(driver, device)
                _merge_edge_from_device(
                    driver,
                    device_id=device_id,
                    rel=REL_HAS_FAULT_MODE,
                    target_label=LABEL_FAULT_MODE,
                    target_name=mode_name,
                )
                edge_count += 1
            for phenomenon in mode.get("phenomena") or []:
                pname = str(phenomenon).strip()
                if not pname:
                    continue
                _merge_fault_node(driver, label=LABEL_PHENOMENON, name=pname)
                counters["phenomena"] += 1
                _merge_edge_fault(
                    driver,
                    source_label=LABEL_FAULT_MODE,
                    source_name=mode_name,
                    rel=REL_PHENOMENON,
                    target_label=LABEL_PHENOMENON,
                    target_name=pname,
                )
                edge_count += 1
            for cause in mode.get("causes") or []:
                cname = str(cause).strip()
                if not cname:
                    continue
                _merge_fault_node(driver, label=LABEL_CAUSE, name=cname)
                counters["causes"] += 1
                _merge_edge_fault(
                    driver,
                    source_label=LABEL_FAULT_MODE,
                    source_name=mode_name,
                    rel=REL_CAUSE,
                    target_label=LABEL_CAUSE,
                    target_name=cname,
                )
                edge_count += 1
                for measure in (mode.get("measures") or {}).get(cause) or []:
                    mname = str(measure).strip()
                    if not mname:
                        continue
                    _merge_fault_node(driver, label=LABEL_MEASURE, name=mname)
                    counters["measures"] += 1
                    _merge_edge_fault(
                        driver,
                        source_label=LABEL_CAUSE,
                        source_name=cname,
                        rel=REL_MEASURE,
                        target_label=LABEL_MEASURE,
                        target_name=mname,
                    )
                    edge_count += 1
            for trigger in mode.get("triggers") or []:
                tname = str(trigger).strip()
                if not tname:
                    continue
                _merge_fault_node(driver, label=LABEL_TRIGGER, name=tname)
                counters["triggers"] += 1
                _merge_edge_fault(
                    driver,
                    source_label=LABEL_FAULT_MODE,
                    source_name=mode_name,
                    rel=REL_TRIGGER,
                    target_label=LABEL_TRIGGER,
                    target_name=tname,
                )
                edge_count += 1

        for edge in seed["topology"]:
            source = str(edge.get("source") or "").strip()
            target = str(edge.get("target") or "").strip()
            source_id = _ensure_device(driver, source)
            target_id = _ensure_device(driver, target)
            _merge_edge_by_id(
                driver,
                source_id=source_id,
                rel=REL_TOPOLOGY,
                target_id=target_id,
            )
            edge_count += 1
    finally:
        driver.close()
    logger.info(f"故障图谱 kg seed 写入 uri={uri} graph={graph} counters={counters} edges={edge_count}")
    return {"status": "success", **counters, "relations": edge_count}


def main() -> int:
    parser = argparse.ArgumentParser(description="导入故障图谱数据到独立 TuGraph kg 图")
    parser.add_argument("--uri", required=True, help="TuGraph Bolt 地址，如 bolt://<host>:7688")
    parser.add_argument("--graph", default="kg", help="图名称，默认 kg")
    parser.add_argument("--username", required=True, help="TuGraph 用户名")
    parser.add_argument("--password", required=True, help="TuGraph 密码")
    parser.add_argument("--file", required=True, help="故障图谱 seed JSON 文件路径")
    args = parser.parse_args()
    try:
        seed = load_fault_seed(args.file)
        result = seed_fault_graph_kg(
            uri=args.uri,
            graph=args.graph,
            username=args.username,
            password=args.password,
            seed=seed,
        )
    except KgSeedError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"导入异常：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
