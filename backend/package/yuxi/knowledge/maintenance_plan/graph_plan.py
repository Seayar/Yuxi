"""kg 图检索：定位设备实例，并为选定设备收集生成处置方案所需的全部证据。

本模块只做“读图 + 打分 + 映射”，不做任何自然语言生成。``search_devices`` 按设备名称、
位置线索、设备类型与电压等级在 ``设备实例`` 中召回并打分；``collect_plan_evidence`` 把
层级路径、停电范围、作业模板（含依据条款）、试验/巡视项目、安全距离、管辖班组与备品备件
收进 ``contracts.PlanEvidence``，供装配模块确定性实例化；``parse_template`` 与
``derive_outage_scope`` 是其中可独立测试的纯函数。

TuGraph 4.5.2 实测限制（ref/kg_schema.md）：一条语句最多一个 MATCH，多跳必须拆成多条语句
在 Python 侧拼接；中文标签/关系/属性直接写裸标识符，不加反引号；所有查询都带 LIMIT。

安全距离按“电压等级 + 适用场所”取：线路类设备（架空线路/杆塔/电缆/绝缘子）或层级路径含
“集电线路”“输电”时取“线路”场所（DL/T 409），其余取“变电”场所（DL/T 408）；指定场所没有
记录时退回同电压等级任一条，并在 ``retrieval_log`` 记一句说明。

电压等级按“设备台账属性 → 按 ancestors 由近到远逐节点解析（同一节点有真实 ``电压等级`` 属性时
优先属性，否则用其名称里的 NkV）”解析：台账里设备自身电压常为空、名称也不带 kV，只能从上级节点
取（``位于`` 查询已 ``RETURN b``，同一次查询同时用于补全层级路径与解析电压，不重复查图）。
更远节点的属性不会覆盖更近节点名称里的电压（``位于`` 可能跳过中间电压区域）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any

from yuxi.knowledge.fault_trace.graph_kg import KgGraphClient, KgGraphError
from yuxi.knowledge.maintenance_plan.contracts import (
    DEVICE_NAME_PROPS,
    LABEL_CLAUSE,
    LABEL_DEVICE,
    LABEL_DEVICE_TYPE,
    LABEL_PATROL_ITEM,
    LABEL_SAFETY_DISTANCE,
    LABEL_SPARE,
    LABEL_STANDARD,
    LABEL_TEAM,
    LABEL_TEMPLATE,
    LABEL_TEST_ITEM,
    MAX_DEVICE_CANDIDATES,
    REL_BELONGS,
    REL_ELECTRIC,
    REL_HAS_TEMPLATE,
    REL_LOCATED_IN,
    REL_MANAGES,
    REL_NEEDS_PATROL,
    REL_NEEDS_TEST,
    REL_SPARE,
    REL_TEMPLATE_CLAUSE,
    SCHEME1_TO_DEVICE_TYPE,
    TASK_PATROL,
    TASK_REPAIR,
    TASK_REPLACE,
    TASK_TEST,
    ClauseRef,
    DeviceCandidate,
    PlanEvidence,
    PlanQuery,
    SafetyDistance,
    SpareInfo,
    TeamInfo,
    WorkTemplate,
)

logger = logging.getLogger(__name__)

# 单条语句的召回上限（任务卡：召回 ≤200）
KG_QUERY_LIMIT = 200
# 依据条款节选长度（contracts.ClauseRef.excerpt ≤200 字）
CLAUSE_EXCERPT_LIMIT = 200

# 层级路径分隔符（台账写法：“变电设施 > 35kV#1变电站 > … > 设备”）
PATH_SEPARATOR = " > "

# 打分权重（任务卡：名称精确相等 > 名称包含 > 层级路径包含，位置/设备类型/电压另加分）
NAME_EXACT_SCORE = 100.0
NAME_CONTAINS_SCORE = 60.0
NAME_CONTAINED_SCORE = 40.0
PATH_CONTAINS_SCORE = 30.0
LOCATION_HIT_SCORE = 30.0
DEVICE_TYPE_MATCH_SCORE = 20.0
VOLTAGE_MATCH_SCORE = 10.0

# Scheme1标签 为空的节点靠台账分类判断是否可作业（任务卡：开关柜/断路器柜/变压器/箱变/杆塔/线路类）
WORKABLE_CLASS_KEYWORDS = (
    "开关柜",
    "断路器柜",
    "断路器",
    "变压器",
    "箱变",
    "箱式变电站",
    "杆塔",
    "线路",
    "母线",
    "电缆",
    "绝缘子",
    "避雷器",
    "隔离开关",
    "互感器",
    "熔断器",
    "电抗器",
    "电容器",
    "接地装置",
)

# 停电范围推导：所在间隔/柜 与 上级电源侧节点 的名称特征
INTERVAL_MARKERS = ("间隔", "柜")
INTERVAL_NUMBER_PATTERN = re.compile(r"_\d+")
POWER_SOURCE_MARKERS = ("变电站", "开闭所", "箱变", "线路")
VOLTAGE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*[kK][vV]")

# 口语 → 台账名称的同义展开（台账里“#1主变压器”“#2站用变压器”与口语“1号主变”“2号站用变”并存）
ABBREVIATION_EXPANSIONS = {
    "主变": "主变压器",
    "站用变": "站用变压器",
    "接地变": "接地变压器",
    "所用变": "所用变压器",
}
ORDINAL_PATTERN = re.compile(r"(?P<num>\d+)\s*号")

# 班组类型按作业类型的偏好（任务卡：试验→试验，检修/更换→检修，巡视→运维；无偏好类型时取运维）
TEAM_TYPE_PREFERENCE = {
    TASK_TEST: "试验",
    TASK_REPAIR: "检修",
    TASK_REPLACE: "检修",
    TASK_PATROL: "运维",
}
FALLBACK_TEAM_TYPE = "运维"

# 通用模板的作业对象写法（找不到专用模板时退回这些模板）
GENERIC_WORK_OBJECTS = (None, "", "整体")

# 安全距离适用场所：T6 的 safety_distance.json 每个电压等级有“变电”“线路”两条记录，
# 变电场所出自 DL/T 408、线路场所出自 DL/T 409，必须按设备选对场所。
PLACE_SUBSTATION = "变电"
PLACE_LINE = "线路"
# 取“线路”场所的设备类型与层级路径标记（设备类型属于其中之一，或层级路径含集电线路/输电）
LINE_PLACE_DEVICE_TYPES = ("架空线路", "杆塔", "电缆", "绝缘子")
LINE_PLACE_PATH_MARKERS = ("集电线路", "输电")


# ---------------------------------------------------------------------------
# 设备定位
# ---------------------------------------------------------------------------
def search_devices(
    client: KgGraphClient, query: PlanQuery, *, limit: int = MAX_DEVICE_CANDIDATES
) -> list[DeviceCandidate]:
    """按 设备名称 / 位置线索 / 设备类型 / 电压等级 在 设备实例 中检索并打分排序。

    - 关键词来自 ``device_name``（含“1号→#1/1#”“主变→主变压器”的同义展开）与 ``location_hint``，
      用 CONTAINS 在 名称/显示名/name 与 层级路径 上召回；
    - 只保留可作业设备：Scheme1标签 非空，或 台账分类 属于开关柜/断路器柜/变压器/箱变/杆塔/线路类；
    - 打分：名称精确相等 > 名称包含 > 层级路径包含，位置线索命中路径、设备类型与电压等级一致另加分；
    - 既无名称关键词也无位置线索时（用户只说“开关柜”这类设备类型），改按 属于 关系按设备类型召回；
    - 返回按 score 降序（同分按名称、id 稳定排序），设备类型用 SCHEME1_TO_DEVICE_TYPE 归一。
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
        if not isinstance(props, dict) or not _is_workable(props):
            continue
        candidate = _build_candidate(props, query=query, keywords=keywords)
        if not candidate.device_id or candidate.score <= 0:
            continue
        current = candidates.get(candidate.device_id)
        if current is None or candidate.score > current.score:
            candidates[candidate.device_id] = candidate

    ordered = sorted(candidates.values(), key=lambda item: (-item.score, item.name, item.device_id))
    return ordered[:limit]


