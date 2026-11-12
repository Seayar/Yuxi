"""kg4 图检索：定位设备实例与检修作业场景，并收集场景下的四类证据。

本模块只做“读图 + 打分 + 过滤”，不做任何自然语言生成。``search_devices`` 在 ``设备实例``
中按名称/层级路径召回，并用 ``属于`` 边补全设备类型；``find_scenes`` 读取
``设备类型 -[包含作业]-> 检修作业场景``；``collect_scene_evidence`` 收集步骤、安全措施、
资源需求、人员需求与责任班组，供装配模块确定性实例化。

TuGraph 4.5.2 实测限制：一条语句最多一个 MATCH，多跳必须拆成多条语句在 Python 侧拼接；
中文标签/关系/属性直接写裸标识符，不加反引号；所有查询都带 LIMIT。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from yuxi.agents.toolkits.tugraph import TuGraphClient, TuGraphError
from yuxi.knowledge.maintenance_plan.contracts import (
    BRANCH_VALUES,
    COMMON_YES,
    DEVICE_NAME_PROPS,
    LABEL_DEVICE,
    LABEL_DEVICE_TYPE,
    LABEL_ORG,
    LABEL_PERSONNEL,
    LABEL_RESOURCE,
    LABEL_SAFETY,
    LABEL_SCENE,
    LABEL_STEP,
    MAX_DEVICE_CANDIDATES,
    REL_BELONGS,
    REL_HAS_SCENE,
    REL_HAS_STEP,
    REL_LOCATED_IN,
    REL_MANAGES,
    REL_NEEDS_PERSONNEL,
    REL_NEEDS_RESOURCE,
    REL_NEEDS_SAFETY,
    BranchSelection,
    DeviceCandidate,
    PersonnelRequirement,
    PlanQuery,
    ResourceRequirement,
    SafetyMeasure,
    Scenario,
    ScenarioEvidence,
    Step,
    TeamInfo,
)

# 单条语句的召回上限
KG_QUERY_LIMIT = 200

# 层级路径分隔符（台账写法：“变电设施 > 35kV#1变电站 > … > 设备”）
PATH_SEPARATOR = " > "

# 打分权重
NAME_EXACT_SCORE = 100.0
NAME_CONTAINS_SCORE = 60.0
NAME_CONTAINED_SCORE = 40.0
PATH_CONTAINS_SCORE = 30.0
LOCATION_HIT_SCORE = 30.0
DEVICE_TYPE_MATCH_SCORE = 20.0
VOLTAGE_MATCH_SCORE = 10.0

VOLTAGE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*[kK][vV]")

# 少数台账线路节点没有 属于 边；只按台账分类精确回退，避免把“线路开关间隔”误判成架空线路。
DEVICE_TYPE_FALLBACKS = {
    "中压线路": "架空线路",
    "配电线路": "架空线路",
    "输电线路": "架空线路",
    "母联/分段开关间隔": "断路器",
    "电压互感器间隔": "电压互感器",
}

# 口语 → 台账名称的同义展开
ABBREVIATION_EXPANSIONS = {
    "主变": "主变压器",
    "站用变": "站用变压器",
    "接地变": "接地变压器",
    "所用变": "所用变压器",
}
ORDINAL_PATTERN = re.compile(r"(?P<num>\d+)\s*号")


# ---------------------------------------------------------------------------
# 设备定位
# ---------------------------------------------------------------------------
def search_devices(
    client: TuGraphClient, query: PlanQuery, *, limit: int = MAX_DEVICE_CANDIDATES
) -> list[DeviceCandidate]:
    """按设备名称/位置线索/设备类型在 ``设备实例`` 中召回，并用 ``属于`` 边补全类型。

    - 关键词来自 ``device_name``（含“1号→#1/1#”的同义展开）与 ``location_hint``；
    - 只保留有 ``属于`` 设备类型边的设备（kg4 中区域/监测节点没有类型边）；
    - 候选不唯一时由调用方发中断让用户选择。
    """

    keywords = _device_keywords(query)
    location_hint = _clean(query.location_hint)

    records: list[dict[str, Any]] = []
    for keyword in [*keywords, location_hint]:
        if keyword:
            records.extend(_query_devices_by_keyword(client, keyword=keyword))
    if not keywords and not location_hint and query.device_type:
        records.extend(_query_devices_by_device_type(client, device_type=query.device_type))

    candidates: dict[str, DeviceCandidate] = {}
    for record in records:
        props = record.get("n") or record.get("a") or {}
        candidate = _build_candidate(props, query=query, keywords=keywords)
        if not candidate.device_id or candidate.score <= 0:
            continue
        current = candidates.get(candidate.device_id)
        if current is None or candidate.score > current.score:
            candidates[candidate.device_id] = candidate

    ordered = sorted(candidates.values(), key=lambda item: (-item.score, item.name, item.device_id))
    top = ordered[: max(limit * 4, limit)]
    types = _query_device_types(client, device_ids=[item.device_id for item in top])

    typed: list[DeviceCandidate] = []
    for candidate in top:
        device_type = types.get(candidate.device_id) or DEVICE_TYPE_FALLBACKS.get(candidate.ledger_class or "")
        if not device_type:
            continue
        score = candidate.score
        if query.device_type and device_type == query.device_type:
            score += DEVICE_TYPE_MATCH_SCORE
        typed.append(replace(candidate, device_type=device_type, score=score))

    typed.sort(key=lambda item: (-item.score, item.name, item.device_id))
    return typed[:limit]


def _build_candidate(props: dict[str, Any], *, query: PlanQuery, keywords: list[str]) -> DeviceCandidate:
    """台账节点属性 → DeviceCandidate；设备类型由后续 ``属于`` 查询补全。"""

    name = _device_name(props)
    path = str(props.get("层级路径") or "")
    score = _name_score(name, path, keywords)
    if not keywords and query.device_type:
        score = 1.0
    location_hint = _clean(query.location_hint)
    if location_hint and location_hint in path:
        score += LOCATION_HIT_SCORE

    voltage_level = _clean(props.get("电压等级"))
    if query.voltage_level and voltage_level == query.voltage_level:
        score += VOLTAGE_MATCH_SCORE

    return DeviceCandidate(
        device_id=_clean(props.get("id")) or name,
        name=name,
        device_type=None,
        voltage_level=voltage_level,
        path=path,
        ledger_class=_clean(props.get("台账分类")) or _clean(props.get("设备分类")),
        ledger_ids=_parse_ledger_ids(props.get("台账ID列表")),
        score=float(score),
    )


def _device_keywords(query: PlanQuery) -> list[str]:
    """设备名称关键词：原话指称 + 同义展开（“1号主变”→“#1主变”“#1主变压器”）。"""

    base = _clean(query.device_name)
    if base is None:
        return []
    return _dedup([base, *_synonym_variants(base)])


