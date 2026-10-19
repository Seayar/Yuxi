"""kg 图检索单元测试：全部使用假 kg 客户端，不连接图谱/数据库/大模型。

夹具取自 ``ref/kg_ref.json`` 的真实台账行（名称、id、层级路径、台账分类原样保留）与
``00_总体设计.md`` 第 4 节给出的新增节点（作业模板/安全距离/班组/备品备件，标注为演示数据）。
"""

from __future__ import annotations

import json

import pytest
from yuxi.knowledge.fault_trace.graph_kg import KgGraphError
from yuxi.knowledge.maintenance_plan.contracts import (
    LABEL_DEVICE,
    LABEL_SAFETY_DISTANCE,
    LABEL_STANDARD,
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
    PlanQuery,
)

from yuxi.knowledge.maintenance_plan.graph_plan import (
    _in_condition,
    _query_parents,
    _query_safety_distance,
    _resolve_voltage_level,
    _safety_distance_place,
    collect_plan_evidence,
    derive_outage_scope,
    parse_template,
    plan_graph_ready,
    search_devices,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 夹具：设备实例（ref/kg_ref.json → typed_devices / top_levels / elec）
# ---------------------------------------------------------------------------
DEVICES: list[dict] = [
    {
        "id": "LGR#04438230471b45ea9e5ea0cc09cfb524",
        "名称": "3k18",
        "Scheme1标签": "开关柜",
        "台账分类": "辅柜",
        "电压等级": "",
        "层级路径": "#3开闭所 > 3k18",
    },
    {
        "id": "LGR#6cf57b839a304b2998ab5af4ee6fe60c",
        "名称": "固定柜",
        "Scheme1标签": "开关柜",
        "台账分类": "电气和仪控屏柜",
        "电压等级": "",
        "层级路径": "#3开闭所 > 3k18 > 固定柜",
    },
    {
        "id": "LGR#02e6bf1edf6a4bb18449cf360a61db88",
        "名称": "992SVG开关",
        "Scheme1标签": "断路器",
        "台账分类": "高压断路器",
        "电压等级": "10kV",
        "层级路径": "变电设施 > 35kV#1变电站 > 10kV电压等级区域 > 二号无功补偿_992 > 992SVG柜 > 992SVG开关",
    },
    {
        "id": "LGR#595ec0247ca8414fae687b6b44a5a38b",
        "名称": "992SVG开关",
        "Scheme1标签": "断路器",
        "台账分类": "高压断路器",
        "电压等级": "10kV",
        "层级路径": "变电设施 > 35kV#2变电站 > 10kV电压等级区域 > 二号无功补偿_992 > 992SVG柜 > 992SVG开关",
    },
    {
        "id": "LGR#7c2fb1be1c9945f89bf2de5efa099704",
        "名称": "#1主变压器",
        "Scheme1标签": "变压器",
        "台账分类": "SF6变压器",
        "电压等级": "35kV",
        "层级路径": "变电设施 > 35kV#2变电站 > 主变区域 > #1主变间隔 > #1主变压器",
    },
    {
        "id": "LGR#41eded5d45a14247ab34c88450d1bcc5",
        "名称": "#1主变_951开关柜",
        "Scheme1标签": "开关柜",
        "台账分类": "所用断路器间隔/开关柜",
        "电压等级": "10kV",
        "层级路径": "变电设施 > 35kV#3变电站 > 10kV电压等级区域 > #1主变10kV侧_951 > #1主变_951开关柜",
    },
    {
        "id": "LGR#demo_main1",
        "名称": "1号主变",
        "Scheme1标签": "变压器",
        "台账分类": "主变压器",
        "电压等级": "35kV",
        "层级路径": "变电设施 > 35kV#1变电站 > 主变区域 > 1号主变",
    },
    {
        "id": "LGR#demo_bx78",
        "名称": "#78箱变",
        "Scheme1标签": "",
        "台账分类": "箱变（组合式）",
        "电压等级": "10kV",
        "层级路径": "箱变群3-7 > #78箱变",
    },
    {
        "id": "LGR#demo_bx13",
        "名称": "箱变13馈出柜",
        "Scheme1标签": "开关柜",
        "台账分类": "断路器柜",
        "电压等级": "",
        "层级路径": "箱变群3-6 > #62箱变 > 10kV电压等级区域 > 箱变13馈出柜",
    },
    {
        "id": "LGR#demo_line1",
        "名称": "1B-S9",
        "Scheme1标签": "配电线路",
        "台账分类": "中压线路",
        "电压等级": "",
        "层级路径": "1B-S9",
    },
    {
        # Scheme1标签 为空且台账分类不可作业 → 必须被过滤（ref/kg_schema.md 的 2000+ 区域/监测节点）
        "id": "LGR#demo_online1",
        "名称": "3k18局放在线监测",
        "Scheme1标签": "",
        "台账分类": "在线监测装置",
        "电压等级": "",
        "层级路径": "#3开闭所 > 3k18 > 在线监测",
    },
    {
        "id": "LGR#reg_kbs3",
        "名称": "#3开闭所",
        "Scheme1标签": "开闭所",
        "台账分类": "开闭所",
        "电压等级": "",
        "层级路径": "#3开闭所",
    },
    {
        "id": "LGR#reg_ss1",
        "名称": "35kV#1变电站",
        "Scheme1标签": "变电站",
        "台账分类": "变电站",
        "电压等级": "35kV",
        "层级路径": "变电设施 > 35kV#1变电站",
    },
    {
        "id": "LGR#demo_elec_up",
        "名称": "110kV进线",
        "Scheme1标签": "",
        "台账分类": "",
        "电压等级": "",
        "层级路径": "110kV进线",
    },
    {
        "id": "LGR#demo_elec_down",
        "名称": "10kV开闭所",
        "Scheme1标签": "",
        "台账分类": "",
        "电压等级": "",
        "层级路径": "10kV开闭所",
    },
    {
        # T12a：线路类设备（杆塔）+ 10kV，用来验证安全距离取“线路”场所（DL/T 409）
        "id": "LGR#line_bn18",
        "名称": "BN18杆塔",
        "Scheme1标签": "杆塔",
        "台账分类": "杆塔单元",
        "电压等级": "10kV",
        "层级路径": "输电设施 > 敖一线 > BN18杆塔",
    },
    {
        # T12a：线路类设备 + 35kV（夹具里只有 35kV 变电记录）→ 触发“场所缺失回退”
        "id": "LGR#line_t35",
        "名称": "T35杆塔",
        "Scheme1标签": "杆塔",
        "台账分类": "杆塔单元",
        "电压等级": "35kV",
        "层级路径": "输电设施 > 集电线路35 > T35杆塔",
    },
    {
        # T12a 反例：自身电压为空、路径里也没有 kV，只能从父节点 #3开闭所 的 电压等级 属性取
        "id": "LGR#parent_vol_3k19",
        "名称": "3k19",
        "Scheme1标签": "断路器",
        "台账分类": "高压断路器",
        "电压等级": "",
        "层级路径": "#3开闭所 > 3k19",
    },
    {
        # T12a 优先级构造用例：直接父节点（位于）是 35kV#3变电站（真实属性 35kV），
        # 层级路径里还有一个名称带 kV 的中间区域节点（10kV电压等级区域）。
        "id": "LGR#prec_35",
        "名称": "PX开关",
        "Scheme1标签": "断路器",
        "台账分类": "高压断路器",
        "电压等级": "",
        "层级路径": "变电设施 > 35kV#3变电站 > 10kV电压等级区域 > PX开关",
    },
]

# 位于：子 → 父（ref/kg_ref.json → locate_samples）；第三项是父节点真实的 电压等级 属性（无则 None）。
# 真实台账里设备自身电压常为空、父节点名称也不带 kV（如 #3开闭所 的 电压等级=10kV，
# 记录 id LGR#093f1ffe560b4ef382e701cd4911b8b3），只能从父节点属性取。
LOCATED_IN = [
    ("LGR#04438230471b45ea9e5ea0cc09cfb524", "#3开闭所", None),
    ("LGR#6cf57b839a304b2998ab5af4ee6fe60c", "3k18", None),
    ("LGR#02e6bf1edf6a4bb18449cf360a61db88", "35kV#1变电站", "35kV"),
    ("LGR#595ec0247ca8414fae687b6b44a5a38b", "35kV#2变电站", "35kV"),
    ("LGR#7c2fb1be1c9945f89bf2de5efa099704", "#1主变间隔", None),
    ("LGR#parent_vol_3k19", "#3开闭所", "10kV"),
    ("LGR#prec_35", "35kV#3变电站", "35kV"),
]

# 电连接：a 为电源侧（ref/kg_ref.json → elec）
ELECTRIC = [("110kV进线", "1号主变"), ("1号主变", "10kV开闭所")]

# 作业模板（00_总体设计.md 第 4 节示例字段，数据来源标注为演示数据）
TEMPLATES: list[dict] = [
    {
        "id": "TPL#开关柜#更换#断路器",
        "名称": "开关柜断路器更换",
        "作业类型": "更换",
        "设备类型": "开关柜",
        "作业对象": "断路器",
        "适用关键词": json.dumps(["断路器", "开关", "手车"], ensure_ascii=False),
        "停电要求": "需停电",
        "安全隔离措施": "断开{上级电源}对应出线开关并上锁，在{设备名称}电缆侧及断路器两侧分别验电、挂接地线，悬挂“禁止合闸，有人工作”标示牌，设专人监护。",
        "工序顺序": json.dumps(
            [
                "断开电源",
                "拆除二次线",
                "拆旧断路器",
                "安装新断路器",
                "恢复二次接线",
                "手动/电动操作试验",
                "回路电阻与绝缘测量",
                "恢复送电",
            ],
            ensure_ascii=False,
        ),
        "操作要点": json.dumps(["拆二次线前逐芯编号并拍照", "触头涂抹适量导电膏"], ensure_ascii=False),
        "技术参数要求": json.dumps(["回路电阻不大于制造厂规定值", "主回路绝缘电阻≥1000MΩ"], ensure_ascii=False),
        "个人防护要求": "穿戴{电压等级}绝缘手套、绝缘鞋、安全帽。",
        "验电接地要求": "手车摇进摇出前必须确认断路器处于分闸位置。",
        "作业安全要求": json.dumps(["设专人监护", "禁止单人作业"], ensure_ascii=False),
        "工器具清单": json.dumps(["力矩扳手（0–25N·m）", "回路电阻测试仪（100A）"], ensure_ascii=False),
        "材料清单": json.dumps(["导电膏", "同型号备用断路器（1台）"], ensure_ascii=False),
        "验收标准": json.dumps(["手动分合闸3次可靠动作", "主回路绝缘电阻≥1000MΩ"], ensure_ascii=False),
        "注意事项": json.dumps(["恢复送电前清点工器具"], ensure_ascii=False),
        "人员配置建议": "4人：工作负责人1人、作业人员2人、专责监护人1人",
        "来源说明": "演示数据：工序与操作要点依据 DL/T 408—2023 第7章",
    },
    {
        "id": "TPL#开关柜#更换",
        "名称": "开关柜更换通用",
        "作业类型": "更换",
        "设备类型": "开关柜",
        "作业对象": "整体",
        "适用关键词": json.dumps(["开关柜"], ensure_ascii=False),
        "停电要求": "需停电",
        "安全隔离措施": "断开{上级电源}对应出线开关并上锁。",
        "工序顺序": json.dumps(["停电验电", "拆除旧设备", "安装新设备", "恢复送电"], ensure_ascii=False),
        "来源说明": "演示数据：通用更换工序",
    },
    {
        "id": "TPL#变压器#检修",
        "名称": "变压器检修",
        "作业类型": "检修",
        "设备类型": "变压器",
        "作业对象": "",
        "适用关键词": json.dumps(["变压器", "主变"], ensure_ascii=False),
        "停电要求": "需停电",
        "工序顺序": json.dumps(["停电验电接地", "外观与紧固检查", "绝缘测量"], ensure_ascii=False),
        "来源说明": "演示数据：通用检修工序",
    },
    {
        # 工序顺序故意不是合法 JSON，适用关键词用分号分隔，用来验证降级拆分
        "id": "TPL#开关柜#巡视",
        "名称": "开关柜巡视",
        "作业类型": "巡视",
        "设备类型": "开关柜",
        "作业对象": "整体",
        "适用关键词": "断路器；开关",
        "停电要求": "不停电",
        "工序顺序": "外观检查\n测温检查",
        "来源说明": "演示数据：通用巡视工序",
    },
]

TEMPLATE_CLAUSES = [("TPL#开关柜#更换#断路器", "DLT408#7.3.1")]

CLAUSE_BODY = (
    "检修设备停电后，应断开各方面电源，且各方面至少有一个明显的断开点，禁止在只经断路器断开电源的设备上工作。"
    "作业人员应在停电设备的两侧以及可能来电的各方面分别验电，确认无电压后立即装设接地线。"
    "装设接地线应由两人进行，先装接地端后装导体端，拆除时顺序相反；接地线截面应满足短路电流热稳定要求。"
    "禁止工作人员擅自移动或拆除遮栏、标示牌，禁止越过遮栏工作。"
    "工作中如需变更安全措施，必须重新履行工作票手续，并经工作负责人同意后方可进行。"
)

CLAUSES = [
    {
        "clause_id": "DLT408#7.3.1",
        "standard_id": "DLT408",
        "条款号": "7.3.1",
        "条款标题": "停电、验电、接地",
        "条款内容": CLAUSE_BODY,
    }
]

STANDARDS = [
    {
        "standard_id": "DLT408",
        "标准编号": "DL/T 408—2023",
        "标准名称": "电力安全工作规程 发电厂和变电站电气部分",
    }
]

SAFETY_DISTANCES = [
    {
        "id": "SD#10kV#变电",
        "电压等级": "10kV",
        "适用场所": "变电",
        "设备不停电安全距离_m": 0.7,
        "工作中正常活动范围安全距离_m": 0.35,
        "带电作业安全距离_m": 0.4,
        "依据标准": "DL/T 408—2023",
        "条款号": "表1",
        "备注": "0.35 m 仅适用于已采取绝缘遮蔽或安全遮栏措施的情况。",
    },
    {
        "id": "SD#10kV#线路",
        "电压等级": "10kV",
        "适用场所": "线路",
        "设备不停电安全距离_m": 0.7,
        "工作中正常活动范围安全距离_m": 0.35,
        "带电作业安全距离_m": 0.4,
        "依据标准": "DL/T 409—2023",
        "条款号": "表1",
    },
    {
        # 故意只给“变电”场所，用来验证“指定场所缺失时退回同电压任一条”
        "id": "SD#35kV#变电",
        "电压等级": "35kV",
        "适用场所": "变电",
        "设备不停电安全距离_m": 1.0,
        "工作中正常活动范围安全距离_m": 0.6,
        "带电作业安全距离_m": 0.6,
        "依据标准": "DL/T 408—2023",
        "条款号": "表1",
    },
]

TEAMS = [
    {"id": "TEAM#开闭所运维班", "名称": "#3开闭所运维班", "班组类型": "运维", "人数": 4, "负责人": "李工", "数据来源": "演示数据"},
    {"id": "TEAM#检修班", "名称": "检修一班", "班组类型": "检修", "人数": 6, "负责人": "王工", "数据来源": "演示数据"},
    {"id": "TEAM#试验班", "名称": "试验班", "班组类型": "试验", "人数": 3, "负责人": "赵工", "数据来源": "演示数据"},
]

TEAM_MANAGES = [
    ("TEAM#开闭所运维班", "#3开闭所"),
    ("TEAM#检修班", "#3开闭所"),
    ("TEAM#试验班", "35kV#1变电站"),
]

SPARES = [
    {
        "id": "SPARE#开关柜#断路器",
        "名称": "同型号备用断路器VS1-12/1250-25",
        "型号规格": "VS1-12/1250-25",
        "库存数量": 1,
        "存放地点": "#3开闭所备品间",
        "数据来源": "演示数据",
    },
    {
        "id": "SPARE#开关柜#触头",
        "名称": "开关柜二次插件",
        "型号规格": "CT-10",
        "库存数量": 6,
        "存放地点": "#3开闭所备品间",
        "数据来源": "演示数据",
    },
]

SPARE_EDGES = [("开关柜", "SPARE#开关柜#断路器"), ("开关柜", "SPARE#开关柜#触头")]

TEST_ITEMS = [
    {"设备类型": "开关柜", "项目名称": "主回路电阻测量"},
    {"设备类型": "开关柜", "项目名称": "二次回路交流耐压试验"},
    {"设备类型": "断路器", "项目名称": "断路器机械特性试验"},
]

PATROL_ITEMS = [
    {"设备类型": "开关柜", "项目名称": "柜内有无异响与放电痕迹"},
    {"设备类型": "断路器", "项目名称": "断路器外观检查"},
]


# ---------------------------------------------------------------------------
# 假 kg 客户端
# ---------------------------------------------------------------------------
def _device_type_of(props: dict) -> str | None:
    """按 contracts 的归一表把 Scheme1标签 归一到 kg 设备类型名称。"""

    scheme1 = str(props.get("Scheme1标签") or "").strip()
    if not scheme1:
        return None
    return SCHEME1_TO_DEVICE_TYPE.get(scheme1, scheme1)


def _node_by_name(name: str) -> dict | None:
    for props in DEVICES:
        if props.get("名称") == name:
            return props
    return None


class FakeKgClient:
    """替身 kg 客户端：按 Cypher 里的标签/关系关键词返回夹具数据，并记录收到的语句。"""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []

    def run(self, cypher: str, **params) -> list[dict]:
        self.queries.append((cypher, params))
        return self._respond(cypher, params)

    def _respond(self, cypher: str, params: dict) -> list[dict]:
        relation = params.get("rel")

        if "CONTAINS" in cypher and params.get("label") == LABEL_DEVICE:
            keyword = params["kw"]
            props_keys = ("名称", "显示名", "name", "层级路径")
            return [
                {"n": props}
                for props in DEVICES
                if any(keyword in str(props.get(key) or "") for key in props_keys)
            ]
        if relation == REL_BELONGS:
            return [{"a": props} for props in DEVICES if _device_type_of(props) == params["device_type"]]
        if relation == REL_LOCATED_IN:
            return [
                {
                    "b": {
                        "id": f"LGR#reg#{parent}",
                        "名称": parent,
                        **({"电压等级": voltage} if voltage else {}),
                    }
                }
                for child_id, parent, voltage in LOCATED_IN
                if child_id == params["device_id"]
            ]
        if relation == REL_ELECTRIC:
            records = []
            for source_name, target_name in ELECTRIC:
                source, target = _node_by_name(source_name), _node_by_name(target_name)
                if source and target:
                    records.append({"a": source, "b": target})
            return records
        if relation == REL_HAS_TEMPLATE:
            return [{"b": props} for props in TEMPLATES if props.get("设备类型") == params["device_type"]]
        if relation == REL_TEMPLATE_CLAUSE:
            return [
                {"b": props}
                for template_id, clause_id in TEMPLATE_CLAUSES
                if template_id == params["template_id"]
                for props in CLAUSES
                if props["clause_id"] == clause_id
            ]
        if params.get("label") == LABEL_STANDARD:
            return [{"n": props} for props in STANDARDS if props["standard_id"] in params["standard_ids"]]
        if relation in (REL_NEEDS_TEST, REL_NEEDS_PATROL):
            items = TEST_ITEMS if relation == REL_NEEDS_TEST else PATROL_ITEMS
            return [{"b": props} for props in items if props["设备类型"] == params["device_type"]]
        if params.get("label") == LABEL_SAFETY_DISTANCE:
            place = params.get("place")
            return [
                {"n": props}
                for props in SAFETY_DISTANCES
                if props["电压等级"] == params["voltage_level"]
                and (place is None or props.get("适用场所") == place)
            ]
        if relation == REL_MANAGES:
            records = []
            for team_id, area in TEAM_MANAGES:
                if area not in params["area_names"]:
                    continue
                team = next(props for props in TEAMS if props["id"] == team_id)
                records.append({"a": team, "b": _node_by_name(area) or {"名称": area}})
            return records
        if relation == REL_SPARE:
            return [
                {"b": spare}
                for device_type, spare_id in SPARE_EDGES
                if device_type == params["device_type"]
                for spare in SPARES
                if spare["id"] == spare_id
            ]
        return []


@pytest.fixture
def client() -> FakeKgClient:
    return FakeKgClient()


def _device(name: str) -> DeviceCandidate:
    """按夹具里的设备实例构造一个 DeviceCandidate（跳过检索，直接测证据收集）。"""

    props = _node_by_name(name)
    assert props is not None, f"夹具里没有设备 {name}"
    return DeviceCandidate(
        device_id=props["id"],
        name=props["名称"],
        device_type=_device_type_of(props),
        voltage_level=props.get("电压等级") or None,
        path=props["层级路径"],
        ledger_class=props.get("台账分类"),
        score=100.0,
    )


# ---------------------------------------------------------------------------
# search_devices
# ---------------------------------------------------------------------------
def test_search_devices_keeps_duplicate_names_with_distinct_paths(client: FakeKgClient):
    """重名设备（992SVG开关 在 35kV#1 与 #2 变电站各一个）返回两个候选，路径不同。"""

    query = PlanQuery(raw_text="更换992SVG开关", task_type=TASK_REPLACE, device_name="992SVG开关", device_type="断路器")

    candidates = search_devices(client, query)

    assert len(candidates) == 2
    assert {candidate.name for candidate in candidates} == {"992SVG开关"}
    assert len({candidate.path for candidate in candidates}) == 2
    assert {candidate.score for candidate in candidates} == {120.0}  # 名称精确相等 100 + 设备类型一致 20
    assert all("35kV#1变电站" in path or "35kV#2变电站" in path for path in (c.path for c in candidates))


def test_search_devices_exact_hit_for_3k18(client: FakeKgClient):
    """“#3开闭所 3k18”：精确命中台账节点，在线监测节点被过滤，路径召回排在其后。"""

    query = PlanQuery(
        raw_text="更换#3开闭所3k18断路器",
        task_type=TASK_REPLACE,
        device_name="3k18",
        location_hint="#3开闭所",
        device_type="开关柜",
    )

    candidates = search_devices(client, query)

    assert candidates[0].name == "3k18"
    assert candidates[0].path == "#3开闭所 > 3k18"
    assert candidates[0].score == 150.0  # 100 + 位置线索 30 + 设备类型 20
    assert candidates[0].device_type == "开关柜"
    assert "3k18局放在线监测" not in {candidate.name for candidate in candidates}
    assert candidates[1].name == "固定柜"  # 仅层级路径命中（30 + 位置 30 + 类型 20）


def test_search_devices_expands_ordinal_and_abbreviation(client: FakeKgClient):
    """“1号主变”通过同义展开召回台账里的 #1 变压器类设备。"""

    query = PlanQuery(
        raw_text="检修1号主变",
        task_type=TASK_REPAIR,
        device_name="1号主变",
        device_type="变压器",
        voltage_level="35kV",
    )

    candidates = search_devices(client, query)

    names = {candidate.name for candidate in candidates}
    assert "#1主变压器" in names
    assert candidates[0].device_type == "变压器"
    assert all(candidate.device_type == "变压器" for candidate in candidates[:2])


def test_search_devices_recalls_by_device_type_without_keywords(client: FakeKgClient):
    """用户只说“开关柜”时按 属于 关系召回该设备类型下的设备。"""

    query = PlanQuery(raw_text="更换开关柜", task_type=TASK_REPLACE, device_type="开关柜")

    candidates = search_devices(client, query)

    assert candidates
    assert {candidate.device_type for candidate in candidates} == {"开关柜"}
    assert {candidate.name for candidate in candidates} == {"#1主变_951开关柜", "3k18", "固定柜", "箱变13馈出柜"}
    assert [params["rel"] for _, params in client.queries] == [REL_BELONGS]


def test_search_devices_respects_limit(client: FakeKgClient):
    query = PlanQuery(raw_text="更换开关柜", task_type=TASK_REPLACE, device_type="开关柜")

    assert len(search_devices(client, query, limit=1)) == 1
    assert len(search_devices(client, query)) == 4


def test_search_devices_without_keywords_or_type_returns_empty(client: FakeKgClient):
    """既没有设备名称/位置线索，也没有设备类型时不查图，直接返回空。"""

    candidates = search_devices(client, PlanQuery(raw_text="更换"))

    assert candidates == []
    assert client.queries == []


def test_search_devices_normalizes_scheme1_label_to_device_type(client: FakeKgClient):
    """台账 Scheme1标签 里的 配电线路 归一到 kg 的 架空线路。"""

    query = PlanQuery(raw_text="巡视线路", task_type=TASK_PATROL, device_type="架空线路")

    candidates = search_devices(client, query)

    assert [candidate.name for candidate in candidates] == ["1B-S9"]
    assert candidates[0].device_type == "架空线路"


# ---------------------------------------------------------------------------
# Cypher 约束
# ---------------------------------------------------------------------------
def test_queries_are_tugraph_compliant(client: FakeKgClient):
    """每条语句只有一个 MATCH、没有反引号、带 LIMIT，用户输入只出现在参数里。"""

    query = PlanQuery(
        raw_text="更换#3开闭所3k18断路器",
        task_type=TASK_REPLACE,
        device_name="3k18",
        location_hint="#3开闭所",
        device_type="开关柜",
        work_object="断路器",
    )
    device = search_devices(client, query)[0]

    collect_plan_evidence(client, query, device)

    assert client.queries
    for cypher, params in client.queries:
        assert cypher.count("MATCH") == 1
        assert "`" not in cypher
        assert "LIMIT $node_limit" in cypher
        assert "3k18" not in cypher
        assert params["node_limit"] <= 200
    assert any(params.get("kw") == "3k18" for _, params in client.queries)


# ---------------------------------------------------------------------------
# collect_plan_evidence：模板
# ---------------------------------------------------------------------------
def test_collect_evidence_uses_object_specific_template(client: FakeKgClient):
    query = PlanQuery(
        raw_text="更换#3开闭所3k18断路器",
        task_type=TASK_REPLACE,
        device_name="3k18",
        location_hint="#3开闭所",
        device_type="开关柜",
        work_object="断路器",
    )
    device = search_devices(client, query)[0]

    evidence = collect_plan_evidence(client, query, device)

    assert evidence.template is not None
    assert evidence.template.template_id == "TPL#开关柜#更换#断路器"
    assert len(evidence.template.step_sequence) == 8
    assert evidence.template.source_note.startswith("演示数据")
    assert any("含 8 道工序" in record for record in evidence.retrieval_log)


def test_collect_evidence_falls_back_to_generic_template(client: FakeKgClient):
    """作业对象在模板里没有对应项时退回“作业对象=整体”的通用模板。"""

    query = PlanQuery(
        raw_text="更换#3开闭所3k18母线",
        task_type=TASK_REPLACE,
        device_name="3k18",
        location_hint="#3开闭所",
        device_type="开关柜",
        work_object="母线",
    )
    device = search_devices(client, query)[0]

    evidence = collect_plan_evidence(client, query, device)

    assert evidence.template is not None
    assert evidence.template.template_id == "TPL#开关柜#更换"
    assert any("退回通用模板" in record for record in evidence.retrieval_log)


def test_collect_evidence_without_matching_template(client: FakeKgClient):
    """该设备类型+作业类型下没有模板时 template 为 None，不编造内容。"""

    query = PlanQuery(
        raw_text="试验#3开闭所3k18",
        task_type=TASK_TEST,
        device_name="3k18",
        location_hint="#3开闭所",
        device_type="开关柜",
    )
    device = search_devices(client, query)[0]

    evidence = collect_plan_evidence(client, query, device)

    assert evidence.template is None
    assert any("未检索到设备类型" in record for record in evidence.retrieval_log)


def test_collect_evidence_without_device_type_skips_template_and_items(client: FakeKgClient):
    device = DeviceCandidate(device_id="LGR#x", name="未知设备", device_type=None, voltage_level=None, path="")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="更换未知设备", task_type=TASK_REPLACE), device)

    assert evidence.template is None
    assert evidence.test_items == []
    assert evidence.spares == []
    assert any("缺少设备类型或作业类型" in record for record in evidence.retrieval_log)