# ---------------------------------------------------------------------------
# 证据收集
# ---------------------------------------------------------------------------
def collect_plan_evidence(client: KgGraphClient, query: PlanQuery, device: DeviceCandidate) -> PlanEvidence:
    """收集生成方案所需的全部证据（层级、停电范围、模板、项目、安全距离、班组、备件）。

    每一步都往 ``retrieval_log`` 写一句人话，供需求说明的“思考过程”直接引用；
    图谱里查不到的内容留空，不编造。
    """

    evidence = PlanEvidence(device=device)
    log = evidence.retrieval_log

    # 1) 层级：层级路径拆分为主，位于 关系校验补全（父节点属性保留，用于解析电压等级）
    parents = _query_parents(client, device=device)
    ancestors = _ancestors_from_path(device.path)
    for props in reversed(parents):
        name = _device_name(props)
        if name and name not in ancestors:
            ancestors.insert(0, name)
    evidence.ancestors = ancestors
    if ancestors:
        log.append(f"沿层级路径解析出上级节点：{' > '.join(reversed(ancestors))}")
    else:
        log.append(f"设备“{device.name}”没有层级路径，未解析到上级节点")

    # 2) 停电范围：设备本身 + 所在间隔/柜 + 上级电源侧 + 电连接上游邻居
    upstream_neighbors, neighbors = _collect_electrical_neighbors(client, device=device)
    evidence.electrical_neighbors = neighbors
    evidence.outage_scope = derive_outage_scope(device, ancestors, upstream_neighbors)
    if evidence.outage_scope:
        log.append(f"停电范围按台账层级推导：{'、'.join(evidence.outage_scope)}")
    if neighbors:
        log.append(f"电连接关系检索到 {len(neighbors)} 台相邻设备：{'、'.join(neighbors)}")
    else:
        log.append("台账电连接关系为空，停电范围仅按层级关系推导")

    # 电压等级：设备台账属性优先；否则按 ancestors 由近到远逐节点解析（节点属性优先于其名称里的 kV）
    device_voltage = _clean(device.voltage_level)
    voltage_level = device_voltage or _resolve_voltage_level(parents, ancestors)
    if voltage_level and not device_voltage:
        log.append(f"设备台账电压等级为空，按上级节点解析出电压等级“{voltage_level}”")

    # 3) 作业模板与依据条款
    if device.device_type and query.task_type:
        records = _query_templates(client, device_type=device.device_type)
        picked = _pick_template(records, task_type=query.task_type, work_object=query.work_object)
        if picked is None:
            log.append(f"未检索到设备类型“{device.device_type}”在作业类型“{query.task_type}”下的作业模板")
        else:
            clauses = _collect_template_clauses(client, template_id=_clean(picked.get("id")) or "")
            evidence.template = parse_template(picked, clauses)
            template = evidence.template
            if query.work_object and template.work_object != query.work_object:
                log.append(f"未匹配到作业对象“{query.work_object}”的专用模板，退回通用模板“{template.name}”")
            else:
                log.append(
                    f"按作业类型“{query.task_type}”和设备类型“{device.device_type}”检索到作业模板"
                    f"“{template.name}”，含 {len(template.step_sequence)} 道工序"
                )
            if clauses:
                log.append(f"作业模板关联 {len(clauses)} 条依据条款")
    else:
        log.append("缺少设备类型或作业类型，未检索作业模板")

    # 4) 试验/巡视项目
    if device.device_type:
        evidence.test_items = _query_item_names(
            client, device_type=device.device_type, relation=REL_NEEDS_TEST, target_label=LABEL_TEST_ITEM
        )
        evidence.patrol_items = _query_item_names(
            client,
            device_type=device.device_type,
            relation=REL_NEEDS_PATROL,
            target_label=LABEL_PATROL_ITEM,
        )
        log.append(
            f"设备类型“{device.device_type}”关联 {len(evidence.test_items)} 个试验项目、"
            f"{len(evidence.patrol_items)} 个巡视项目"
        )
    else:
        log.append("设备类型未知，未检索试验项目与巡视项目")

    # 5) 安全距离：先按电压等级 + 适用场所（变电/线路）取，指定场所缺失时回退同电压任一条
    if voltage_level:
        place = _safety_distance_place(device)
        evidence.safety_distance = _query_safety_distance(client, voltage_level=voltage_level, place=place, log=log)
        if evidence.safety_distance is None:
            log.append(f"知识图谱中暂无“{voltage_level}”的安全距离数据")
        else:
            distance = evidence.safety_distance
            log.append(
                f"按电压等级“{voltage_level}”、优先适用场所“{place}”取到安全距离："
                f"设备不停电 {_meters(distance.no_outage_m)}、"
                f"工作中正常活动范围 {_meters(distance.work_range_m)}、"
                f"带电作业 {_meters(distance.live_work_m)}"
            )
    else:
        log.append("设备与上级节点都没有电压等级，未检索安全距离")

    # 6) 管辖班组：沿 ancestors 由近到远找第一个被班组管辖的区域节点
    evidence.team = _find_team(client, ancestors=ancestors, task_type=query.task_type)
    if evidence.team is None:
        log.append("未检索到该设备或其上级区域的管辖班组")
    else:
        team = evidence.team
        count = f"（{team.member_count}人）" if team.member_count is not None else ""
        leader = f"，负责人“{team.leader}”" if team.leader else ""
        log.append(f"沿层级向上在“{team.managed_area}”找到管辖班组“{team.name}”{count}{leader}")

    # 7) 备品备件
    if device.device_type:
        work_object = evidence.template.work_object if evidence.template is not None else ""
        evidence.spares = _find_spares(
            client, device_type=device.device_type, work_object=work_object or (query.work_object or "")
        )
        log.append(f"设备类型“{device.device_type}”关联 {len(evidence.spares)} 项备品备件")
    else:
        log.append("设备类型未知，未检索备品备件")

    return evidence