def _synonym_variants(name: str) -> list[str]:
    """口语指称的同义写法：1号→#1/1#，主变→主变压器。"""

    variants: list[str] = []
    for match in ORDINAL_PATTERN.finditer(name):
        number = match.group("num")
        for replacement in (f"#{number}", f"{number}#"):
            variants.append(name[: match.start()] + replacement + name[match.end() :])
    variants.extend(_expansion_variants(name))
    for variant in list(variants):
        variants.extend(_expansion_variants(variant))
    return variants


def _expansion_variants(name: str) -> list[str]:
    """把口语简称展开成台账全称，已经写全的不再展开。"""

    return [
        name.replace(short, full)
        for short, full in ABBREVIATION_EXPANSIONS.items()
        if short in name and full not in name
    ]


def _name_score(name: str, path: str, keywords: list[str]) -> float:
    """名称打分：精确相等 > 名称包含 > 名称被包含 > 仅层级路径包含。"""

    best = 0.0
    for keyword in keywords:
        if name == keyword:
            best = max(best, NAME_EXACT_SCORE)
        elif keyword in name:
            best = max(best, NAME_CONTAINS_SCORE)
        elif name and name in keyword:
            best = max(best, NAME_CONTAINED_SCORE)
    if best == 0 and any(keyword in path for keyword in keywords):
        best = PATH_CONTAINS_SCORE
    return best


def _device_name(props: dict[str, Any]) -> str:
    """取设备名称：按 contracts 的名称属性优先级，全空时退回 id。"""

    for prop in DEVICE_NAME_PROPS:
        value = _clean(props.get(prop))
        if value:
            return value
    return _clean(props.get("id")) or ""