def test_collect_evidence_clauses_use_standard_number_and_truncate_excerpt(client: FakeKgClient):
    query = PlanQuery(raw_text="更换#3开闭所3k18断路器", task_type=TASK_REPLACE, device_name="3k18", device_type="开关柜", work_object="断路器")
    device = search_devices(client, query)[0]

    evidence = collect_plan_evidence(client, query, device)

    assert evidence.template is not None
    clause = evidence.template.clauses[0]
    assert (clause.standard_no, clause.clause_no) == ("DL/T 408—2023", "7.3.1")
    assert clause.title == "停电、验电、接地"
    assert len(clause.excerpt) == 200
    assert len(CLAUSE_BODY) > 200


# ---------------------------------------------------------------------------
# collect_plan_evidence：层级、停电范围、电压与安全距离
# ---------------------------------------------------------------------------
def test_collect_evidence_derives_ancestors_outage_scope_and_safety_distance(client: FakeKgClient):
    query = PlanQuery(raw_text="更换992SVG开关", task_type=TASK_REPLACE, device_name="992SVG开关", device_type="断路器")
    device = search_devices(client, query)[0]

    evidence = collect_plan_evidence(client, query, device)

    assert evidence.ancestors == ["992SVG柜", "二号无功补偿_992", "10kV电压等级区域", "35kV#1变电站", "变电设施"]
    assert evidence.outage_scope == ["992SVG开关", "992SVG柜", "35kV#1变电站"]
    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert (evidence.safety_distance.no_outage_m, evidence.safety_distance.work_range_m) == (0.7, 0.35)
    assert evidence.safety_distance.clause is not None
    assert evidence.safety_distance.clause.standard_no == "DL/T 408—2023"


