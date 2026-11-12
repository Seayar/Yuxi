"""kg5 图检索：定位设备、解析预测服务设备 ID，并按预测故障类型匹配维护知识。

本模块只做“读图 + 打分 + 过滤”，不做任何自然语言生成。图谱读取都指向设备预测维护
自己的 ``kg5`` 图：``search_devices`` 在 ``设备实例`` 中按名称/层级路径召回，
``resolve_device_id`` 先取设备节点的 ``台账ID列表``（占位值时按精确名称回查三区台账），
``lookup_maintenance_guidance`` 沿 ``设备类型 -[具有现象]-> 故障现象`` 匹配预测故障类型，
再从故障现象取 ``所需资源`` 与 ``注意事项``。图谱不读故障溯源 ``kg3`` 与处置方案 ``kg4``。

TuGraph 4.5.2 实测限制：一条语句最多一个 MATCH，多跳必须拆成多条语句在 Python 侧拼接；
中文标签/关系/属性直接写裸标识符，不加反引号；所有查询都带 LIMIT。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import replace
from typing import Any

from yuxi.agents.toolkits.tugraph import TuGraphClient, TuGraphError
from yuxi.knowledge.device_prediction.contracts import (
    DEVICE_NAME_PROPS,
    FAULT_CODE_EXACT_SCORE,
    FAULT_TYPE_SEPARATORS,
    GUIDE_QUERY_LIMIT,
    GUIDANCE_NOT_MATCHED_NOTE,
    GUIDANCE_NO_DEVICE_TYPE_NOTE,
    GUIDANCE_QUERY_FAILED_NOTE,
    LABEL_DEVICE,
    LABEL_DEVICE_TYPE,
    LABEL_PHENOMENON,
    LEDGER_ID_PLACEHOLDERS,
    MAX_DEVICE_CANDIDATES,
    NUMBERED_ITEM_PATTERN,
    PHENOMENON_BIGRAM_MIN_RATIO,
    PHENOMENON_BIGRAM_SCORE,
    PHENOMENON_MATCH_THRESHOLD,
    PHENOMENON_NAME_CONTAINS_SCORE,
    PHENOMENON_NAME_EXACT_SCORE,
    PROP_CAUTIONS,
    PROP_FAULT_CODE,
    PROP_PHENOMENON_NAME,
    PROP_REQUIRED_RESOURCES,
    REL_BELONGS,
    REL_HAS_PHENOMENON,
    DeviceCandidate,
    MaintenanceGuidance,
    PredictionQuery,
    dedup,
)
from yuxi.utils import logger

# 单条设备召回语句的上限
KG_QUERY_LIMIT = 200

# 打分权重：名称精确 > 名称包含 > 名称被包含 > 仅层级路径包含
NAME_EXACT_SCORE = 100.0
NAME_CONTAINS_SCORE = 60.0
NAME_CONTAINED_SCORE = 40.0
PATH_CONTAINS_SCORE = 30.0
LOCATION_HIT_SCORE = 30.0
DEVICE_TYPE_MATCH_SCORE = 20.0
VOLTAGE_MATCH_SCORE = 10.0

# 口语 → 台账名称的同义展开
ABBREVIATION_EXPANSIONS = {
    "主变": "主变压器",
    "站用变": "站用变压器",
    "接地变": "接地变压器",
    "所用变": "所用变压器",
}
ORDINAL_PATTERN = re.compile(r"(?P<num>\d+)\s*号")
# 维护知识字段的分隔符：换行是条目分隔；只有整段没有换行时才用分号兜底拆项。
# 顿号与逗号在中文里常出现在一句话内部，不参与切分；结构化多条目请用 JSON 数组。
_LINE_SEPARATOR_PATTERN = re.compile(r"[\r\n]+")
_ITEM_SEPARATOR_PATTERN = re.compile(r"[；;]+")


# ---------------------------------------------------------------------------
# 设备定位
# ---------------------------------------------------------------------------
def search_devices(
    client: TuGraphClient, query: PredictionQuery, *, limit: int = MAX_DEVICE_CANDIDATES
) -> list[DeviceCandidate]:
    """按设备名称/位置线索在 kg5 的 ``设备实例`` 中召回并打分。

    - 关键词来自 ``device_name``（含“1号→#1/1#”的同义展开）与 ``location_hint``；
    - 设备类型只作为加分项与展示信息：缺少 ``属于`` 边不影响设备召回；
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
        candidate = _build_candidate(record.get("n") or record.get("a") or {}, query=query, keywords=keywords)
        if not candidate.device_id or candidate.score <= 0:
            continue
        current = candidates.get(candidate.device_id)
        if current is None or candidate.score > current.score:
            candidates[candidate.device_id] = candidate

    ordered = sorted(candidates.values(), key=lambda item: (-item.score, item.name, item.device_id))
    top = ordered[: max(limit * 2, limit)]
    types = _query_device_types(client, device_ids=[item.device_id for item in top])

    typed: list[DeviceCandidate] = []
    for candidate in top:
        device_type = types.get(candidate.device_id)
        score = candidate.score
        if query.device_type and device_type == query.device_type:
            score += DEVICE_TYPE_MATCH_SCORE
        typed.append(replace(candidate, device_type=device_type, score=score))

    typed.sort(key=lambda item: (-item.score, item.name, item.device_id))
    return typed[:limit]