def parse_template(props: dict, clauses: list[ClauseRef]) -> WorkTemplate:
    """作业模板节点属性（JSON 数组以字符串存储）→ WorkTemplate；非法 JSON 时按换行/；拆分，不抛异常。"""

    return WorkTemplate(
        template_id=_clean(props.get("id")) or "",
        name=_clean(props.get("名称")) or "",
        task_type=_clean(props.get("作业类型")) or "",
        device_type=_clean(props.get("设备类型")) or "",
        work_object=_clean(props.get("作业对象")) or "",
        outage_required=_clean(props.get("停电要求")) or "",
        isolation_measures=_clean(props.get("安全隔离措施")) or "",
        step_sequence=_json_list(props.get("工序顺序"), field="工序顺序"),
        key_points=_json_list(props.get("操作要点"), field="操作要点"),
        tech_params=_json_list(props.get("技术参数要求"), field="技术参数要求"),
        personal_protection=_clean(props.get("个人防护要求")) or "",
        grounding_requirements=_clean(props.get("验电接地要求")) or "",
        work_safety=_json_list(props.get("作业安全要求"), field="作业安全要求"),
        tools=_json_list(props.get("工器具清单"), field="工器具清单"),
        materials=_json_list(props.get("材料清单"), field="材料清单"),
        acceptance=_json_list(props.get("验收标准"), field="验收标准"),
        notes=_json_list(props.get("注意事项"), field="注意事项"),
        staffing=_clean(props.get("人员配置建议")) or "",
        clauses=list(clauses),
        source_note=_clean(props.get("来源说明")) or "",
    )