def test_collect_evidence_uses_ancestor_voltage_when_device_has_none(client: FakeKgClient):
    """设备自身没有电压等级时，用最近一个带电压的上级节点（10kV电压等级区域）取安全距离。"""

    device = _device("箱变13馈出柜")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="检修箱变13馈出柜", task_type=TASK_REPAIR), device)

    assert device.voltage_level is None
    assert evidence.ancestors == ["10kV电压等级区域", "#62箱变", "箱变群3-6"]
    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert evidence.safety_distance.no_outage_m == 0.7


def test_collect_evidence_without_any_voltage_leaves_safety_distance_empty(client: FakeKgClient):
    """设备与上级节点都没有电压等级时安全距离留空，并在检索记录里说明。

    父节点（位于 关系）是查到了的，只是没有 电压等级 属性，名称里也没有 kV —— 属“确实无电压”。
    """

    device = _device("3k18")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="巡视3k18", task_type=TASK_PATROL), device)

    assert any(params.get("rel") == REL_LOCATED_IN for _, params in client.queries)
    assert evidence.ancestors == ["#3开闭所"]
    assert evidence.safety_distance is None
    assert any("未检索安全距离" in record for record in evidence.retrieval_log)


def test_collect_evidence_adds_upstream_electrical_neighbor_to_outage_scope(client: FakeKgClient):
    """拓扑为空时按层级推导，有 电连接 时把上游邻居加入停电范围。"""

    device = _device("1号主变")
    query = PlanQuery(raw_text="检修1号主变", task_type=TASK_REPAIR, device_name="1号主变", device_type="变压器")

    evidence = collect_plan_evidence(client, query, device)

    assert evidence.electrical_neighbors == ["110kV进线", "10kV开闭所"]
    assert evidence.outage_scope == ["1号主变", "35kV#1变电站", "110kV进线"]