async def resolve_device_id(candidate: DeviceCandidate) -> tuple[str, str] | None:
    """解析预测服务使用的设备 ID，返回 ``(设备ID, 来源)``；取不到时返回 None。

    先取 ``kg5`` 设备节点的 ``台账ID列表``（跳过“补充数据”等占位值）；只有节点上的 ID 不可
    用时，才按**精确名称 + 层级祖先**回查三区设备台账（MinIO ``fault-assets/ledger/devices.json``）。
    台账里只接受与设备名称完全一致的行，不采用子串匹配，避免把报告绑定到另一台设备；
    图谱节点 id 属于图内部命名空间，不作为业务设备 ID 外发。
    """

    for ledger_id in candidate.ledger_ids:
        value = str(ledger_id).strip()
        if value and value not in LEDGER_ID_PLACEHOLDERS:
            return value, "台账ID列表"
    device_id = await _lookup_ledger_device_id(candidate)
    return (device_id, "台账接口") if device_id else None


async def _lookup_ledger_device_id(candidate: DeviceCandidate) -> str | None:
    """按精确名称与层级祖先查三区台账；名称不精确、台账不可用或无法唯一命中时返回 None。"""

    name = _clean(candidate.name)
    if name is None:
        return None
    # 延迟导入：台账模块由故障溯源拥有，图谱模块只在补设备 ID 时用到它。
    from yuxi.knowledge.fault_trace.ledger import ledger_repository

    try:
        device = await ledger_repository.find_device(name, ancestors=_path_ancestors(candidate.path))
    except Exception as exc:  # noqa: BLE001 —— 台账不可用不影响图谱侧结论，由调用方给固定文案
        logger.warning(f"设备台账查询失败（{candidate.name}）: {exc}")
        return None
    if device is None or _normalize_text(device.name) != _normalize_text(name):
        return None
    return device.device_id or None


def _path_ancestors(path: str) -> list[str]:
    """层级路径 → 上级名称；去掉设备自身。"""

    parts = [part.strip() for part in str(path or "").split(">") if part.strip()]
    return parts[:-1] if parts else []


def _device_keywords(query: PredictionQuery) -> list[str]:
    """设备名称关键词：原话指称 + 同义展开（“1号主变”→“#1主变”“#1主变压器”）。"""

    base = _clean(query.device_name)
    if base is None:
        return []
    return dedup([base, *_synonym_variants(base)])


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


def _build_candidate(props: dict[str, Any], *, query: PredictionQuery, keywords: list[str]) -> DeviceCandidate:
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
        voltage_level=voltage_level,
        path=path,
        ledger_ids=_parse_ledger_ids(props.get("台账ID列表")),
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


def _device_name(props: dict[str, Any]) -> str:
    """取设备名称：按名称属性优先级，全空时退回节点 id。"""

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
    return tuple(dedup(part.strip() for part in re.split(r"[、,，;；\s]+", text)))