def _parse_ledger_ids(value: Any) -> tuple[str, ...]:
    """``台账ID列表`` → 台账 ID 元组；分隔符兼容顿号/逗号/分号/空白。"""

    text = _clean(value)
    if text is None:
        return ()
    return tuple(_dedup(part.strip() for part in re.split(r"[、,，;；\s]+", text)))


# ---------------------------------------------------------------------------
# 检修作业场景
# ---------------------------------------------------------------------------
def find_scenes(client: TuGraphClient, device: DeviceCandidate) -> list[Scenario]:
    """读取 ``设备类型 -[包含作业]-> 检修作业场景``；设备无类型时返回空列表。"""

    if not device.device_type:
        return []
    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND labels(b)[0] = $dst "
        "AND type(r) = $rel AND coalesce(a.名称, '') = $device_type "
        "RETURN b LIMIT $node_limit",
        src=LABEL_DEVICE_TYPE,
        dst=LABEL_SCENE,
        rel=REL_HAS_SCENE,
        device_type=device.device_type,
        node_limit=KG_QUERY_LIMIT,
    )
    scenes = [_parse_scenario(record.get("b") or {}, device_type=device.device_type) for record in records]
    return [scene for scene in scenes if scene.scene_id]


def rank_scenes(scenes: list[Scenario], query: PlanQuery) -> list[Scenario]:
    """按用户原文与场景名称/说明的二元组重叠打分，返回降序列表。

    用户明确给出 A/B/C/D 级时先按 ``适用检修等级`` 过滤；过滤后为空则退回全部场景。
    """

    candidates = list(scenes)
    if query.maintenance_level:
        wanted = str(query.maintenance_level).strip().upper().rstrip("级")
        filtered = [scene for scene in candidates if str(scene.level).strip().upper().rstrip("级") == wanted]
        if filtered:
            candidates = filtered

    text = " ".join(part for part in (query.raw_text, query.scene_hint, query.work_object, query.task_type) if part)
    ranked = [replace(scene, score=round(_scene_score(text, scene), 2)) for scene in candidates]
    ranked.sort(key=lambda item: (-item.score, item.name, item.scene_id))
    return ranked


def _scene_score(text: str, scene: Scenario) -> float:
    """场景打分：场景名被原文包含 +100；名称二元组权重 2、说明权重 1。"""

    if not text:
        return 0.0
    normalized = _normalize_text(text)
    name = _normalize_text(scene.name)
    score = 0.0
    if name and name in normalized:
        score += 100.0
    query_bigrams = _bigrams(normalized)
    score += 2.0 * len(query_bigrams & _bigrams(name))
    score += 1.0 * len(query_bigrams & _bigrams(_normalize_text(scene.description)))
    return score


def _normalize_text(text: str) -> str:
    """去掉空白和标点，保留中文/字母/数字，供二元组比较。"""

    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or "")).casefold()


def _bigrams(text: str) -> set[str]:
    """字符二元组集合；长度小于 2 时返回空集。"""

    return {text[index : index + 2] for index in range(max(len(text) - 1, 0))}