# ---------------------------------------------------------------------------
# collect_plan_evidence：班组与备件
# ---------------------------------------------------------------------------
def test_collect_evidence_team_inherits_from_ancestor_with_type_preference(client: FakeKgClient):
    """班组沿祖先继承（设备本身没有管辖关系），同区域多班组按作业类型偏好挑选。"""

    replace_query = PlanQuery(raw_text="更换#3开闭所3k18", task_type=TASK_REPLACE, device_name="3k18", device_type="开关柜")
    patrol_query = PlanQuery(raw_text="巡视#3开闭所3k18", task_type=TASK_PATROL, device_name="3k18", device_type="开关柜")
    test_query = PlanQuery(raw_text="试验992SVG开关", task_type=TASK_TEST, device_name="992SVG开关", device_type="断路器")

    replace_team = collect_plan_evidence(client, replace_query, search_devices(client, replace_query)[0]).team
    patrol_team = collect_plan_evidence(client, patrol_query, search_devices(client, patrol_query)[0]).team
    test_team = collect_plan_evidence(client, test_query, search_devices(client, test_query)[0]).team

    assert replace_team is not None and replace_team.name == "检修一班"  # 更换偏好检修
    assert replace_team.managed_area == "#3开闭所"
    assert replace_team.member_count == 6
    assert replace_team.leader == "王工"
    assert replace_team.source_note == "演示数据"
    assert patrol_team is not None and patrol_team.name == "#3开闭所运维班"  # 巡视偏好运维
    assert test_team is not None and test_team.name == "试验班"  # 试验偏好试验，且挂在 35kV#1变电站