# ---------------------------------------------------------------------------
# 维护知识（设备类型 → 故障现象）
# ---------------------------------------------------------------------------
def lookup_maintenance_guidance(
    client: TuGraphClient,
    *,
    fault_type: str,
    device_type: str = "",
    limit: int = GUIDE_QUERY_LIMIT,
) -> MaintenanceGuidance:
    """按预测故障类型在 ``故障现象`` 上匹配 ``所需资源`` 与 ``注意事项``。

    - 候选范围优先限定为该设备类型经 ``具有现象`` 关联的现象节点，避免把别类设备的现象
      资源算到本设备上；设备没有类型时退回全图现象匹配；
    - 现象匹配顺序：``故障码`` 精确 → 名称精确 → 名称包含 → 字符二元组重叠；
    - 没匹配到现象时返回带说明的空结果，不伪造内容；查询失败（图不可用、标签不存在）
      同样降级为带失败说明的空结果。
    """

    wanted = _clean(fault_type)
    if wanted is None:
        return MaintenanceGuidance(notes=[GUIDANCE_NOT_MATCHED_NOTE])
    scoped = _clean(device_type)
    try:
        phenomena = _query_phenomena(client, device_type=scoped, limit=limit)
        matched = _best_phenomenon(phenomena, wanted, allow_fuzzy=scoped is not None)
    except TuGraphError as exc:
        logger.warning(f"kg5 维护知识查询失败（故障类型：{fault_type}）: {exc}")
        return MaintenanceGuidance(notes=[GUIDANCE_QUERY_FAILED_NOTE])
    if matched is None:
        return MaintenanceGuidance(
            notes=[GUIDANCE_NOT_MATCHED_NOTE] + ([] if scoped is not None else [GUIDANCE_NO_DEVICE_TYPE_NOTE])
        )
    phenomenon, phenomenon_name = matched
    return MaintenanceGuidance(
        fault_type=phenomenon_name,
        resources=_split_items(phenomenon.get(PROP_REQUIRED_RESOURCES)),
        cautions=_split_items(phenomenon.get(PROP_CAUTIONS)),
        notes=[] if scoped is not None else [GUIDANCE_NO_DEVICE_TYPE_NOTE],
    )


def _query_phenomena(client: TuGraphClient, *, device_type: str | None, limit: int) -> list[dict[str, Any]]:
    """取候选 ``故障现象``：有设备类型时按 ``具有现象`` 限定，否则取全图现象。"""

    if device_type:
        records = client.run(
            "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel "
            "AND labels(b)[0] = $dst AND coalesce(a.名称, '') = $device_type "
            "RETURN b LIMIT $node_limit",
            src=LABEL_DEVICE_TYPE,
            rel=REL_HAS_PHENOMENON,
            dst=LABEL_PHENOMENON,
            device_type=device_type,
            node_limit=limit,
        )
        return [record.get("b") or {} for record in records]

    records = client.run(
        "MATCH (n) WHERE labels(n)[0] = $label RETURN n LIMIT $node_limit",
        label=LABEL_PHENOMENON,
        node_limit=limit,
    )
    return [record.get("n") or {} for record in records]


def _best_phenomenon(
    nodes: list[dict[str, Any]], fault_type: str, *, allow_fuzzy: bool
) -> tuple[dict[str, Any], str] | None:
    """在候选中选匹配分最高且达到阈值的现象，返回 ``(节点, 匹配用的现象名)``。

    同分时取名称更长（更具体）的现象，再按节点 id 稳定排序，避免依赖图的返回顺序。
    ``allow_fuzzy`` 为假时只接受故障码/名称精确匹配：设备没有类型时跨类型做包含匹配风险太高。
    """

    best: tuple[float, tuple[int, str], dict[str, Any]] | None = None
    for node in nodes:
        score = _phenomenon_score(node, fault_type, allow_fuzzy=allow_fuzzy)
        if score <= 0:
            continue
        key = (len(_normalize_text(node.get(PROP_PHENOMENON_NAME))), str(node.get("id") or ""))
        if best is None or score > best[0] or (score == best[0] and key > best[1]):
            best = (score, key, node)
    if best is None or best[0] < PHENOMENON_MATCH_THRESHOLD:
        return None
    node = best[2]
    return node, _clean(node.get(PROP_PHENOMENON_NAME)) or ""


def _phenomenon_score(node: dict[str, Any], fault_type: str, *, allow_fuzzy: bool) -> float:
    """现象匹配打分：故障码精确 > 名称精确 > 名称包含 > 二元组重叠。

    预测故障类型可能是“高温过热/局部放电”这类复合写法，按分隔符拆开后取各部分的最高分。
    """

    candidates = (fault_type, *_fault_type_parts(fault_type))
    return max((_score_text(node, text, allow_fuzzy=allow_fuzzy) for text in candidates), default=0.0)