def derive_outage_scope(device: DeviceCandidate, ancestors: list[str], neighbors: list[str]) -> list[str]:
    """停电范围规则：[设备本身, 所在间隔/柜（ancestors 中名称含 间隔/柜/_9xx 编号的最近一个）,
    最近的 站/开闭所/箱变/线路 节点（作为上级电源侧）] 去重；有 电连接 邻居时把上游邻居加入。

    ``neighbors`` 按“上游邻居”语义传入（调用方只传电连接中指向本设备的一侧）；去重后保序。
    """

    scope = [device.name] if device.name else []
    interval = next((name for name in ancestors if _is_interval(name)), None)
    if interval:
        scope.append(interval)
    power_source = next((name for name in ancestors if name != interval and _is_power_source(name)), None)
    if power_source:
        scope.append(power_source)
    for neighbor in neighbors:
        if neighbor and neighbor not in scope:
            scope.append(neighbor)
    return scope


def plan_graph_ready(client: KgGraphClient) -> bool:
    """探测 kg 图是否可用：未配置或连接失败返回 False，由工具层投影为“处置方案知识库未配置”。"""

    try:
        client.run("RETURN 1")
    except KgGraphError:
        return False
    return True


# ---------------------------------------------------------------------------
# 设备候选的内部实现
# ---------------------------------------------------------------------------
def _device_keywords(query: PlanQuery) -> list[str]:
    """设备名称关键词：原话指称 + 同义展开（“1号主变”→“#1主变”“#1主变压器”）。"""

    base = _clean(query.device_name)
    if base is None:
        return []
    return _dedup([base, *_synonym_variants(base)])


def _synonym_variants(name: str) -> list[str]:
    """口语指称的同义写法：1号→#1/1#，主变→主变压器（台账里两种写法并存）。"""

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


def _build_candidate(props: dict[str, Any], *, query: PlanQuery, keywords: list[str]) -> DeviceCandidate:
    """台账节点属性 → 打分后的 DeviceCandidate。"""

    name = _device_name(props)
    path = str(props.get("层级路径") or "")
    score = _name_score(name, path, keywords)
    location_hint = _clean(query.location_hint)
    if location_hint and location_hint in path:
        score += LOCATION_HIT_SCORE

    scheme1 = _clean(props.get("Scheme1标签"))
    device_type = SCHEME1_TO_DEVICE_TYPE.get(scheme1, scheme1) if scheme1 else None
    if query.device_type and device_type == query.device_type:
        score += DEVICE_TYPE_MATCH_SCORE

    voltage_level = _clean(props.get("电压等级"))
    if query.voltage_level and voltage_level == query.voltage_level:
        score += VOLTAGE_MATCH_SCORE

    return DeviceCandidate(
        device_id=_clean(props.get("id")) or name,
        name=name,
        device_type=device_type,
        voltage_level=voltage_level,
        path=path,
        ledger_class=_clean(props.get("台账分类")) or _clean(props.get("设备分类")),
        score=float(score),
    )


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