def test_collect_evidence_spares_prefer_work_object(client: FakeKgClient):
    query = PlanQuery(
        raw_text="更换#3开闭所3k18断路器",
        task_type=TASK_REPLACE,
        device_name="3k18",
        device_type="开关柜",
        work_object="断路器",
    )
    device = search_devices(client, query)[0]

    evidence = collect_plan_evidence(client, query, device)

    assert [spare.name for spare in evidence.spares] == ["同型号备用断路器VS1-12/1250-25", "开关柜二次插件"]
    assert evidence.spares[0].quantity == 1
    assert evidence.spares[0].location == "#3开闭所备品间"
    assert evidence.spares[0].source_note == "演示数据"


def test_collect_evidence_collects_test_and_patrol_items(client: FakeKgClient):
    query = PlanQuery(raw_text="试验992SVG开关", task_type=TASK_TEST, device_name="992SVG开关", device_type="断路器")
    device = search_devices(client, query)[0]

    evidence = collect_plan_evidence(client, query, device)

    assert evidence.test_items == ["断路器机械特性试验"]
    assert evidence.patrol_items == ["断路器外观检查"]


def test_collect_evidence_writes_human_readable_retrieval_log(client: FakeKgClient):
    query = PlanQuery(
        raw_text="更换#3开闭所3k18断路器",
        task_type=TASK_REPLACE,
        device_name="3k18",
        device_type="开关柜",
        work_object="断路器",
    )
    device = search_devices(client, query)[0]

    evidence = collect_plan_evidence(client, query, device)

    assert evidence.retrieval_log
    assert any("停电范围按台账层级推导" in record for record in evidence.retrieval_log)
    assert any("找到管辖班组" in record for record in evidence.retrieval_log)
    assert all(isinstance(record, str) and record for record in evidence.retrieval_log)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
def test_parse_template_parses_json_arrays_and_tolerates_bad_json():
    props = {
        "id": "TPL#x",
        "名称": "开关柜巡视",
        "作业类型": TASK_PATROL,
        "设备类型": "开关柜",
        "作业对象": "整体",
        "工序顺序": "外观检查\n测温检查",
        "工器具清单": "扳手；万用表",
        "验收标准": "",
        "注意事项": ["恢复送电前清点工器具"],
        "适用关键词": "断路器；开关",
    }

    template = parse_template(props, [ClauseRef(standard_no="DL/T 408—2023", clause_no="7.3.1")])

    assert template.template_id == "TPL#x"
    assert template.step_sequence == ["外观检查", "测温检查"]
    assert template.tools == ["扳手", "万用表"]
    assert template.notes == ["恢复送电前清点工器具"]
    assert template.acceptance == []
    assert template.work_object == "整体"
    assert template.clauses[0].clause_no == "7.3.1"


