"""把处置方案知识（作业模板 / 安全距离 / 班组 / 备品备件）幂等写入 TuGraph ``kg3`` 图。

照搬 ``ref/fault_trace_参考实现/seed.py`` 的结构：``_ensure_kg_schema`` 先注册中文点/边标签、
再用 Cypher ``MERGE`` 按单属性主键 ``id`` 幂等写入、``argparse`` CLI、返回计数。

TuGraph 4.5.2 是强 schema：``SET`` 未在 ``createVertexLabel`` 中声明的属性会报错，因此本模块
把每个标签的全部属性（含以 JSON 字符串存储的数组字段）都在建标签语句里显式声明，并用
``'STRING'`` / ``'INT64'`` / ``'DOUBLE'`` 带引号的类型名。中文标签/属性不加反引号，值一律走
查询参数（参数名用 ASCII），不把数据拼进 Cypher 文本。

凭据规则：密码只从环境变量 ``KG_PASSWORD`` 读取，不设默认值、不提供密码 CLI 参数、不记录密码。
``dry_run=True`` 时不创建 driver、不连接图，只输出将执行的语句数与离线预检结果。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from yuxi.knowledge.maintenance_plan.contracts import (
    LABEL_CLAUSE,
    LABEL_DEVICE,
    LABEL_DEVICE_TYPE,
    LABEL_SAFETY_DISTANCE,
    LABEL_SPARE,
    LABEL_TEAM,
    LABEL_TEMPLATE,
    REL_HAS_TEMPLATE,
    REL_MANAGES,
    REL_SPARE,
    REL_TEMPLATE_CLAUSE,
)
from yuxi.knowledge.maintenance_plan.preflight import PreflightError, preflight_seed
from yuxi.utils import logger

# 密码只从该环境变量读取
PASSWORD_ENV = "KG_PASSWORD"
# seed 默认数据目录：集成后位于 yuxi/knowledge/maintenance_plan/data/
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
# 六份数据文件（三份作业模板合并为 templates）
TEMPLATE_FILES = ("templates_switchgear.json", "templates_transformer.json", "templates_line.json")
SAFETY_DISTANCE_FILE = "safety_distance.json"
TEAM_FILE = "teams.json"
SPARE_FILE = "spares.json"

# 模板缺“版本”时写入的默认值（任务卡：缺“版本”时写 "1"）
DEFAULT_TEMPLATE_VERSION = "1"
# 单条查询的返回上限：条款按标准整取时可能上千，故给足上限
CLAUSE_QUERY_LIMIT = 5000
# 一个区域名称在台账中的候选设备上限（用于判定重名）
DEVICE_QUERY_LIMIT = 50

# 依据条款匹配：条款号后缀（如“ a)”“（表N）”）归一化用
CLAUSE_SUFFIX_PATTERNS = (
    re.compile(r"\s*[（(][^）)]*[）)]\s*$"),  # 尾部括号注（表N）等
    re.compile(r"\s*[a-zA-Z]\s*[）)]\s*$"),  # 尾部分项标记 a)、B)
)

# 点标签 schema：(属性名, 参数名, 类型)；除主键 id 外都是 optional
_VERTEX_SCHEMAS: dict[str, tuple[tuple[str, str, str], ...]] = {
    LABEL_TEMPLATE: (
        ("id", "pid", "STRING"),
        ("名称", "name", "STRING"),
        ("作业类型", "task_type", "STRING"),
        ("设备类型", "device_type", "STRING"),
        ("作业对象", "work_object", "STRING"),
        ("停电要求", "outage_required", "STRING"),
        ("安全隔离措施", "isolation_measures", "STRING"),
        ("个人防护要求", "personal_protection", "STRING"),
        ("验电接地要求", "grounding_requirements", "STRING"),
        ("人员配置建议", "staffing", "STRING"),
        ("来源说明", "source_note", "STRING"),
        ("版本", "version", "STRING"),
        ("工序顺序", "step_sequence", "STRING"),
        ("操作要点", "key_points", "STRING"),
        ("技术参数要求", "tech_params", "STRING"),
        ("作业安全要求", "work_safety", "STRING"),
        ("工器具清单", "tools", "STRING"),
        ("材料清单", "materials", "STRING"),
        ("验收标准", "acceptance", "STRING"),
        ("注意事项", "notes", "STRING"),
        ("适用关键词", "keywords", "STRING"),
        ("依据条款", "clauses", "STRING"),
    ),
    LABEL_SAFETY_DISTANCE: (
        ("id", "sid", "STRING"),
        ("电压等级", "voltage_level", "STRING"),
        ("依据标准", "standard", "STRING"),
        ("条款号", "clause_no", "STRING"),
        ("适用场所", "scene", "STRING"),
        ("备注", "remark", "STRING"),
        ("设备不停电安全距离_m", "no_outage_m", "DOUBLE"),
        ("工作中正常活动范围安全距离_m", "work_range_m", "DOUBLE"),
        ("带电作业安全距离_m", "live_work_m", "DOUBLE"),
    ),
    LABEL_TEAM: (
        ("id", "tid", "STRING"),
        ("名称", "name", "STRING"),
        ("班组类型", "team_type", "STRING"),
        ("负责人", "leader", "STRING"),
        ("数据来源", "source_note", "STRING"),
        ("人数", "member_count", "INT64"),
        ("管辖区域", "areas", "STRING"),
    ),
    LABEL_SPARE: (
        ("id", "pid", "STRING"),
        ("名称", "name", "STRING"),
        ("型号规格", "model", "STRING"),
        ("存放地点", "location", "STRING"),
        ("数据来源", "source_note", "STRING"),
        ("库存数量", "quantity", "INT64"),
        ("适用设备类型", "device_types", "STRING"),
        ("适用作业对象", "work_objects", "STRING"),
    ),
}

# 边标签 schema：关系名 → (起点标签, 终点标签, 边属性 (属性名, 参数名, 类型) 序列)
_EDGE_SCHEMAS: dict[str, tuple[tuple[str, str], tuple[tuple[str, str, str], ...]]] = {
    REL_HAS_TEMPLATE: ((LABEL_DEVICE_TYPE, LABEL_TEMPLATE), ()),
    REL_TEMPLATE_CLAUSE: ((LABEL_TEMPLATE, LABEL_CLAUSE), (("用途", "usage", "STRING"),)),
    REL_MANAGES: ((LABEL_TEAM, LABEL_DEVICE), ()),
    REL_SPARE: ((LABEL_DEVICE_TYPE, LABEL_SPARE), ()),
}

# 以 JSON 字符串写入图的模板字段（TuGraph 强 schema 下统一声明为 STRING）
TEMPLATE_JSON_FIELDS = (
    "工序顺序",
    "操作要点",
    "技术参数要求",
    "作业安全要求",
    "工器具清单",
    "材料清单",
    "验收标准",
    "注意事项",
    "适用关键词",
    "依据条款",
)
# 直接按字符串写入的模板字段
TEMPLATE_TEXT_FIELDS = (
    "名称",
    "作业类型",
    "设备类型",
    "作业对象",
    "停电要求",
    "安全隔离措施",
    "个人防护要求",
    "验电接地要求",
    "人员配置建议",
    "来源说明",
)


class KgSeedError(Exception):
    """kg 处置方案 seed 数据或导入失败。"""


# ---------------------------------------------------------------------------
# 读取与校验
# ---------------------------------------------------------------------------
def load_plan_seed(data_dir: str | Path) -> dict[str, list[dict]]:
    """读取并校验六份 seed JSON，返回 ``{"templates","safety_distances","teams","spares"}``。

    ``templates`` 是三份作业模板文件的合并；每份文件必须是顶层数组、元素为对象且非空。
    """

    root = Path(data_dir)
    templates: list[dict] = []
    for filename in TEMPLATE_FILES:
        templates.extend(_read_json_list(root / filename, what=filename))
    if not templates:
        raise KgSeedError(f"{root} 下没有读到任何作业模板")
    return {
        "templates": templates,
        "safety_distances": _read_json_list(root / SAFETY_DISTANCE_FILE, what=SAFETY_DISTANCE_FILE),
        "teams": _read_json_list(root / TEAM_FILE, what=TEAM_FILE),
        "spares": _read_json_list(root / SPARE_FILE, what=SPARE_FILE),
    }


def _read_json_list(path: Path, *, what: str) -> list[dict]:
    if not path.is_file():
        raise KgSeedError(f"seed 文件不存在: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise KgSeedError(f"{what} 不是合法 JSON: {exc}") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise KgSeedError(f"{what} 顶层必须是对象数组")
    if not payload:
        raise KgSeedError(f"{what} 不能为空")
    return payload


# ---------------------------------------------------------------------------
# schema 注册
# ---------------------------------------------------------------------------
def _vertex_label_cypher(label: str, fields: tuple[tuple[str, str, str], ...]) -> str:
    """建点标签语句：主键 id 必填（optional=false），其余属性 optional=true。"""

    parts = [f"'{label}'", "'id'"]
    for name, _param, field_type in fields:
        optional = "false" if name == "id" else "true"
        parts.append(f"'{name}', '{field_type}', {optional}")
    return "CALL db.createVertexLabel(" + ", ".join(parts) + ")"


def _edge_label_cypher(
    label: str,
    endpoints: tuple[str, str],
    fields: tuple[tuple[str, str, str], ...],
) -> str:
    """建边标签语句：两端标签用 JSON 约束串给出，边属性 optional=true。"""

    source, target = endpoints
    constraints = f'[["{source}","{target}"]]'
    parts = [f"'{label}'", f"'{constraints}'"]
    for name, _param, field_type in fields:
        parts.append(f"'{name}', '{field_type}', true")
    return "CALL db.createEdgeLabel(" + ", ".join(parts) + ")"


def _ensure_kg_schema(driver) -> dict[str, int]:
    """幂等注册本模块所需的中文点/边标签；已注册的跳过，返回本次注册数量。"""

    created = {"vertex": 0, "edge": 0}
    with driver.session() as session:
        registered_vertex = {str(record.get("label")) for record in session.run("CALL db.vertexLabels()")}
        registered_edge = {str(record.get("label")) for record in session.run("CALL db.edgeLabels()")}
        for label, fields in _VERTEX_SCHEMAS.items():
            if label in registered_vertex:
                continue
            session.run(_vertex_label_cypher(label, fields)).consume()
            created["vertex"] += 1
        for label, (endpoints, fields) in _EDGE_SCHEMAS.items():
            if label in registered_edge:
                continue
            session.run(_edge_label_cypher(label, endpoints, fields)).consume()
            created["edge"] += 1
    return created


# ---------------------------------------------------------------------------
# 节点与边的写入
# ---------------------------------------------------------------------------
def _merge_node(driver, *, label: str, props: dict[str, Any]) -> None:
    """按主键 id ``MERGE`` 节点并 ``SET`` 其余属性；``props`` 里没有的（null）属性不 SET。"""

    schema = _VERTEX_SCHEMAS[label]
    primary_param = schema[0][1]
    set_parts: list[str] = []
    params: dict[str, Any] = {}
    for prop, param, _field_type in schema:
        if prop == "id":
            params[param] = props["id"]
            continue
        if prop not in props:
            continue
        set_parts.append(f"n.{prop} = ${param}")
        params[param] = props[prop]
    cypher = f"MERGE (n:{label} {{id: ${primary_param}}}) SET " + ", ".join(set_parts)
    with driver.session() as session:
        session.run(cypher, **params)


def _merge_edge(
    driver,
    *,
    rel: str,
    src_label: str,
    src_prop: str,
    src_param: str,
    src_value: Any,
    dst_label: str,
    dst_prop: str,
    dst_param: str,
    dst_value: Any,
    set_props: tuple[tuple[str, str, Any], ...] = (),
) -> bool:
    """两端各按单属性匹配后 ``MERGE`` 关系，并回读 ``count(r)`` 确认边确实存在。

    端点缺失时 ``MERGE`` 不写边也不报错，只靠调用方计数会把“没写”当成“写了”。
    返回 ``True`` 表示回读确认到这条边。
    """

    set_clause = "".join(f" SET r.{name} = ${param}" for name, param, _value in set_props)
    cypher = (
        f"MATCH (a:{src_label} {{{src_prop}: ${src_param}}}) "
        f"MATCH (b:{dst_label} {{{dst_prop}: ${dst_param}}}) "
        f"MERGE (a)-[r:{rel}]->(b){set_clause}"
    )
    params: dict[str, Any] = {src_param: src_value, dst_param: dst_value}
    for name, param, value in set_props:
        params[param] = value
    with driver.session() as session:
        session.run(cypher, **params)
        # 回读只带端点参数：不把边属性参数传给一条用不到它们的查询。
        readback = (
            f"MATCH (a:{src_label} {{{src_prop}: ${src_param}}})"
            f"-[r:{rel}]->"
            f"(b:{dst_label} {{{dst_prop}: ${dst_param}}}) "
            "RETURN count(r) AS total"
        )
        matched = session.run(readback, **{src_param: src_value, dst_param: dst_value}).single()
    return bool(matched and matched["total"])


def _template_props(template: dict[str, Any]) -> dict[str, Any]:
    """作业模板 JSON → 节点属性（文本原样、数组字段 json.dumps、版本缺省补 "1"）。"""

    props: dict[str, Any] = {"id": str(template["id"]).strip()}
    for field in TEMPLATE_TEXT_FIELDS:
        value = template.get(field)
        props[field] = "" if value is None else str(value).strip()
    version = template.get("版本")
    props["版本"] = str(version).strip() if version not in (None, "") else DEFAULT_TEMPLATE_VERSION
    for field in TEMPLATE_JSON_FIELDS:
        value = template.get(field)
        props[field] = json.dumps(value if value is not None else [], ensure_ascii=False)
    return props


def _safety_distance_props(item: dict[str, Any]) -> dict[str, Any]:
    """安全距离 JSON → 节点属性；三项距离为 null 时不写（由 _merge_node 跳过）。"""

    props: dict[str, Any] = {"id": str(item["id"]).strip()}
    for field in ("电压等级", "依据标准", "条款号", "适用场所", "备注"):
        value = item.get(field)
        props[field] = "" if value is None else str(value).strip()
    for field in ("设备不停电安全距离_m", "工作中正常活动范围安全距离_m", "带电作业安全距离_m"):
        value = item.get(field)
        if value is not None:
            props[field] = float(value)
    return props


def _team_props(team: dict[str, Any]) -> dict[str, Any]:
    """班组 JSON → 节点属性（管辖区域以 JSON 字符串存，人数 INT64）。"""

    props: dict[str, Any] = {"id": str(team["id"]).strip()}
    for field in ("名称", "班组类型", "负责人", "数据来源"):
        value = team.get(field)
        props[field] = "" if value is None else str(value).strip()
    count = team.get("人数")
    if count is not None:
        props["人数"] = int(count)
    props["管辖区域"] = json.dumps(team.get("管辖区域") or [], ensure_ascii=False)
    return props


def _spare_props(spare: dict[str, Any]) -> dict[str, Any]:
    """备品备件 JSON → 节点属性（两个适用清单以 JSON 字符串存，库存 INT64）。"""

    props: dict[str, Any] = {"id": str(spare["id"]).strip()}
    for field in ("名称", "型号规格", "存放地点", "数据来源"):
        value = spare.get(field)
        props[field] = "" if value is None else str(value).strip()
    quantity = spare.get("库存数量")
    if quantity is not None:
        props["库存数量"] = int(quantity)
    props["适用设备类型"] = json.dumps(spare.get("适用设备类型") or [], ensure_ascii=False)
    props["适用作业对象"] = json.dumps(spare.get("适用作业对象") or [], ensure_ascii=False)
    return props


# ---------------------------------------------------------------------------
# 依据条款与管辖区域的匹配
# ---------------------------------------------------------------------------
def _normalize_clause_no(value: str) -> str:
    """条款号归一化：去掉尾部的“ a)”“（表N）”等后缀。"""

    text = str(value or "").strip()
    changed = True
    while changed and text:
        changed = False
        for pattern in CLAUSE_SUFFIX_PATTERNS:
            stripped = pattern.sub("", text).strip()
            if stripped != text:
                text = stripped
                changed = True
    return text


def _query_clause_id_by_number(driver, *, standard_id: str, clause_no: str) -> str | None:
    """按 (standard_id, 条款号) 精确查一个标准条款节点的 id。"""

    with driver.session() as session:
        row = session.run(
            f"MATCH (n:{LABEL_CLAUSE}) WHERE coalesce(n.standard_id, '') = $sid "
            "AND coalesce(n.条款号, '') = $no RETURN n.id AS id LIMIT 1",
            sid=standard_id,
            no=clause_no,
        ).single()
    if row is None:
        return None
    clause_id = row.get("id")
    return str(clause_id) if clause_id else None


def _query_standard_clauses(driver, *, standard_id: str) -> list[dict[str, str]]:
    """取某标准下的全部条款 (id, 条款号)，供归一化匹配使用。"""

    with driver.session() as session:
        rows = session.run(
            f"MATCH (n:{LABEL_CLAUSE}) WHERE coalesce(n.standard_id, '') = $sid "
            "RETURN n.id AS id, n.条款号 AS no LIMIT $node_limit",
            sid=standard_id,
            node_limit=CLAUSE_QUERY_LIMIT,
        )
        return [{"id": str(row.get("id") or ""), "条款号": str(row.get("no") or "")} for row in rows]


def _match_clause_id(driver, *, standard_id: str, clause_no: str, cache: dict[str, list[dict]]) -> str | None:
    """匹配标准条款 id：先精确、再去尾部后缀、最后按标准整取条款在 Python 侧归一化匹配。"""

    raw = str(clause_no or "").strip()
    if not standard_id or not raw:
        return None
    exact = _query_clause_id_by_number(driver, standard_id=standard_id, clause_no=raw)
    if exact:
        return exact
    normalized = _normalize_clause_no(raw)
    if normalized and normalized != raw:
        exact = _query_clause_id_by_number(driver, standard_id=standard_id, clause_no=normalized)
        if exact:
            return exact
    clauses = cache.get(standard_id)
    if clauses is None:
        clauses = _query_standard_clauses(driver, standard_id=standard_id)
        cache[standard_id] = clauses
    for row in clauses:
        if row["条款号"] == raw or (normalized and _normalize_clause_no(row["条款号"]) == normalized):
            return row["id"] or None
    return None


def _query_devices_by_name(driver, *, name: str) -> list[dict[str, str]]:
    """按 设备实例.名称 查候选设备 (id, 层级路径)。"""

    with driver.session() as session:
        rows = session.run(
            f"MATCH (n:{LABEL_DEVICE}) WHERE coalesce(n.名称, '') = $name "
            "RETURN n.id AS id, n.层级路径 AS path LIMIT $node_limit",
            name=name,
            node_limit=DEVICE_QUERY_LIMIT,
        )
        return [{"id": str(row.get("id") or ""), "path": str(row.get("path") or "")} for row in rows]


def _resolve_area_device(driver, *, name: str, path: str | None) -> tuple[str | None, str | None]:
    """把区域名称解析成唯一设备实例 id；重名且无法用路径区分时返回未匹配原因，不静默任选。"""

    rows = _query_devices_by_name(driver, name=name)
    if len(rows) == 1:
        return (rows[0]["id"] or None), None
    if not rows:
        return None, f"区域“{name}”未在设备台账中找到"
    if path:
        matched = [row for row in rows if row["path"] == path]
        if len(matched) == 1:
            return (matched[0]["id"] or None), None
        return None, f"区域“{name}”在台账中有 {len(rows)} 个重名设备，管辖区域路径“{path}”未能唯一匹配"
    return None, f"区域“{name}”在台账中有 {len(rows)} 个重名设备，且无管辖区域路径可区分"


def _area_path_of(team: dict[str, Any], area: str) -> str | None:
    """取区域名称对应的层级路径（``管辖区域路径`` 支持 {名称: 路径} 映射）。"""

    mapping = team.get("管辖区域路径")
    if isinstance(mapping, dict):
        value = mapping.get(area)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# ---------------------------------------------------------------------------
# 语句计数（dry-run 用，纯离线）
# ---------------------------------------------------------------------------
def _planned_statements(seed: dict[str, list[dict]]) -> dict[str, Any]:
    """按 seed 数据估算将执行的语句数（不连图，边按计划上限计数）。"""

    templates = seed["templates"]
    teams = seed["teams"]
    spares = seed["spares"]
    edges = {
        REL_HAS_TEMPLATE: len(templates),
        REL_TEMPLATE_CLAUSE: sum(len(t.get("依据条款") or []) for t in templates),
        REL_MANAGES: sum(len(t.get("管辖区域") or []) for t in teams),
        REL_SPARE: sum(len(s.get("适用设备类型") or []) for s in spares),
    }
    nodes = len(templates) + len(seed["safety_distances"]) + len(teams) + len(spares)
    edge_total = sum(edges.values())
    return {
        "vertex_schema": len(_VERTEX_SCHEMAS),
        "edge_schema": len(_EDGE_SCHEMAS),
        "nodes": nodes,
        "edges": edge_total,
        "total": nodes + edge_total,
        "edges_by_relation": edges,
    }


# ---------------------------------------------------------------------------
# driver 创建（dry-run 不调用）
# ---------------------------------------------------------------------------
def _open_driver(*, uri: str, graph: str, username: str, password: str):
    """创建 TuGraph Bolt driver——本模块唯一创建 driver 的位置，dry-run 不会走到这里。"""

    return GraphDatabase.driver(uri, auth=(username, password), database=graph)


# ---------------------------------------------------------------------------
# 主写入流程
# ---------------------------------------------------------------------------
def seed_plan_graph_kg(
    *,
    uri: str,
    graph: str,
    username: str,
    password: str,
    seed: dict[str, list[dict]],
    dry_run: bool = False,
    kg_ref: dict | None = None,
) -> dict[str, Any]:
    """把 seed 幂等写入外部 kg3 图，返回节点/关系计数与未匹配清单。

    ``dry_run=True`` 时不创建 driver、不连接图，只输出将执行的语句数与离线预检结果。
    预检由 CLI 在连图前执行；直接调用本函数不会重跑预检，调用方必须自行保证前置条件。
    """

    if dry_run:
        planned = _planned_statements(seed)
        report = preflight_seed(seed, kg_ref=kg_ref)
        return {
            "status": "dry_run",
            "templates": len(seed["templates"]),
            "safety_distances": len(seed["safety_distances"]),
            "teams": len(seed["teams"]),
            "spares": len(seed["spares"]),
            "edges": dict(planned["edges_by_relation"]),
            "edges_verified": {rel: 0 for rel in planned["edges_by_relation"]},
            "unmatched_clauses": [],
            "unmatched_areas": [],
            "unmatched_edges": [],
            "statements": planned,
            "preflight": report,
        }

    templates = seed["templates"]
    teams = seed["teams"]
    spares = seed["spares"]
    # ``edges`` 是发出的 MERGE 意图数；``edges_verified`` 是写完后回读 ``count(r)`` 确认的条数。
    # 两者不一致说明端点不存在（例如“设备类型”节点由主知识库维护、本模块不创建）。
    edges = {REL_HAS_TEMPLATE: 0, REL_TEMPLATE_CLAUSE: 0, REL_MANAGES: 0, REL_SPARE: 0}
    edges_verified = {REL_HAS_TEMPLATE: 0, REL_TEMPLATE_CLAUSE: 0, REL_MANAGES: 0, REL_SPARE: 0}
    unmatched_edges: list[dict[str, Any]] = []
    unmatched_clauses: list[dict[str, Any]] = []
    unmatched_areas: list[dict[str, Any]] = []
    clause_cache: dict[str, list[dict]] = {}

    def record_edge(rel: str, *, written: bool, source: Any, target: Any) -> None:
        edges[rel] += 1
        if written:
            edges_verified[rel] += 1
            return
        unmatched_edges.append({"relation": rel, "source": str(source), "target": str(target)})

    driver = _open_driver(uri=uri, graph=graph, username=username, password=password)
    try:
        schema = _ensure_kg_schema(driver)

        # 1) 节点
        for template in templates:
            _merge_node(driver, label=LABEL_TEMPLATE, props=_template_props(template))
        for item in seed["safety_distances"]:
            _merge_node(driver, label=LABEL_SAFETY_DISTANCE, props=_safety_distance_props(item))
        for team in teams:
            _merge_node(driver, label=LABEL_TEAM, props=_team_props(team))
        for spare in spares:
            _merge_node(driver, label=LABEL_SPARE, props=_spare_props(spare))

        # 2) 适用模板：设备类型 → 作业模板
        for template in templates:
            template_id = str(template["id"]).strip()
            device_type = str(template.get("设备类型") or "").strip()
            if not device_type:
                continue
            written = _merge_edge(
                driver,
                rel=REL_HAS_TEMPLATE,
                src_label=LABEL_DEVICE_TYPE,
                src_prop="名称",
                src_param="device_type",
                src_value=device_type,
                dst_label=LABEL_TEMPLATE,
                dst_prop="id",
                dst_param="pid",
                dst_value=template_id,
            )
            record_edge(REL_HAS_TEMPLATE, written=written, source=device_type, target=template_id)

        # 3) 依据条款：作业模板 → 标准条款（按 standard_id + 条款号 匹配，未匹配记入清单）
        for template in templates:
            template_id = str(template["id"]).strip()
            for clause in template.get("依据条款") or []:
                standard_id = str(clause.get("standard_id") or "").strip()
                clause_no = str(clause.get("条款号") or "").strip()
                clause_id = _match_clause_id(driver, standard_id=standard_id, clause_no=clause_no, cache=clause_cache)
                if not clause_id:
                    unmatched_clauses.append({"template": template_id, "standard_id": standard_id, "条款号": clause_no})
                    continue
                written = _merge_edge(
                    driver,
                    rel=REL_TEMPLATE_CLAUSE,
                    src_label=LABEL_TEMPLATE,
                    src_prop="id",
                    src_param="tid",
                    src_value=template_id,
                    dst_label=LABEL_CLAUSE,
                    dst_prop="id",
                    dst_param="cid",
                    dst_value=clause_id,
                    set_props=(("用途", "usage", str(clause.get("用途") or "").strip()),),
                )
                record_edge(REL_TEMPLATE_CLAUSE, written=written, source=template_id, target=clause_id)

        # 4) 管辖：班组 → 设备实例（重名且无法区分时不静默任选）
        for team in teams:
            team_id = str(team["id"]).strip()
            for area in team.get("管辖区域") or []:
                name = str(area or "").strip()
                if not name:
                    continue
                device_id, reason = _resolve_area_device(driver, name=name, path=_area_path_of(team, name))
                if not device_id:
                    unmatched_areas.append({"team": team_id, "area": name, "reason": reason or "未匹配"})
                    continue
                written = _merge_edge(
                    driver,
                    rel=REL_MANAGES,
                    src_label=LABEL_TEAM,
                    src_prop="id",
                    src_param="tid",
                    src_value=team_id,
                    dst_label=LABEL_DEVICE,
                    dst_prop="id",
                    dst_param="did",
                    dst_value=device_id,
                )
                record_edge(REL_MANAGES, written=written, source=team_id, target=device_id)

        # 5) 配备备件：设备类型 → 备品备件（适用设备类型每项一条边）
        for spare in spares:
            spare_id = str(spare["id"]).strip()
            for device_type in spare.get("适用设备类型") or []:
                name = str(device_type or "").strip()
                if not name:
                    continue
                written = _merge_edge(
                    driver,
                    rel=REL_SPARE,
                    src_label=LABEL_DEVICE_TYPE,
                    src_prop="名称",
                    src_param="device_type",
                    src_value=name,
                    dst_label=LABEL_SPARE,
                    dst_prop="id",
                    dst_param="pid",
                    dst_value=spare_id,
                )
                record_edge(REL_SPARE, written=written, source=name, target=spare_id)
    finally:
        driver.close()

    logger.info(
        f"处置方案 kg seed 写入 uri={uri} graph={graph} "
        f"templates={len(templates)} teams={len(teams)} spares={len(spares)} edges={edges}"
    )
    return {
        "status": "success",
        "templates": len(templates),
        "safety_distances": len(seed["safety_distances"]),
        "teams": len(teams),
        "spares": len(spares),
        "edges": edges,
        "edges_verified": edges_verified,
        "unmatched_clauses": unmatched_clauses,
        "unmatched_areas": unmatched_areas,
        "unmatched_edges": unmatched_edges,
        "schema": schema,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导入处置方案知识数据到 TuGraph kg3 图")
    parser.add_argument("--uri", required=True, help="TuGraph Bolt 地址，如 bolt://192.168.138.10:7688")
    parser.add_argument("--graph", default="kg3", help="图名称，默认 kg3")
    parser.add_argument("--username", default="admin", help="用户名，默认 admin")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="seed 数据目录，默认包内 data/")
    parser.add_argument(
        "--kg-ref",
        default="",
        help="ref/kg_ref.json 路径；缺少时班组管辖区域检查按未通过处理，预检会拦截导入",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只做离线预检与语句计数，不连接图、不创建 driver",
    )
    args = parser.parse_args()

    try:
        seed = load_plan_seed(args.data_dir)
    except KgSeedError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1

    # 写入前的前置条件先校验：密码决定能否真实连接，kg_ref 决定管辖区域能否被证明。
    password = os.environ.get(PASSWORD_ENV, "")
    if not args.dry_run and not password:
        print(f"导入失败：未设置环境变量 {PASSWORD_ENV}（不提供默认密码）", file=sys.stderr)
        return 1

    kg_ref: dict | None = None
    if not args.kg_ref:
        print("导入失败：缺少 --kg-ref（ref/kg_ref.json），无法校验班组管辖区域", file=sys.stderr)
        return 1
    ref_path = Path(args.kg_ref)
    if not ref_path.is_file():
        print(f"导入失败：kg_ref 文件不存在: {ref_path}", file=sys.stderr)
        return 1
    with ref_path.open(encoding="utf-8") as handle:
        kg_ref = json.load(handle)

    # 连图前先离线预检；预检不过直接拦截，避免写入坏数据
    report = preflight_seed(seed, kg_ref=kg_ref)
    if not report["ok"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("离线预检未通过，已终止导入。", file=sys.stderr)
        return 2

    try:
        result = seed_plan_graph_kg(
            uri=args.uri,
            graph=args.graph,
            username=args.username,
            password=password,
            seed=seed,
            dry_run=args.dry_run,
            kg_ref=kg_ref,
        )
    except (KgSeedError, PreflightError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"导入异常：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