def _is_workable(props: dict[str, Any]) -> bool:
    """可作业设备：Scheme1标签 非空，或 台账分类 落在可作业关键词内。"""

    if _clean(props.get("Scheme1标签")):
        return True
    ledger_class = _clean(props.get("设备分类")) or _clean(props.get("台账分类")) or ""
    return any(keyword in ledger_class for keyword in WORKABLE_CLASS_KEYWORDS)


def _device_name(props: dict[str, Any]) -> str:
    """取设备名称：按 contracts 的名称属性优先级，全空时退回 id。"""

    for prop in DEVICE_NAME_PROPS:
        value = _clean(props.get(prop))
        if value:
            return value
    return _clean(props.get("id")) or ""


# ---------------------------------------------------------------------------
# 层级、停电范围与电压
# ---------------------------------------------------------------------------
def _ancestors_from_path(path: str) -> list[str]:
    """层级路径 → 上级节点名称，从近到远（去掉设备自身）。"""

    parts = [part.strip() for part in (path or "").split(PATH_SEPARATOR) if part.strip()]
    return list(reversed(parts[:-1])) if parts else []


def _is_interval(name: str) -> bool:
    """是否所在间隔/柜：名称含 间隔/柜，或含 _9xx 一类编号。"""

    return any(marker in name for marker in INTERVAL_MARKERS) or bool(INTERVAL_NUMBER_PATTERN.search(name))


def _is_power_source(name: str) -> bool:
    """是否上级电源侧节点：站/开闭所/箱变（群）/集电线路。"""

    return any(marker in name for marker in POWER_SOURCE_MARKERS)


def _resolve_voltage_level(parents: list[dict[str, Any]], ancestors: list[str]) -> str | None:
    """按 ``ancestors`` 由近到远逐个节点解析电压等级（设备自身电压由调用方优先处理）。

    同一个节点有真实 ``电压等级`` 属性（来自 位于 查询返回的父节点属性）时优先用属性，否则用该
    节点名称里的 ``NkV``；取到即返回，因此**更远节点的属性不会覆盖更近节点名称里的电压**。
    ``位于`` 关系可能跳过中间节点（如设备在“10kV电压等级区域”下、位于 直接返回 35kV 变电站），
    所以不在 ``ancestors`` 里的父节点排在所有祖先之后，避免远站属性覆盖更近区域。
    所有节点都没有有效电压时返回 None（保持缺失，不假定某类节点的电压，如不假定开闭所都是 10kV）。
    """

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


def _collect_electrical_neighbors(client: KgGraphClient, *, device: DeviceCandidate) -> tuple[list[str], list[str]]:
    """电连接邻居：a→b 表示 a 为电源侧，返回（上游邻居, 全部邻居）。"""

    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $label AND labels(b)[0] = $label "
        "AND type(r) = $rel "
        "AND (coalesce(a.id, '') = $device_id OR coalesce(b.id, '') = $device_id) "
        "RETURN a, b LIMIT $node_limit",
        label=LABEL_DEVICE,
        rel=REL_ELECTRIC,
        device_id=device.device_id,
        node_limit=KG_QUERY_LIMIT,
    )

    upstream: list[str] = []
    neighbors: list[str] = []
    for record in records:
        source = record.get("a") or {}
        target = record.get("b") or {}
        if _is_device_node(target, device) and not _is_device_node(source, device):
            upstream.append(_device_name(source))
            neighbors.append(_device_name(source))
        elif _is_device_node(source, device) and not _is_device_node(target, device):
            neighbors.append(_device_name(target))
    return _dedup(upstream), _dedup(neighbors)


def _is_device_node(props: dict[str, Any], device: DeviceCandidate) -> bool:
    """节点是否就是目标设备：优先比 id，没有 id 时退回名称（台账重名靠 id 区分）。"""

    node_id = _clean(props.get("id"))
    if node_id and device.device_id:
        return node_id == device.device_id
    name = _device_name(props)
    return bool(name and name == device.name)