def test_parse_template_handles_missing_and_invalid_fields():
    template = parse_template({"id": "TPL#y", "工序顺序": "[]", "操作要点": "123"}, [])

    assert template.name == ""
    assert template.step_sequence == []
    assert template.key_points == []  # 非数组也非字符串 → 忽略，不抛异常
    assert template.clauses == []


def test_derive_outage_scope_rules():
    device = DeviceCandidate(
        device_id="d",
        name="992SVG开关",
        device_type="断路器",
        voltage_level="10kV",
        path="变电设施 > 35kV#1变电站 > 10kV电压等级区域 > 二号无功补偿_992 > 992SVG柜 > 992SVG开关",
    )
    ancestors = ["992SVG柜", "二号无功补偿_992", "10kV电压等级区域", "35kV#1变电站", "变电设施"]

    assert derive_outage_scope(device, ancestors, []) == ["992SVG开关", "992SVG柜", "35kV#1变电站"]
    assert derive_outage_scope(device, ancestors, ["110kV进线"]) == [
        "992SVG开关",
        "992SVG柜",
        "35kV#1变电站",
        "110kV进线",
    ]
    assert derive_outage_scope(device, ["#3开闭所"], ["#3开闭所"]) == ["992SVG开关", "#3开闭所"]


def test_derive_outage_scope_does_not_mistake_interval_for_power_source():
    """箱变馈出柜的“箱变13馈出柜”是所在间隔，上级电源取 #62箱变。"""

    device = DeviceCandidate(
        device_id="d",
        name="#62箱变13馈出柜",
        device_type="开关柜",
        voltage_level="10kV",
        path="箱变群3-6 > #62箱变 > 10kV电压等级区域 > 箱变13馈出柜 > #62箱变13馈出柜",
    )
    ancestors = ["箱变13馈出柜", "10kV电压等级区域", "#62箱变", "箱变群3-6"]

    assert derive_outage_scope(device, ancestors, []) == ["#62箱变13馈出柜", "箱变13馈出柜", "#62箱变"]


def test_derive_outage_scope_for_device_without_ancestors():
    device = DeviceCandidate(device_id="d", name="#3开闭所", device_type="开闭所", voltage_level=None, path="#3开闭所")

    assert derive_outage_scope(device, [], []) == ["#3开闭所"]


# ---------------------------------------------------------------------------
# 图谱可用性探测
# ---------------------------------------------------------------------------
def test_plan_graph_ready_returns_true_when_query_succeeds(client: FakeKgClient):
    assert plan_graph_ready(client) is True


def test_plan_graph_ready_returns_false_when_graph_unavailable():
    class BrokenClient:
        def run(self, cypher: str, **params) -> list[dict]:
            raise KgGraphError("无法连接故障溯源知识图谱")

    assert plan_graph_ready(BrokenClient()) is False


# ---------------------------------------------------------------------------
# T12a 新增：安全距离按“适用场所”选取（变电 / 线路 / 场所缺失回退）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("device_type", "path", "expected"),
    [
        ("架空线路", "输电设施 > 敖一线 > 1B-1K-1", "线路"),
        ("杆塔", "输电设施 > 敖一线 > BN18杆塔", "线路"),
        ("电缆", "箱变群3-6 > 电缆沟 > 电缆", "线路"),
        ("绝缘子", "输电设施 > 敖一线 > 绝缘子", "线路"),
        ("断路器", "变电设施 > 35kV#1变电站 > 992SVG开关", "变电"),
        ("开关柜", "变电设施 > #3开闭所 > 3k18", "变电"),
        ("变压器", "变电设施 > 35kV#2变电站 > #1主变压器", "变电"),
        # 设备类型本身是变电类，但层级路径带“集电线路/输电”时仍按线路
        ("开关柜", "输电设施 > 集电线路1 > 开关柜", "线路"),
        ("变压器", "输电走廊 > 主变", "线路"),
    ],
)
def test_safety_distance_place_by_device_type_and_path(device_type, path, expected):
    device = DeviceCandidate(device_id="d", name="n", device_type=device_type, voltage_level=None, path=path)

    assert _safety_distance_place(device) == expected


def test_query_safety_distance_selects_requested_place(client: FakeKgClient):
    """同一电压等级下，“线路”取 DL/T 409 的记录、“变电”取 DL/T 408 的记录。"""

    line = _query_safety_distance(client, voltage_level="10kV", place="线路")
    substation = _query_safety_distance(client, voltage_level="10kV", place="变电")

    assert line is not None and substation is not None
    assert line.voltage_level == substation.voltage_level == "10kV"
    assert (line.no_outage_m, line.work_range_m, line.live_work_m) == (0.7, 0.35, 0.4)
    assert line.clause is not None and line.clause.standard_no == "DL/T 409—2023"
    assert substation.clause is not None and substation.clause.standard_no == "DL/T 408—2023"


def test_query_safety_distance_preserves_applicability_remark(client: FakeKgClient):
    """图谱安全距离的条件不能在映射时丢弃。"""

    distance = _query_safety_distance(client, voltage_level="10kV", place="变电")
    assert distance.remark == SAFETY_DISTANCES[0]["备注"]


def test_query_safety_distance_falls_back_and_logs_when_place_missing(client: FakeKgClient):
    """夹具里只有 35kV 变电记录：请求 35kV 线路场所时退回同电压任一条并记一句说明。"""

    log: list[str] = []

    distance = _query_safety_distance(client, voltage_level="35kV", place="线路", log=log)

    assert distance is not None
    assert distance.voltage_level == "35kV"
    assert distance.no_outage_m == 1.0
    assert distance.clause is not None and distance.clause.standard_no == "DL/T 408—2023"
    assert len(log) == 1
    assert "没有“线路”场所的安全距离记录" in log[0]
    assert "退回同电压等级任一条" in log[0]


def test_query_safety_distance_returns_none_when_voltage_absent(client: FakeKgClient):
    assert _query_safety_distance(client, voltage_level="999kV", place="变电") is None


def test_query_safety_distance_does_not_log_when_both_places_absent(client: FakeKgClient):
    """连同电压等级都没有时不写“已退回”的说明（由调用处报“暂无安全距离数据”）。"""

    log: list[str] = []

    assert _query_safety_distance(client, voltage_level="999kV", place="线路", log=log) is None
    assert log == []


def test_collect_evidence_picks_line_place_for_line_device(client: FakeKgClient):
    """杆塔（线路类设备）取“线路”场所的安全距离（DL/T 409），检索记录写明场所。"""

    device = _device("BN18杆塔")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="检修BN18杆塔", task_type=TASK_REPAIR), device)

    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert evidence.safety_distance.clause is not None
    assert evidence.safety_distance.clause.standard_no == "DL/T 409—2023"
    assert any("适用场所“线路”" in record for record in evidence.retrieval_log)