def _score_text(node: dict[str, Any], text: str, *, allow_fuzzy: bool) -> float:
    """单个候选文本与现象的匹配分。"""

    wanted = _normalize_text(text)
    if not wanted:
        return 0.0
    if any(_normalize_text(code) == wanted for code in _split_items(node.get(PROP_FAULT_CODE))):
        return FAULT_CODE_EXACT_SCORE

    name = _normalize_text(node.get(PROP_PHENOMENON_NAME))
    if not name:
        return 0.0
    if name == wanted:
        return PHENOMENON_NAME_EXACT_SCORE
    if not allow_fuzzy:
        return 0.0
    if name in wanted or wanted in name:
        return PHENOMENON_NAME_CONTAINS_SCORE
    if _bigram_ratio(name, wanted) >= PHENOMENON_BIGRAM_MIN_RATIO:
        return PHENOMENON_BIGRAM_SCORE
    return 0.0


def _fault_type_parts(fault_type: str) -> list[str]:
    """把复合故障类型拆成子类型：“开关柜绝缘退化/局放” → 两项；拆分不出多项时返回空。"""

    pattern = "|".join(re.escape(separator) for separator in FAULT_TYPE_SEPARATORS)
    parts = [_clean(part) for part in re.split(pattern, fault_type)]
    unique = dedup([part for part in parts if part and part != fault_type])
    return unique if len(unique) > 1 else []


def _split_items(value: Any) -> list[str]:
    """维护知识字段 → 条目列表：数组、JSON 数组字符串、换行分隔文本三种写法都接受。

    图谱里的条目以换行为分隔；“注意事项”“处置措施说明”的编号条目内部也含分号，
    所以按换行切分后不再拆分号。只有整段没有换行时才按分号拆成多项。
    """

    if isinstance(value, (list, tuple)):
        return _compact(value)
    text = _clean(value) if isinstance(value, str) else None
    if text is None:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return _compact(parsed)
    lines = _compact(_LINE_SEPARATOR_PATTERN.split(text))
    if len(lines) == 1 and not NUMBERED_ITEM_PATTERN.match(lines[0]):
        return _compact(_ITEM_SEPARATOR_PATTERN.split(lines[0]))
    return lines


def _compact(values) -> list[str]:
    """逐项去掉空白并丢弃空项，保持原顺序。"""

    return [item for item in (_clean(value) for value in values) if item]


def _normalize_text(value: Any) -> str:
    """匹配用文本：全角转半角、去掉空白与标点、忽略大小写。"""

    return re.sub(r"[^\w\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def _bigram_ratio(left: str, right: str) -> float:
    """字符二元组重叠率；任一文本长度小于 2 时返回 0。"""

    if len(left) < 2 or len(right) < 2:
        return 0.0
    left_pairs = {left[index : index + 2] for index in range(len(left) - 1)}
    right_pairs = {right[index : index + 2] for index in range(len(right) - 1)}
    return len(left_pairs & right_pairs) / len(left_pairs | right_pairs)


# ---------------------------------------------------------------------------
# 查询语句（一条语句一个 MATCH；中文标签/属性不加反引号；用户输入只走参数）
# ---------------------------------------------------------------------------
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
    """设备 id → 设备类型名称（来自 ``属于`` 边）。

    kg5 没有设备类型层（或查询失败）时返回空映射：设备类型只是展示与加分项，
    不能因为它缺失而让整次设备召回失败。
    """

    ids = dedup(device_ids)
    if not ids:
        return {}
    try:
        records = client.run(
            "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND labels(b)[0] = $dst "
            "AND type(r) = $rel AND coalesce(a.id, '') IN $device_ids "
            "RETURN a, b LIMIT $node_limit",
            src=LABEL_DEVICE,
            dst=LABEL_DEVICE_TYPE,
            rel=REL_BELONGS,
            device_ids=ids,
            node_limit=KG_QUERY_LIMIT,
        )
    except TuGraphError as exc:
        logger.info(f"kg5 设备类型查询不可用，按无类型继续：{exc}")
        return {}
    types: dict[str, str] = {}
    for record in records:
        device_id = _clean((record.get("a") or {}).get("id"))
        device_type = _clean((record.get("b") or {}).get("名称"))
        if device_id and device_type and device_id not in types:
            types[device_id] = device_type
    return types


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
def _clean(value: Any) -> str | None:
    """空值/空白统一收敛为 None。"""

    if value is None:
        return None
    text = str(value).strip()
    return text or None