# ---------------------------------------------------------------------------
# 作业模板、项目与安全距离
# ---------------------------------------------------------------------------
def _pick_template(records: list[dict[str, Any]], *, task_type: str | None, work_object: str | None) -> dict | None:
    """按作业类型过滤后，优先作业对象精确命中、再适用关键词命中，最后退回通用模板。"""

    matched: list[dict[str, Any]] = []
    for record in records:
        props = record.get("b") or {}
        if task_type and _clean(props.get("作业类型")) != task_type:
            continue
        matched.append(props)
    if not matched:
        return None

    generic = [props for props in matched if _clean(props.get("作业对象")) in GENERIC_WORK_OBJECTS]
    if work_object:
        for props in matched:
            if _clean(props.get("作业对象")) == work_object:
                return props
        for props in matched:
            if _keyword_hits(props, work_object):
                return props
        return generic[0] if generic else None
    return generic[0] if generic else matched[0]


def _keyword_hits(props: dict[str, Any], work_object: str) -> bool:
    """模板的 适用关键词 是否覆盖该作业对象（互相包含都算命中）。"""

    keywords = _json_list(props.get("适用关键词"), field="适用关键词")
    return any(work_object in keyword or keyword in work_object for keyword in keywords)


def _collect_template_clauses(client: KgGraphClient, *, template_id: str) -> list[ClauseRef]:
    """作业模板 -[依据条款]-> 标准条款，标准编号从 标准 节点取。"""

    if not template_id:
        return []
    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel "
        "AND labels(b)[0] = $dst AND coalesce(a.id, '') = $template_id "
        "RETURN b LIMIT $node_limit",
        src=LABEL_TEMPLATE,
        rel=REL_TEMPLATE_CLAUSE,
        dst=LABEL_CLAUSE,
        template_id=template_id,
        node_limit=KG_QUERY_LIMIT,
    )

    clause_props = [record.get("b") or {} for record in records]
    standard_numbers = _query_standard_numbers(
        client, standard_ids=[_clean(props.get("standard_id")) for props in clause_props]
    )
    clauses: list[ClauseRef] = []
    for props in clause_props:
        standard_id = _clean(props.get("standard_id")) or ""
        clauses.append(
            ClauseRef(
                standard_no=standard_numbers.get(standard_id, standard_id),
                clause_no=_clean(props.get("条款号")) or "",
                title=_clean(props.get("条款标题")) or "",
                excerpt=(_clean(props.get("条款内容")) or "")[:CLAUSE_EXCERPT_LIMIT],
            )
        )
    return clauses


def _query_item_names(client: KgGraphClient, *, device_type: str, relation: str, target_label: str) -> list[str]:
    """设备类型 -[需要试验/需要巡视]-> 项目名称。"""

    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel "
        "AND labels(b)[0] = $dst AND coalesce(a.名称, '') = $device_type "
        "RETURN b LIMIT $node_limit",
        src=LABEL_DEVICE_TYPE,
        rel=relation,
        dst=target_label,
        device_type=device_type,
        node_limit=KG_QUERY_LIMIT,
    )
    return _dedup(_clean((record.get("b") or {}).get("项目名称")) for record in records)


def _safety_distance_place(device: DeviceCandidate) -> str:
    """按设备判定安全距离适用场所：线路类设备（架空线路/杆塔/电缆/绝缘子），
    或层级路径含“集电线路”“输电”时取“线路”，其余取“变电”。"""

    if device.device_type in LINE_PLACE_DEVICE_TYPES:
        return PLACE_LINE
    if any(marker in device.path for marker in LINE_PLACE_PATH_MARKERS):
        return PLACE_LINE
    return PLACE_SUBSTATION