def test_collect_evidence_picks_substation_place_for_switchgear(client: FakeKgClient):
    """开关柜（变电类设备）取“变电”场所的安全距离（DL/T 408）。"""

    device = _device("箱变13馈出柜")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="检修箱变13馈出柜", task_type=TASK_REPAIR), device)

    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert evidence.safety_distance.clause is not None
    assert evidence.safety_distance.clause.standard_no == "DL/T 408—2023"
    assert any("适用场所“变电”" in record for record in evidence.retrieval_log)


def test_collect_evidence_logs_place_fallback(client: FakeKgClient):
    """35kV 线路类设备：夹具无 35kV 线路记录 → 退回 35kV 变电记录并在 retrieval_log 说明。"""

    device = _device("T35杆塔")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="检修T35杆塔", task_type=TASK_REPAIR), device)

    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "35kV"
    assert evidence.safety_distance.clause is not None
    assert evidence.safety_distance.clause.standard_no == "DL/T 408—2023"
    assert any("没有“线路”场所的安全距离记录" in record for record in evidence.retrieval_log)
    assert any("退回同电压等级任一条" in record for record in evidence.retrieval_log)


# ---------------------------------------------------------------------------
# T12a 新增：IN 条件构造函数与全量 Cypher 约束
# ---------------------------------------------------------------------------
def test_in_condition_helper_builds_fragment():
    assert _in_condition("b", "名称", "area_names") == "coalesce(b.名称, '') IN $area_names"
    assert _in_condition("n", "standard_id", "standard_ids") == "coalesce(n.standard_id, '') IN $standard_ids"


def test_all_cypher_single_match_and_no_backticks(client: FakeKgClient):
    """走遍设备检索 + 证据收集的全部语句：每条只有一个 MATCH、无反引号、带 LIMIT。"""

    queries = (
        PlanQuery(
            raw_text="更换#3开闭所3k18断路器",
            task_type=TASK_REPLACE,
            device_name="3k18",
            location_hint="#3开闭所",
            device_type="开关柜",
            work_object="断路器",
        ),
        PlanQuery(raw_text="检修BN18杆塔", task_type=TASK_REPAIR, device_name="BN18杆塔", device_type="杆塔"),
        PlanQuery(raw_text="巡视线路", task_type=TASK_PATROL, device_type="架空线路"),
        PlanQuery(raw_text="更换开关柜", task_type=TASK_REPLACE, device_type="开关柜"),
    )
    for query in queries:
        for device in search_devices(client, query):
            collect_plan_evidence(client, query, device)

    assert client.queries
    for cypher, params in client.queries:
        assert cypher.count("MATCH") == 1
        assert "`" not in cypher
        assert "LIMIT $node_limit" in cypher
        assert params["node_limit"] <= 200
    # 安全距离按场所过滤的语句、以及 IN 条件构造函数产出的片段都在语句里
    assert any("$place" in cypher for cypher, _ in client.queries)
    assert any("IN $area_names" in cypher for cypher, _ in client.queries)


# ---------------------------------------------------------------------------
# T12a 二轮：父节点有电压属性但名称不带 kV 时解析电压（保留父节点图谱属性）
# ---------------------------------------------------------------------------
def test_query_parents_returns_full_node_properties(client: FakeKgClient):
    """位于 查询一次拿全父节点属性（名称 + 电压等级），不是只留名称。"""

    parents = _query_parents(client, device=_device("3k19"))

    assert parents == [
        {"id": "LGR#reg##3开闭所", "名称": "#3开闭所", "电压等级": "10kV"},
    ]
    assert [params["rel"] for _, params in client.queries] == [REL_LOCATED_IN]


def test_collect_evidence_inherits_voltage_from_parent_property(client: FakeKgClient):
    """验收 #1 反例：3k19 自身电压为空、名称也不带 kV，从父节点 #3开闭所 的 10kV 属性取。

    对应真实台账 3k18（id LGR#04438230471b45ea9e5ea0cc09cfb524）与其父节点 #3开闭所
    （id LGR#093f1ffe560b4ef382e701cd4911b8b3，电压等级=10kV）。
    """

    device = _device("3k19")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="更换3k19", task_type=TASK_REPLACE), device)

    assert device.voltage_level is None
    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert evidence.safety_distance.no_outage_m == 0.7
    assert evidence.safety_distance.work_range_m == 0.35
    assert evidence.safety_distance.live_work_m == 0.4
    assert evidence.safety_distance.clause is not None
    assert evidence.safety_distance.clause.standard_no == "DL/T 408—2023"  # 断路器 → 变电场所
    assert any("按上级节点解析出电压等级“10kV”" in record for record in evidence.retrieval_log)


def test_collect_evidence_nearer_ancestor_name_wins_over_farther_parent_property(client: FakeKgClient):
    """更正后的优先级：更近节点名称里的 kV 不被更远节点的真实属性覆盖。

    PX开关 的层级路径是“变电设施 > 35kV#3变电站 > 10kV电压等级区域 > PX开关”，位于 直接返回
    35kV#3变电站（属性 35kV），最近的祖先“10kV电压等级区域”名称带 10kV → 取 10kV。
    二轮曾在此断言 35kV（“父站属性优先于更近区域名称”），属**不正确期望**，已按三度指令更正。
    """

    device = _device("PX开关")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="检修PX开关", task_type=TASK_REPAIR), device)

    assert evidence.ancestors == ["10kV电压等级区域", "35kV#3变电站", "变电设施"]
    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert evidence.safety_distance.no_outage_m == 0.7
    assert any("按上级节点解析出电压等级“10kV”" in record for record in evidence.retrieval_log)


def test_collect_evidence_prefers_device_voltage_over_parent(client: FakeKgClient):
    """设备自身有电压时优先用设备电压：992SVG开关 10kV 不被父节点 35kV#1变电站 覆盖。"""

    device = _device("992SVG开关")

    evidence = collect_plan_evidence(client, PlanQuery(raw_text="检修992SVG开关", task_type=TASK_REPAIR), device)

    assert device.voltage_level == "10kV"
    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert evidence.safety_distance.no_outage_m == 0.7


def test_collect_evidence_queries_parents_once(client: FakeKgClient):
    """父节点属性与名称补全复用同一次 位于 查询，不为同一父节点重复查图。"""

    collect_plan_evidence(client, PlanQuery(raw_text="更换3k19", task_type=TASK_REPLACE), _device("3k19"))

    assert [params.get("rel") for _, params in client.queries].count(REL_LOCATED_IN) == 1


def test_resolve_voltage_uses_nearest_ancestor_property():
    parents = [
        {"名称": "远节点", "电压等级": "35kV"},
        {"名称": "近节点", "电压等级": "10kV"},
    ]

    assert _resolve_voltage_level(parents, ["近节点", "中间", "远节点"]) == "10kV"


def test_resolve_voltage_skips_ancestors_without_any_voltage():
    parents = [{"名称": "近节点"}, {"名称": "远节点", "电压等级": "35kV"}]

    assert _resolve_voltage_level(parents, ["近节点", "远节点"]) == "35kV"


def test_resolve_voltage_prefers_nearer_name_over_farther_property():
    """更远节点的真实属性不覆盖更近节点名称里的 kV。"""

    parents = [{"名称": "35kV#1变电站", "电压等级": "35kV"}]

    assert _resolve_voltage_level(parents, ["10kV电压等级区域", "35kV#1变电站"]) == "10kV"