# ---------------------------------------------------------------------------
# 证据收集
# ---------------------------------------------------------------------------
def collect_scene_evidence(
    client: TuGraphClient,
    query: PlanQuery,
    device: DeviceCandidate,
    scenario: Scenario,
    branch: BranchSelection | None,
) -> ScenarioEvidence:
    """收集场景下的步骤、安全措施、资源需求、人员需求与责任班组。

    过滤规则：``是否通用=是`` 或 ``适用分支值=当前分支``；其他组合只记 warning，不纳入。
    步骤与分支无关，按 ``步骤序号`` 排序。
    """

    evidence = ScenarioEvidence(device=device, scenario=scenario, branch=branch)
    log = evidence.retrieval_log

    parents = _query_parents(client, device=device)
    ancestors = _ancestors_from_path(device.path)
    for props in reversed(parents):
        name = _device_name(props)
        if name and name not in ancestors:
            ancestors.insert(0, name)

    voltage_level = _clean(device.voltage_level) or _resolve_voltage_level(parents, ancestors)
    if voltage_level and not device.voltage_level:
        evidence.device = replace(device, voltage_level=voltage_level)
        log.append(f"设备台账电压等级为空，按上级节点解析出电压等级“{voltage_level}”")

    log.append(
        f"按设备类型“{device.device_type}”检索到检修作业场景“{scenario.name}”"
        f"（{scenario.level or '未标注等级'}，分支维度：{scenario.branch_dimension or '无'}）"
    )
    if branch is not None:
        log.append(
            f"分支判定：{branch.dimension} = {branch.condition}（{branch.branch}，来源：{_branch_source_text(branch)}）"
        )
    else:
        log.append("该场景没有分支判定维度，仅输出通用内容")

    steps = [
        _parse_step(record.get("b") or {})
        for record in _query_children(client, scenario.scene_id, REL_HAS_STEP, LABEL_STEP)
    ]
    steps = [step for step in steps if step.node_id]
    steps.sort(key=lambda item: (item.sequence, item.node_id))
    evidence.steps = steps
    log.append(f"检修步骤共 {len(steps)} 道工序（与分支无关）")

    safety = [
        _parse_safety(record.get("b") or {})
        for record in _query_children(client, scenario.scene_id, REL_NEEDS_SAFETY, LABEL_SAFETY)
    ]
    safety = _select_items(safety, branch, evidence, kind="安全措施")
    safety.sort(key=lambda item: item.node_id)
    evidence.safety = safety
    common_safety = sum(1 for item in safety if item.common)
    log.append(f"安全措施：通用 {common_safety} 条，当前分支 {len(safety) - common_safety} 条")

    resources = [
        _parse_resource(record.get("b") or {})
        for record in _query_children(client, scenario.scene_id, REL_NEEDS_RESOURCE, LABEL_RESOURCE)
    ]
    resources = _select_items(resources, branch, evidence, kind="资源需求")
    resources.sort(key=lambda item: item.node_id)
    evidence.resources = resources
    common_resources = sum(1 for item in resources if item.common)
    log.append(f"资源需求：通用 {common_resources} 项，当前分支 {len(resources) - common_resources} 项")

    personnel = [
        _parse_personnel(record.get("b") or {})
        for record in _query_children(client, scenario.scene_id, REL_NEEDS_PERSONNEL, LABEL_PERSONNEL)
    ]
    personnel = _select_items(personnel, branch, evidence, kind="人员需求")
    personnel.sort(key=lambda item: item.node_id)
    evidence.personnel = personnel
    total = sum(item.count for item in personnel)
    common_count = sum(item.count for item in personnel if item.common)
    log.append(f"人员需求：通用 {common_count} 人，当前分支 {total - common_count} 人，合计 {total} 人")

    evidence.team = _find_team(client, ancestors=ancestors)
    if evidence.team is None:
        log.append("未检索到该设备或上级区域的管辖班组")
    else:
        log.append(f"沿层级向上在“{evidence.team.managed_area}”找到责任班组“{evidence.team.name}”")

    return evidence


def _select_items(
    items: list[Any], branch: BranchSelection | None, evidence: ScenarioEvidence, *, kind: str
) -> list[Any]:
    """按 是否通用/适用分支值 过滤；非法标记只记 warning。"""

    selected: list[Any] = []
    for item in items:
        common = bool(getattr(item, "common", False))
        applicable = str(getattr(item, "applicable_branch", "") or "").strip()
        if common:
            selected.append(item)
            continue
        if applicable not in BRANCH_VALUES:
            evidence.warnings.append(
                f"{kind}“{getattr(item, 'name', getattr(item, 'role', ''))}”的适用分支值非法：{applicable or '空'}"
            )
            continue
        if branch is not None and applicable == branch.branch:
            selected.append(item)
    return selected


def _branch_source_text(branch: BranchSelection) -> str:
    if branch.source == "user":
        return "用户确认"
    return "Redis"


# ---------------------------------------------------------------------------
# 查询语句（一条语句一个 MATCH；中文标签/属性不加反引号；用户输入只走参数）
# ---------------------------------------------------------------------------
def _in_condition(alias: str, prop: str, param: str) -> str:
    """生成 ``coalesce(alias.属性, '') IN $参数`` 条件片段。"""

    return f"coalesce({alias}.{prop}, '') IN ${param}"