def _query_safety_distance(
    client: KgGraphClient, *, voltage_level: str, place: str, log: list[str] | None = None
) -> SafetyDistance | None:
    """按电压等级 + 适用场所取 安全距离 节点。

    指定场所没有记录时退回同电压等级任一条，并往 ``log``（retrieval_log）记一句说明；
    两条语句都只有一个 MATCH、中文属性不加反引号。
    """

    records = client.run(
        "MATCH (n) WHERE labels(n)[0] = $label AND coalesce(n.电压等级, '') = $voltage_level "
        "AND coalesce(n.适用场所, '') = $place "
        "RETURN n LIMIT $node_limit",
        label=LABEL_SAFETY_DISTANCE,
        voltage_level=voltage_level,
        place=place,
        node_limit=KG_QUERY_LIMIT,
    )
    if not records:
        records = client.run(
            "MATCH (n) WHERE labels(n)[0] = $label AND coalesce(n.电压等级, '') = $voltage_level "
            "RETURN n LIMIT $node_limit",
            label=LABEL_SAFETY_DISTANCE,
            voltage_level=voltage_level,
            node_limit=KG_QUERY_LIMIT,
        )
        if records and log is not None:
            log.append(f"知识图谱中“{voltage_level}”没有“{place}”场所的安全距离记录，已退回同电压等级任一条")
    if not records:
        return None

    props = records[0].get("n") or {}
    standard_no = _clean(props.get("依据标准")) or ""
    clause_no = _clean(props.get("条款号")) or ""
    clause = ClauseRef(standard_no=standard_no, clause_no=clause_no) if standard_no or clause_no else None
    return SafetyDistance(
        voltage_level=_clean(props.get("电压等级")) or voltage_level,
        no_outage_m=_as_float(props.get("设备不停电安全距离_m")),
        work_range_m=_as_float(props.get("工作中正常活动范围安全距离_m")),
        live_work_m=_as_float(props.get("带电作业安全距离_m")),
        clause=clause,
        remark=_clean(props.get("备注")) or "",
    )


# ---------------------------------------------------------------------------
# 班组与备品备件
# ---------------------------------------------------------------------------
def _find_team(client: KgGraphClient, *, ancestors: list[str], task_type: str | None) -> TeamInfo | None:
    """沿 ancestors 由近到远找第一个被班组管辖的区域节点，多个班组按作业类型偏好挑选。"""

    if not ancestors:
        return None
    cypher = (
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel "
        f"AND labels(b)[0] = $dst AND {_in_condition('b', '名称', 'area_names')} "
        "RETURN a, b LIMIT $node_limit"
    )
    records = client.run(
        cypher,
        src=LABEL_TEAM,
        rel=REL_MANAGES,
        dst=LABEL_DEVICE,
        area_names=ancestors,
        node_limit=KG_QUERY_LIMIT,
    )

    teams_by_area: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        area = _device_name(record.get("b") or {})
        if area:
            teams_by_area.setdefault(area, []).append(record.get("a") or {})

    for area in ancestors:
        teams = teams_by_area.get(area)
        if teams:
            return _build_team(_pick_team(teams, task_type), managed_area=area)
    return None


def _pick_team(teams: list[dict[str, Any]], task_type: str | None) -> dict[str, Any]:
    """同区域多个班组：按作业类型偏好班组类型，没有偏好类型时取运维班。"""

    preferred = TEAM_TYPE_PREFERENCE.get(task_type, FALLBACK_TEAM_TYPE)
    for team in teams:
        if _clean(team.get("班组类型")) == preferred:
            return team
    for team in teams:
        if _clean(team.get("班组类型")) == FALLBACK_TEAM_TYPE:
            return team
    return teams[0]


def _build_team(props: dict[str, Any], *, managed_area: str) -> TeamInfo:
    return TeamInfo(
        name=_clean(props.get("名称")) or "",
        member_count=_as_int(props.get("人数")),
        leader=_clean(props.get("负责人")) or "",
        managed_area=managed_area,
        source_note=_clean(props.get("数据来源")) or "",
    )


def _find_spares(client: KgGraphClient, *, device_type: str, work_object: str) -> list[SpareInfo]:
    """设备类型 -[配备备件]-> 备品备件；名称含作业对象的备件排前面。"""

    if not device_type:
        return []
    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel "
        "AND labels(b)[0] = $dst AND coalesce(a.名称, '') = $device_type "
        "RETURN b LIMIT $node_limit",
        src=LABEL_DEVICE_TYPE,
        rel=REL_SPARE,
        dst=LABEL_SPARE,
        device_type=device_type,
        node_limit=KG_QUERY_LIMIT,
    )
    spares = [_build_spare(record.get("b") or {}) for record in records]
    if not work_object:
        return spares
    matched = [spare for spare in spares if work_object in spare.name]
    others = [spare for spare in spares if work_object not in spare.name]
    return matched + others


def _build_spare(props: dict[str, Any]) -> SpareInfo:
    return SpareInfo(
        name=_clean(props.get("名称")) or "",
        model=_clean(props.get("型号规格")) or "",
        quantity=_as_int(props.get("库存数量")),
        location=_clean(props.get("存放地点")) or "",
        source_note=_clean(props.get("数据来源")) or "",
    )