def test_resolve_voltage_prefers_property_of_the_same_node_over_its_name():
    """同一个节点：真实属性优先于该节点名称里的 kV。"""

    parents = [{"名称": "10kV电压等级区域", "电压等级": "35kV"}]

    assert _resolve_voltage_level(parents, ["10kV电压等级区域"]) == "35kV"


def test_resolve_voltage_handles_parent_outside_ancestors():
    """父节点不在层级路径里（位于 与 层级路径 不一致）时仍能取到电压，但排在所有祖先之后。"""

    assert _resolve_voltage_level([{"名称": "孤立父节点", "电压等级": "10kV"}], ["别的节点"]) == "10kV"
    # 更近祖先名称里有 kV 时优先于“不在路径里”的父节点属性
    assert _resolve_voltage_level([{"名称": "孤立父节点", "电压等级": "35kV"}], ["10kV电压等级区域"]) == "10kV"


def test_resolve_voltage_returns_none_without_any_voltage():
    assert _resolve_voltage_level([{"名称": "x"}], ["x"]) is None
    assert _resolve_voltage_level([], []) is None


def test_collect_evidence_mirrors_independent_review_minimal_client():
    """镜像独立验收反例（work/test_parent_voltage_review.py）：除 位于/安全距离 外一律返回空。

    与验收反例使用同样的设备 id、父节点 id 与假数据，只在本目录内验证同一断言，
    不改动验收方文件。
    """

    class MinimalClient:
        """只认识 位于 与 安全距离 两类查询的假客户端。"""

        def __init__(self) -> None:
            self.queries: list[tuple[str, dict]] = []

        def run(self, cypher: str, **params) -> list[dict]:
            self.queries.append((cypher, params))
            if params.get("rel") == REL_LOCATED_IN:
                return [
                    {
                        "b": {
                            "id": "LGR#093f1ffe560b4ef382e701cd4911b8b3",
                            "名称": "#3开闭所",
                            "电压等级": "10kV",
                            "层级路径": "#3开闭所",
                        }
                    }
                ]
            if params.get("label") == LABEL_SAFETY_DISTANCE:
                return [
                    {
                        "n": {
                            "电压等级": "10kV",
                            "适用场所": "变电",
                            "设备不停电安全距离_m": 0.7,
                            "工作中正常活动范围安全距离_m": 0.35,
                            "带电作业安全距离_m": 0.4,
                        }
                    }
                ]
            return []

    client = MinimalClient()
    device = DeviceCandidate(
        device_id="LGR#04438230471b45ea9e5ea0cc09cfb524",
        name="3k18",
        device_type="开关柜",
        voltage_level=None,
        path="#3开闭所 > 3k18",
    )

    evidence = collect_plan_evidence(client, PlanQuery(task_type=TASK_REPLACE, device_name="3k18"), device)

    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert evidence.safety_distance.no_outage_m == 0.7
    assert evidence.safety_distance.work_range_m == 0.35
    assert evidence.safety_distance.live_work_m == 0.4
    # 父节点属性与名称补全复用同一次 位于 查询
    assert [params.get("rel") for _, params in client.queries].count(REL_LOCATED_IN) == 1


def test_collect_evidence_mirrors_nearer_region_review_minimal_client():
    """镜像独立验收反例 #2：位于 跳过中间电压区域，更近区域的名称必须胜过更远站的属性。

    路径“35kV#1变电站 > 10kV电压等级区域 > 馈出柜”，位于 直接返回 35kV#1变电站（属性 35kV）；
    更近的“10kV电压等级区域”名称带 10kV → 必须取 10kV（与验收反例同一设备名、路径与假数据）。
    """

    class MinimalClient:
        """只认识 位于 与 安全距离 两类查询的假客户端（安全距离回显被查询的电压等级）。"""

        def __init__(self) -> None:
            self.queries: list[tuple[str, dict]] = []

        def run(self, cypher: str, **params) -> list[dict]:
            self.queries.append((cypher, params))
            if params.get("rel") == REL_LOCATED_IN:
                return [{"b": {"id": "station", "名称": "35kV#1变电站", "电压等级": "35kV"}}]
            if params.get("label") == LABEL_SAFETY_DISTANCE:
                return [
                    {
                        "n": {
                            "电压等级": params["voltage_level"],
                            "适用场所": "变电",
                            "设备不停电安全距离_m": 0.7,
                            "工作中正常活动范围安全距离_m": 0.35,
                            "带电作业安全距离_m": 0.4,
                        }
                    }
                ]
            return []

    client = MinimalClient()
    device = DeviceCandidate(
        device_id="child",
        name="馈出柜",
        device_type="开关柜",
        voltage_level=None,
        path="35kV#1变电站 > 10kV电压等级区域 > 馈出柜",
    )

    evidence = collect_plan_evidence(client, PlanQuery(task_type=TASK_REPAIR, device_name="馈出柜"), device)

    assert evidence.ancestors == ["10kV电压等级区域", "35kV#1变电站"]
    assert evidence.safety_distance is not None
    assert evidence.safety_distance.voltage_level == "10kV"
    assert evidence.safety_distance.no_outage_m == 0.7
    # 只查一次 位于，且安全距离按解析出的 10kV 查询
    assert [params.get("rel") for _, params in client.queries].count(REL_LOCATED_IN) == 1
    assert any(params.get("voltage_level") == "10kV" for _, params in client.queries)



def test_selected_duplicate_device_keeps_its_own_parent_and_outage():
    """选定台账主键后不能再按同名设备扩散父级证据。"""

    class IdentityClient:
        def run(self, cypher, **params):
            if params.get('rel') != REL_LOCATED_IN:
                return []
            own = {'b': {'id': 'station-2', '名称': '35kV#2变电站', '电压等级': '35kV'}}
            other = {'b': {'id': 'station-1', '名称': '35kV#1变电站', '电压等级': '35kV'}}
            return [own, other] if '$device_name' in cypher else [own]

    device = DeviceCandidate('chosen-2', '992SVG开关', '开关柜', '10kV',
                             '35kV#2变电站 > 10kV电压等级区域 > 992SVG柜 > 992SVG开关')
    evidence = collect_plan_evidence(IdentityClient(), PlanQuery(task_type=TASK_REPAIR, device_name=device.name), device)
    assert '35kV#2变电站' in evidence.outage_scope
    assert '35kV#1变电站' not in evidence.ancestors
    assert '35kV#1变电站' not in evidence.outage_scope


def test_selected_device_electrical_query_uses_ids_without_name_expansion():
    """电连接查询也只允许选定 ID，避免同名设备挤占邻接查询上限。"""

    queries = []

    class IdentityClient:
        def run(self, cypher, **params):
            queries.append((cypher, params))
            return []

    device = DeviceCandidate('chosen-2', '992SVG开关', '开关柜', '10kV', '35kV#2变电站 > 992SVG开关')
    collect_plan_evidence(IdentityClient(), PlanQuery(task_type=TASK_REPAIR, device_name=device.name), device)
    cypher, params = next((c, p) for c, p in queries if p.get('rel') == REL_ELECTRIC)
    assert params['device_id'] == device.device_id
    assert '$device_name' not in cypher