def _query_devices_by_keyword(client: TuGraphClient, *, keyword: str) -> list[dict[str, Any]]:
    """按关键词在 名称/显示名/name 与 层级路径 上召回设备实例。"""

    props = (*DEVICE_NAME_PROPS, "层级路径")
    conditions = " OR ".join(f"coalesce(n.{prop}, '') CONTAINS $kw" for prop in props)
    return client.run(
        f"MATCH (n) WHERE labels(n)[0] = $label AND ({conditions}) RETURN n LIMIT $node_limit",
        label=LABEL_DEVICE,
        kw=keyword,
        node_limit=KG_QUERY_LIMIT,
    )


def _query_devices_by_device_type(client: TuGraphClient, *, device_type: str) -> list[dict[str, Any]]:
    """按 属于 关系召回某设备类型下的设备实例（用户只说了设备类型时用）。"""

    return client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel "
        "AND labels(b)[0] = $dst AND coalesce(b.名称, '') = $device_type "
        "RETURN a LIMIT $node_limit",
        src=LABEL_DEVICE,
        rel=REL_BELONGS,
        dst=LABEL_DEVICE_TYPE,
        device_type=device_type,
        node_limit=KG_QUERY_LIMIT,
    )


def _query_device_types(client: TuGraphClient, *, device_ids: list[str]) -> dict[str, str]:
    """设备 id → 设备类型名称（来自 属于 边）。"""

    ids = _dedup(device_ids)
    if not ids:
        return {}
    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND labels(b)[0] = $dst "
        "AND type(r) = $rel AND " + _in_condition("a", "id", "device_ids") + " "
        "RETURN a, b LIMIT $node_limit",
        src=LABEL_DEVICE,
        dst=LABEL_DEVICE_TYPE,
        rel=REL_BELONGS,
        device_ids=ids,
        node_limit=KG_QUERY_LIMIT,
    )
    types: dict[str, str] = {}
    for record in records:
        device_id = _clean((record.get("a") or {}).get("id"))
        device_type = _clean((record.get("b") or {}).get("名称"))
        if device_id and device_type:
            types[device_id] = device_type
    return types


def _query_parents(client: TuGraphClient, *, device: DeviceCandidate) -> list[dict[str, Any]]:
    """设备实例 -[位于]-> 父节点属性（补全层级路径与电压等级）。"""

    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND labels(b)[0] = $dst "
        "AND type(r) = $rel AND coalesce(a.id, '') = $device_id "
        "RETURN b LIMIT $node_limit",
        src=LABEL_DEVICE,
        dst=LABEL_DEVICE,
        rel=REL_LOCATED_IN,
        device_id=device.device_id,
        node_limit=KG_QUERY_LIMIT,
    )
    return [record.get("b") or {} for record in records]


def _query_children(client: TuGraphClient, scene_id: str, relation: str, target_label: str) -> list[dict[str, Any]]:
    """检修作业场景 -[关系]-> 子节点。"""

    return client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel "
        "AND labels(b)[0] = $dst AND coalesce(a.id, '') = $scene_id "
        "RETURN b LIMIT $node_limit",
        src=LABEL_SCENE,
        rel=relation,
        dst=target_label,
        scene_id=scene_id,
        node_limit=KG_QUERY_LIMIT,
    )


def _find_team(client: TuGraphClient, *, ancestors: list[str]) -> TeamInfo | None:
    """沿 ancestors 由近到远找第一个被班组管辖的区域节点。"""

    if not ancestors:
        return None
    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND labels(b)[0] = $dst "
        "AND type(r) = $rel AND coalesce(a.组织类型, '') = '班组' "
        "AND " + _in_condition("b", "名称", "area_names") + " "
        "RETURN a, b LIMIT $node_limit",
        src=LABEL_ORG,
        dst=LABEL_DEVICE,
        rel=REL_MANAGES,
        area_names=ancestors,
        node_limit=KG_QUERY_LIMIT,
    )
    teams_by_area: dict[str, TeamInfo] = {}
    for record in records:
        area = _device_name(record.get("b") or {})
        org = record.get("a") or {}
        if area and area not in teams_by_area:
            teams_by_area[area] = TeamInfo(
                name=_clean(org.get("名称")) or "",
                managed_area=area,
                source_note=_clean(org.get("来源")) or "",
            )
    for area in ancestors:
        if area in teams_by_area:
            return teams_by_area[area]
    return None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _parse_scenario(props: dict[str, Any], *, device_type: str) -> Scenario:
    return Scenario(
        scene_id=_clean(props.get("id")) or "",
        name=_clean(props.get("名称")) or "",
        description=_clean(props.get("说明")) or "",
        level=_clean(props.get("适用检修等级")) or "",
        branch_dimension=_clean(props.get("分支判定维度")) or "",
        branch1_condition=_clean(props.get("分支1条件")) or "",
        branch2_condition=_clean(props.get("分支2条件")) or "",
        device_type=device_type,
    )