# ---------------------------------------------------------------------------
# 查询语句（一条语句一个 MATCH；中文标签/属性不加反引号；用户输入只走参数）
# ---------------------------------------------------------------------------
def _in_condition(alias: str, prop: str, param: str) -> str:
    """生成 ``coalesce(alias.属性, '') IN $参数`` 条件片段。

    设备/班组/标准等按名称列表过滤的语句都用它拼条件；集中到一处，便于联调时
    若某个 TuGraph 版本不支持该写法，只改这一个函数即可。
    """

    return f"coalesce({alias}.{prop}, '') IN ${param}"


def _query_devices_by_keyword(client: KgGraphClient, *, keyword: str) -> list[dict[str, Any]]:
    """按关键词在 名称/显示名/name 与 层级路径 上召回设备实例。"""

    props = (*DEVICE_NAME_PROPS, "层级路径")
    conditions = " OR ".join(f"coalesce(n.{prop}, '') CONTAINS $kw" for prop in props)
    return client.run(
        f"MATCH (n) WHERE labels(n)[0] = $label AND ({conditions}) RETURN n LIMIT $node_limit",
        label=LABEL_DEVICE,
        kw=keyword,
        node_limit=KG_QUERY_LIMIT,
    )


def _query_devices_by_device_type(client: KgGraphClient, *, device_type: str) -> list[dict[str, Any]]:
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


def _query_parents(client: KgGraphClient, *, device: DeviceCandidate) -> list[dict[str, Any]]:
    """设备实例 -[位于]-> 父节点属性（校验/补全层级路径，并保留父节点的电压等级等图谱属性）。

    只查一次：父节点名称用于补全 ``ancestors``，电压等级属性用于解析设备电压
    （真实台账里设备自身电压常为空、名称也不带 kV，只能从父节点属性取）。
    """

    records = client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND labels(b)[0] = $dst "
        "AND type(r) = $rel "
        "AND coalesce(a.id, '') = $device_id "
        "RETURN b LIMIT $node_limit",
        src=LABEL_DEVICE,
        dst=LABEL_DEVICE,
        rel=REL_LOCATED_IN,
        device_id=device.device_id,
        node_limit=KG_QUERY_LIMIT,
    )
    return [record.get("b") or {} for record in records]


def _query_templates(client: KgGraphClient, *, device_type: str) -> list[dict[str, Any]]:
    """设备类型 -[适用模板]-> 作业模板（作业类型过滤在 Python 侧做）。"""

    return client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel "
        "AND labels(b)[0] = $dst AND coalesce(a.名称, '') = $device_type "
        "RETURN b LIMIT $node_limit",
        src=LABEL_DEVICE_TYPE,
        rel=REL_HAS_TEMPLATE,
        dst=LABEL_TEMPLATE,
        device_type=device_type,
        node_limit=KG_QUERY_LIMIT,
    )


def _query_standard_numbers(client: KgGraphClient, *, standard_ids: list[str | None]) -> dict[str, str]:
    """standard_id → 标准编号（如 DLT408 → DL/T 408—2023）。"""

    ids = _dedup(standard_ids)
    if not ids:
        return {}
    cypher = (
        "MATCH (n) WHERE labels(n)[0] = $label "
        f"AND {_in_condition('n', 'standard_id', 'standard_ids')} "
        "RETURN n LIMIT $node_limit"
    )
    records = client.run(
        cypher,
        label=LABEL_STANDARD,
        standard_ids=ids,
        node_limit=KG_QUERY_LIMIT,
    )
    numbers: dict[str, str] = {}
    for record in records:
        props = record.get("n") or {}
        standard_id = _clean(props.get("standard_id"))
        if standard_id:
            numbers[standard_id] = _clean(props.get("标准编号")) or standard_id
    return numbers


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _json_list(value: Any, *, field: str) -> list[str]:
    """模板里的 JSON 数组（以字符串存储）→ 字符串列表；非法 JSON 时按换行/分号拆分。"""

    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return _dedup(_text(item) for item in value)
    text = str(value).strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"作业模板字段 {field} 不是合法 JSON，按换行/分号拆分")
        return _split_text(text)
    if isinstance(payload, list):
        return _dedup(_text(item) for item in payload)
    if isinstance(payload, str):
        return _split_text(payload)
    logger.warning(f"作业模板字段 {field} 既不是数组也不是字符串，已忽略")
    return []


def _split_text(text: str) -> list[str]:
    """按换行或中英文分号拆分文本列表。"""

    return _dedup(part.strip() for part in re.split(r"[\n;；]", text))


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


def _as_float(value: Any) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _meters(value: float | None) -> str:
    """安全距离展示：没有数据时用“—”占位。"""

    return "—" if value is None else f"{value}m"