def _parse_step(props: dict[str, Any]) -> Step:
    return Step(
        node_id=_clean(props.get("id")) or "",
        sequence=_as_int(props.get("步骤序号")) or 0,
        content=_clean(props.get("操作内容")) or "",
        name=_clean(props.get("名称")) or "",
    )


def _parse_safety(props: dict[str, Any]) -> SafetyMeasure:
    return SafetyMeasure(
        node_id=_clean(props.get("id")) or "",
        name=_clean(props.get("名称")) or "",
        content=_clean(props.get("具体内容")) or "",
        common=_clean(props.get("是否通用")) == COMMON_YES,
        applicable_branch=_clean(props.get("适用分支值")) or "",
        category=_clean(props.get("措施类别")) or "",
    )


def _parse_resource(props: dict[str, Any]) -> ResourceRequirement:
    return ResourceRequirement(
        node_id=_clean(props.get("id")) or "",
        name=_clean(props.get("名称")) or "",
        quantity=_as_int(props.get("数量")),
        unit=_clean(props.get("计量单位")) or "",
        quantity_requirement=_clean(props.get("数量要求")) or "",
        common=_clean(props.get("是否通用")) == COMMON_YES,
        applicable_branch=_clean(props.get("适用分支值")) or "",
    )


def _parse_personnel(props: dict[str, Any]) -> PersonnelRequirement:
    return PersonnelRequirement(
        node_id=_clean(props.get("id")) or "",
        role=_clean(props.get("岗位角色")) or "",
        count=_as_int(props.get("人数")) or 0,
        common=_clean(props.get("是否通用")) == COMMON_YES,
        applicable_branch=_clean(props.get("适用分支值")) or "",
        duty=_clean(props.get("职责约束")) or "",
        qualification=_clean(props.get("资质要求")) or "",
    )


# ---------------------------------------------------------------------------
# 层级与电压
# ---------------------------------------------------------------------------
def _ancestors_from_path(path: str) -> list[str]:
    """层级路径 → 上级节点名称，从近到远（去掉设备自身）。"""

    parts = [part.strip() for part in (path or "").split(PATH_SEPARATOR) if part.strip()]
    return list(reversed(parts[:-1])) if parts else []


def _resolve_voltage_level(parents: list[dict[str, Any]], ancestors: list[str]) -> str | None:
    """按 ancestors 由近到远解析电压等级；节点属性优先于名称里的 NkV。"""

    voltage_by_name: dict[str, str] = {}
    for props in parents:
        name = _device_name(props)
        voltage = _clean(props.get("电压等级"))
        if name and voltage:
            voltage_by_name.setdefault(name, voltage)

    ordered = list(ancestors)
    ordered.extend(_dedup(_device_name(props) for props in parents if _device_name(props) not in ancestors))

    for name in ordered:
        voltage = voltage_by_name.get(name)
        if voltage:
            return voltage
        match = VOLTAGE_PATTERN.search(name)
        if match:
            return f"{match.group('value')}kV"
    return None


def plan_graph_ready(client: TuGraphClient) -> bool:
    """探测 kg4 图是否可用。"""

    try:
        client.run("RETURN 1")
    except TuGraphError:
        return False
    return True


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _dedup(items: Iterable[Any]) -> list[str]:
    """去掉空值并保序去重。"""

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = _text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _clean(value: Any) -> str | None:
    """空值统一收敛为 None（图谱里空串与缺失混用）。"""

    text = _text(value)
    return text or None


def _as_int(value: Any) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None
