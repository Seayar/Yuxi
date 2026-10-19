"""T7 方案装配的单测。

全部使用本地构造的假 ``PlanQuery`` / ``PlanEvidence``，不连接数据库、图谱与大模型。
夹具覆盖任务卡点名的三套场景（开关柜更换、变压器检修、线路巡视），并补一套开关柜试验
用于覆盖“试验项目补充与去重”。
"""

from __future__ import annotations

import dataclasses
import logging
import re

import pytest

from yuxi.knowledge.maintenance_plan.answer import (
    DEMO_RESOURCE_NOTE,
    FALLBACK_LOCATION,
    FALLBACK_UPSTREAM_POWER,
    FALLBACK_VOLTAGE_LEVEL,
    NO_OUTAGE_SCOPE_TEXT,
    NO_REFERENCE_TEXT,
    NO_SAFETY_DISTANCE_NOTE,
    NO_TEAM_TEXT,
    OUTAGE_DERIVATION_NOTE,
    OUTAGE_NOT_REQUIRED_TEXT,
    PATROL_ITEM_MARK,
    PLACEHOLDER_DEVICE_NAME,
    PLACEHOLDER_LOCATION,
    PLACEHOLDER_UPSTREAM_POWER,
    PLACEHOLDER_VOLTAGE_LEVEL,
    TEST_ITEM_MARK,
    assemble_report,
    build_placeholder_context,
    build_plan_markdown,
    build_thinking,
    fill_placeholders,
    produce_maintenance_plan_answer,
    verify_report,
)
from yuxi.knowledge.maintenance_plan.contracts import (
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

pytestmark = pytest.mark.unit

HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
DEMO_NOTE = "演示数据（待接入生产系统）"

SECTION_HEADINGS = (
    "## 一、检修前准备",
    "## 二、检修步骤",
    "## 三、安全措施",
    "## 四、所需工器具及材料",
    "## 五、责任班组与资源安排",
    "## 六、验收标准",
)


# ---------------------------------------------------------------------------
# 夹具一：开关柜更换（#3开闭所 3k18），班组/备件/安全距离齐全，条款含重复项
# ---------------------------------------------------------------------------
def _switchgear_replace_case() -> tuple[PlanQuery, PlanEvidence]:
    query = PlanQuery(
        raw_text="更换#3开闭所3k18开关柜断路器",
        task_type=TASK_REPLACE,
        device_name="3k18",
        device_type="开关柜",
        voltage_level="10kV",
        work_object="断路器",
        location_hint="#3开闭所",
    )
    template = WorkTemplate(
        template_id="TPL#开关柜#更换#断路器",
        name="开关柜断路器更换",
        task_type=TASK_REPLACE,
        device_type="开关柜",
        work_object="断路器",
        outage_required="需停电",
        isolation_measures=(
            "断开{上级电源}对应出线开关并上锁，在{设备名称}电缆侧及断路器两侧分别验电、挂接地线，"
            "悬挂“禁止合闸，有人工作”标示牌，设专人监护。"
        ),
        step_sequence=[
            "断开{上级电源}侧电源并验电",
            "拆除二次线并逐芯编号",
            "拆下旧断路器手车",
            "安装同型号新断路器",
            "恢复二次接线并核对相序",
            "手动分合闸3次、电动分合闸3次",
            "测量回路电阻与主回路绝缘电阻",
            "拆除接地线、恢复送电",
        ],
        key_points=[
            "拆二次线前逐芯编号并拍照，接线端子扭矩1.2N·m",
            "新断路器手车摇入试验位置与工作位置需到位，底盘车闭锁可靠",
            "触头涂抹适量导电膏，触头弹簧压力均匀",
        ],
        tech_params=[
            "额定电压12kV，额定电流1250A，额定短路开断电流25kA",
            "回路电阻不大于60μΩ，主回路绝缘电阻不低于1000MΩ",
        ],
        personal_protection="穿戴{电压等级}绝缘手套、绝缘鞋、安全帽",
        grounding_requirements="在{设备名称}电缆侧及断路器两侧分别验电、挂接地线，接地线装设可靠",
        work_safety=["手车摇进摇出前必须确认断路器处于分闸位置", "作业期间设专人监护"],
        tools=[
            "力矩扳手（0–25N·m）",
            "万用表",
            "回路电阻测试仪（100A）",
            "2500V绝缘电阻表",
            "吊带",
        ],
        materials=["导电膏", "同型号备用断路器VS1-12/1250-25（1台）"],
        acceptance=[
            "手动分合闸3次、电动分合闸3次均可靠动作，合闸弹跳时间不大于2ms",
            "主回路回路电阻不大于60μΩ",
        ],
        notes=["作业前核对一次系统图与二次接线图"],
        staffing="4人：工作负责人1人、作业人员2人、专责监护人1人",
        clauses=[
            ClauseRef("DL/T 408—2023", "7.3.1", "停电、验电、接地", "作业时须停电、验电并装设接地线"),
            ClauseRef("DL/T 408—2023", "7.3.1", "停电、验电、接地", "作业时须停电、验电并装设接地线"),
            ClauseRef("DL/T 596—2021", "8.4", "断路器试验", "断路器回路电阻与绝缘电阻测量要求"),
        ],
        source_note="工序与操作要点依据 DL/T 408—2023 第7章及通用检修工艺",
    )
    evidence = PlanEvidence(
        device=DeviceCandidate(
            device_id="LGR#3k18",
            name="3k18",
            device_type="开关柜",
            voltage_level="10kV",
            path="#3开闭所 > 3k18",
            ledger_class="中压断路器柜",
            score=0.95,
        ),
        ancestors=["#3开闭所"],
        outage_scope=["3k18", "#3开闭所"],
        template=template,
        safety_distance=SafetyDistance(
            voltage_level="10kV",
            no_outage_m=0.70,
            work_range_m=0.35,
            live_work_m=0.40,
            clause=ClauseRef("DL/T 408—2023", "6.2.1", "安全距离", "10kV 设备不停电时的安全距离为0.70m"),
        ),
        team=TeamInfo(
            name="开闭所运维班",
            member_count=4,
            leader="李工",
            managed_area="#3开闭所",
            source_note=DEMO_NOTE,
        ),
        spares=[
            SpareInfo(
                name="同型号备用断路器",
                model="VS1-12/1250-25",
                quantity=1,
                location="备品库A-03",
                source_note=DEMO_NOTE,
            )
        ],
        retrieval_log=["设备定位：名称+层级路径打分命中唯一候选 3k18"],
    )
    return query, evidence


# ---------------------------------------------------------------------------
# 夹具二：变压器检修（#2站用变压器），无班组、无安全距离、无备件，电压等级缺失
# ---------------------------------------------------------------------------
def _transformer_repair_case() -> tuple[PlanQuery, PlanEvidence]:
    query = PlanQuery(
        raw_text="检修#2站用变压器冷却系统",
        task_type=TASK_REPAIR,
        device_name="#2站用变压器",
        device_type="变压器",
        work_object="冷却系统",
        location_hint="35kV#1变电站",
    )
    template = WorkTemplate(
        template_id="TPL#变压器#检修",
        name="变压器冷却系统检修",
        task_type=TASK_REPAIR,
        device_type="变压器",
        work_object="",
        outage_required="需停电",
        isolation_measures="断开{上级电源}各侧断路器并上锁，在{设备名称}各侧及中性点分别验电、挂接地线。",
        step_sequence=[
            "停电隔离",
            "检查油泵与风扇电机及控制回路",
            "清洗冷却器散热片",
            "更换损坏密封件",
            "复测绝缘与启动电流",
        ],
        key_points=["拆除油泵电机接线前做好相序标记", "清洗散热片时控制水压不大于0.3MPa"],
        tech_params=["油泵电机绝缘电阻不低于10MΩ", "顶层油温不高于85℃"],
        personal_protection="穿戴{电压等级}防护手套、绝缘鞋、安全帽",
        grounding_requirements="在{设备名称}各侧及中性点分别验电、挂接地线",
        work_safety=["主变本体作业时禁止明火"],
        tools=["万用表", "500V绝缘电阻表", "扳手", "接油容器"],
        materials=["清洗剂（2瓶）", "密封件备件套装（含O型圈4套）"],
        acceptance=["冷却器试运行无异响、无渗漏"],
        notes=["作业前核对历史缺陷记录"],
        staffing="4人：工作负责人1人、作业人员3人",
        clauses=[ClauseRef("DL/T 596—2021", "附录E/F", "变压器检修", "变压器本体与冷却系统检修要求")],
        source_note="通用检修工艺",
    )
    evidence = PlanEvidence(
        device=DeviceCandidate(
            device_id="LGR#2站用变",
            name="#2站用变压器",
            device_type="变压器",
            voltage_level=None,
            path="变电设施 > 35kV#1变电站 > #2站用变压器",
            ledger_class="站用变压器",
            score=0.90,
        ),
        ancestors=["35kV#1变电站", "变电设施"],
        outage_scope=["#2站用变压器", "35kV#1变电站"],
        template=template,
        retrieval_log=["安全距离：未检索到该电压等级的安全距离数据"],
    )
    return query, evidence


# ---------------------------------------------------------------------------
# 夹具三：线路巡视（BN18杆塔），模板不停电、设备电压等级缺失（取安全距离电压等级）
# ---------------------------------------------------------------------------
def _line_patrol_case() -> tuple[PlanQuery, PlanEvidence]:
    query = PlanQuery(
        raw_text="巡视敖一线BN18杆塔",
        task_type=TASK_PATROL,
        device_name="BN18杆塔",
        device_type="杆塔",
        location_hint="敖一线",
    )
    template = WorkTemplate(
        template_id="TPL#杆塔#巡视",
        name="架空线路杆塔巡视",
        task_type=TASK_PATROL,
        device_type="杆塔",
        work_object="",
        outage_required="不停电",
        isolation_measures="巡视作业不接触带电设备，沿巡视路线行进，与带电体保持{电压等级}安全距离。",
        step_sequence=[
            "核对线路运行方式",
            "杆塔外观检查",
            "导线与金具红外测温",
            "通道环境检查",
            "记录并上报缺陷",
        ],
        key_points=["红外测温重点检查导线接头与金具", "发现异常立即拍照并登记缺陷"],
        tech_params=["连接部位温升不大于40K"],
        personal_protection="穿戴安全帽、绝缘鞋、反光背心",
        grounding_requirements="巡视过程中不操作设备；若需接触设备，必须执行停电、验电、挂接地线后方可作业",
        work_safety=["大风、雷雨天气不进行登杆作业"],
        tools=["红外热像仪", "巡视终端", "望远镜", "温湿度计"],
        materials=["缺陷登记单", "巡检标签"],
        acceptance=["巡视记录完整，无遗漏", "杆塔倾斜检查"],
        notes=["巡视前查看前一天运行告警与负荷曲线"],
        staffing="2人：工作负责人1人、巡视人员1人",
        clauses=[ClauseRef("DL/T 409—2023", "6.2", "线路巡视安全要求", "线路巡视的安全距离与安全要求")],
        source_note="依据 DL/T 409—2023 架空线路部分及通用巡视要求",
    )
    evidence = PlanEvidence(
        device=DeviceCandidate(
            device_id="LGR#BN18杆塔",
            name="BN18杆塔",
            device_type="杆塔",
            voltage_level=None,
            path="输电设施 > 敖一线 > BN18杆塔",
            ledger_class="杆塔单元",
            score=0.88,
        ),
        ancestors=["敖一线", "输电设施"],
        outage_scope=["BN18杆塔"],
        template=template,
        patrol_items=["杆塔倾斜检查", "导线及金具红外测温", "通道树木距离检查"],
        safety_distance=SafetyDistance(
            voltage_level="10kV",
            no_outage_m=0.70,
            work_range_m=0.35,
            live_work_m=0.40,
            clause=ClauseRef("DL/T 409—2023", "6.2.1", "安全距离", "10kV 线路安全距离要求"),
        ),
        team=TeamInfo(
            name="集电线路运维班",
            member_count=2,
            leader="张工",
            managed_area="敖一线",
            source_note=DEMO_NOTE,
        ),
        retrieval_log=["设备定位：命中 1 个候选 BN18杆塔"],
    )
    return query, evidence


# ---------------------------------------------------------------------------
# 夹具四：开关柜试验（#62箱变13馈出柜），多级层级路径 + 试验项目补充去重
# ---------------------------------------------------------------------------
def _switchgear_test_case() -> tuple[PlanQuery, PlanEvidence]:
    query = PlanQuery(
        raw_text="对#62箱变13馈出柜做预防性试验",
        task_type=TASK_TEST,
        device_name="#62箱变13馈出柜",
        device_type="开关柜",
        voltage_level="10kV",
        location_hint="#62箱变",
    )
    template = WorkTemplate(
        template_id="TPL#开关柜#试验",
        name="开关柜例行试验",
        task_type=TASK_TEST,
        device_type="开关柜",
        work_object="",
        outage_required="需停电",
        isolation_measures="断开{上级电源}侧电源并上锁，在{设备名称}两侧及相邻柜间隔验电、挂接地线。",
        step_sequence=[
            "在{所在位置}布置试验区域并设围栏",
            "测量主回路绝缘电阻",
            "进行主回路交流耐压试验",
            "测量断路器回路电阻",
            "拆除试验接线并恢复送电",
        ],
        key_points=["耐压前确认所有人员撤离", "耐压后充分放电，放电时间不少于5min"],
        tech_params=["交流耐压试验电压42kV（1min）", "主回路绝缘电阻不低于1000MΩ"],
        personal_protection="穿戴{电压等级}绝缘手套、绝缘鞋、安全帽",
        grounding_requirements="试验前对{设备名称}上下级及被试相验电、挂接地线",
        work_safety=["相邻带电柜间隔必须验电并保持安全距离"],
        tools=["2500V绝缘电阻表", "交流耐压试验装置（0–50kV）", "100A回路电阻测试仪", "放电棒"],
        materials=["试验接线", "接地线", "绝缘胶带"],
        acceptance=["主回路绝缘电阻不低于1000MΩ", "交流耐压42kV/1min无闪络、无击穿", "回路电阻测量"],
        notes=["试验仪器须在检定有效期内"],
        staffing="3人：试验负责人1人、试验人员2人",
        clauses=[ClauseRef("DL/T 596—2021", "8.4", "开关柜试验", "开关柜预防性试验项目与判据")],
        source_note="依据 DL/T 596—2021 第8章",
    )
    evidence = PlanEvidence(
        device=DeviceCandidate(
            device_id="LGR#000663b342f44515a2ad7a8bfa5edfca",
            name="#62箱变13馈出柜",
            device_type="开关柜",
            voltage_level="10kV",
            path="箱变群3-6 > #62箱变 > 10kV电压等级区域 > 箱变13馈出柜 > #62箱变13馈出柜",
            ledger_class="中压断路器柜",
            score=0.92,
        ),
        ancestors=["箱变13馈出柜", "10kV电压等级区域", "#62箱变", "箱变群3-6"],
        outage_scope=["#62箱变13馈出柜", "#62箱变"],
        template=template,
        test_items=["测量铁心对地的绝缘电阻", "测量绕组的直流电阻", "回路电阻测量"],
        safety_distance=SafetyDistance(
            voltage_level="10kV",
            no_outage_m=0.70,
            work_range_m=0.35,
            live_work_m=0.40,
            clause=ClauseRef("DL/T 408—2023", "6.2.1", "安全距离", "10kV 设备不停电时的安全距离为0.70m"),
        ),
        team=TeamInfo(name="箱变运维班", member_count=3, leader="王工", managed_area="#62箱变"),
        spares=[],
        retrieval_log=["模板检索：设备类型=开关柜，作业类型=试验，命中 1 个模板"],
    )
    return query, evidence


CASE_BUILDERS = {
    "开关柜更换": _switchgear_replace_case,
    "变压器检修": _transformer_repair_case,
    "线路巡视": _line_patrol_case,
    "开关柜试验": _switchgear_test_case,
}


@pytest.fixture
def switchgear_replace():
    return _switchgear_replace_case()


@pytest.fixture
def transformer_repair():
    return _transformer_repair_case()


@pytest.fixture
def line_patrol():
    return _line_patrol_case()


@pytest.fixture
def switchgear_test():
    return _switchgear_test_case()


@pytest.fixture(params=sorted(CASE_BUILDERS))
def any_case(request):
    """四套完整证据夹具中的任意一套。"""

    return CASE_BUILDERS[request.param]()


# ---------------------------------------------------------------------------
# 占位符
# ---------------------------------------------------------------------------
def test_placeholder_context_takes_device_fields(switchgear_replace):
    _, evidence = switchgear_replace

    ctx = build_placeholder_context(evidence)

    assert ctx[PLACEHOLDER_DEVICE_NAME] == "3k18"
    assert ctx[PLACEHOLDER_VOLTAGE_LEVEL] == "10kV"
    assert ctx[PLACEHOLDER_LOCATION] == "#3开闭所"
    assert ctx[PLACEHOLDER_UPSTREAM_POWER] == "#3开闭所"


def test_placeholder_location_joins_ancestors_from_far_to_near(switchgear_test):
    _, evidence = switchgear_test

    ctx = build_placeholder_context(evidence)

    assert ctx[PLACEHOLDER_LOCATION] == "箱变群3-6 > #62箱变 > 10kV电压等级区域 > 箱变13馈出柜"


def test_placeholder_voltage_falls_back_to_safety_distance(line_patrol):
    _, evidence = line_patrol

    ctx = build_placeholder_context(evidence)

    assert evidence.device.voltage_level is None
    assert ctx[PLACEHOLDER_VOLTAGE_LEVEL] == "10kV"


def test_placeholder_voltage_falls_back_to_text(transformer_repair):
    _, evidence = transformer_repair

    ctx = build_placeholder_context(evidence)

    assert ctx[PLACEHOLDER_VOLTAGE_LEVEL] == FALLBACK_VOLTAGE_LEVEL


def test_placeholder_upstream_power_falls_back_when_no_upstream_node(line_patrol):
    _, evidence = line_patrol

    assert evidence.outage_scope == ["BN18杆塔"]

    ctx = build_placeholder_context(evidence)

    assert ctx[PLACEHOLDER_UPSTREAM_POWER] == FALLBACK_UPSTREAM_POWER


def test_placeholder_upstream_power_uses_last_outage_node(switchgear_replace):
    _, evidence = switchgear_replace

    ctx = build_placeholder_context(evidence)

    assert ctx[PLACEHOLDER_UPSTREAM_POWER] == evidence.outage_scope[-1] == "#3开闭所"


def test_placeholder_location_falls_back_when_no_ancestor(switchgear_replace):
    _, evidence = switchgear_replace

    ctx = build_placeholder_context(dataclasses.replace(evidence, ancestors=[]))

    assert ctx[PLACEHOLDER_LOCATION] == FALLBACK_LOCATION


def test_fill_placeholders_replaces_all_legal_placeholders():
    ctx = {
        PLACEHOLDER_DEVICE_NAME: "3k18",
        PLACEHOLDER_VOLTAGE_LEVEL: "10kV",
        PLACEHOLDER_LOCATION: "#3开闭所",
        PLACEHOLDER_UPSTREAM_POWER: "#3开闭所",
    }

    assert fill_placeholders("{设备名称}|{电压等级}|{所在位置}|{上级电源}", ctx) == "3k18|10kV|#3开闭所|#3开闭所"


def test_fill_placeholders_keeps_unknown_placeholder_and_logs(caplog):
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        result = fill_placeholders("在{设备名称}的{某某字段}处作业", {PLACEHOLDER_DEVICE_NAME: "3k18"})

    assert result == "在3k18的{某某字段}处作业"
    assert "{某某字段}" in result
    assert "未知占位符" in caplog.text


def test_fill_placeholders_keeps_legal_placeholder_missing_from_context():
    result = fill_placeholders("{电压等级}作业", {})

    assert result == "{电压等级}作业"


def test_fill_placeholders_handles_empty_text():
    assert fill_placeholders("", {PLACEHOLDER_DEVICE_NAME: "3k18"}) == ""


# ---------------------------------------------------------------------------
# 装配：标题、停电范围、安全距离、班组备件、依据条款
# ---------------------------------------------------------------------------
def test_title_matches_task_card_examples(switchgear_replace, transformer_repair):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    assert report.title == "3k18 断路器更换方案"

    query, evidence = transformer_repair
    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    # T12a：作业对象优先用 query.work_object（"冷却系统"），不再被模板的空作业对象顶掉
    assert report.title == "#2站用变压器 冷却系统检修方案"

    # 用户没说作业对象、模板作业对象也为空时退回模板设备类型；设备名已含"变压器"故不重复
    no_object = assemble_report(
        dataclasses.replace(query, work_object=None), evidence, elapsed_seconds=1.0
    )
    assert no_object.title == "#2站用变压器 检修方案"


def test_title_contains_plan_suffix_once(any_case):
    query, evidence = any_case

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.title.count("方案") == 1


# ---------------------------------------------------------------------------
# T12a 标题缩短：优先 query.work_object、模板对象去括号、仍超 12 字则省略
# ---------------------------------------------------------------------------
def _title_of(
    device_name: str,
    *,
    work_object: str | None,
    template_work_object: str,
    task_type: str = TASK_REPAIR,
    device_type: str = "架空线路",
) -> str:
    """构造最小证据，只关心标题规则（其余字段留空）。"""

    query = PlanQuery(raw_text="x", task_type=task_type, device_name=device_name, work_object=work_object)
    template = WorkTemplate(
        template_id="TPL#t",
        name="t",
        task_type=task_type,
        device_type=device_type,
        work_object=template_work_object,
    )
    evidence = PlanEvidence(
        device=DeviceCandidate(
            device_id="d", name=device_name, device_type=device_type, voltage_level="35kV", path=device_name
        ),
        template=template,
    )
    return assemble_report(query, evidence, elapsed_seconds=1.0).title


def test_title_prefers_query_work_object_over_template():
    title = _title_of(
        "1B-1K-1", work_object="绝缘子", template_work_object="导线、地线及金具（断股修补、防振锤复位）"
    )

    assert title == "1B-1K-1 绝缘子检修方案"


def test_title_uses_template_work_object_without_brackets():
    """T5 真实长作业对象：去掉全角括号及内容后只剩部件名，仍可进标题。"""

    title = _title_of(
        "1B-1K-1",
        work_object=None,
        template_work_object="导线、地线及金具（断股修补、防振锤复位、间隔棒调整）",
    )

    assert title == "1B-1K-1 导线、地线及金具检修方案"


def test_title_strips_half_width_brackets_too():
    title = _title_of("BN18杆塔", work_object=None, template_work_object="杆塔本体(塔材、螺栓、拉线、基础)")

    assert title == "BN18杆塔 杆塔本体检修方案"


def test_title_strips_brackets_from_query_work_object():
    """query.work_object 同样去括号：T5 最长的“试验”作业对象去括号后仅 8 字。"""

    title = _title_of(
        "1B-1K-1",
        work_object=(
            "导线、地线及金具（红外测温、微风振动测量、绝缘子瓷击穿带电检测、"
            "线路绝缘电阻测量、杆塔接地电阻测量）"
        ),
        template_work_object="",
        task_type=TASK_TEST,
    )

    assert title == "1B-1K-1 导线、地线及金具试验方案"


def test_title_omits_object_when_still_too_long():
    """T5 无括号且 31 字的作业对象：省略对象部分，只留 设备名 + 作业类型 + “方案”。"""

    title = _title_of(
        "1B-1K-1",
        work_object=None,
        template_work_object="电缆本体、终端及中间接头、接地装置、支架、路径及通道、电子设备",
        task_type=TASK_PATROL,
    )

    assert title == "1B-1K-1 巡视方案"


def test_title_omits_over_long_query_work_object():
    title = _title_of("1B-1K-1", work_object="电缆本体、终端及中间接头、接地装置", template_work_object="")

    assert title == "1B-1K-1 检修方案"


def test_title_object_length_boundary():
    """恰好 12 字保留，13 字省略。"""

    twelve = "甲" * 12
    thirteen = "甲" * 13

    assert _title_of("D", work_object=twelve, template_work_object="") == f"D {twelve}检修方案"
    assert _title_of("D", work_object=thirteen, template_work_object="") == "D 检修方案"


def test_title_omits_object_already_in_device_name():
    title = _title_of("1B-1K-1杆塔", work_object=None, template_work_object="杆塔", device_type="杆塔")

    assert title == "1B-1K-1杆塔 检修方案"


def test_title_strips_duplicate_plan_suffix():
    title = _title_of("3k18更换方案", work_object="断路器", template_work_object="")

    assert title == "3k18更换 断路器检修方案"
    assert title.count("方案") == 1


def test_outage_scope_joins_nodes_and_appends_derivation_note(switchgear_replace):
    query, evidence = switchgear_replace

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.outage_scope == f"3k18、#3开闭所{OUTAGE_DERIVATION_NOTE}"


def test_outage_scope_is_not_required_for_patrol_without_outage(line_patrol):
    query, evidence = line_patrol

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.outage_scope == OUTAGE_NOT_REQUIRED_TEXT


def test_outage_scope_keeps_derivation_for_patrol_with_outage(line_patrol):
    query, evidence = line_patrol
    evidence = dataclasses.replace(
        evidence, template=dataclasses.replace(evidence.template, outage_required="需停电")
    )

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.outage_scope == f"BN18杆塔{OUTAGE_DERIVATION_NOTE}"


def test_outage_scope_falls_back_when_empty(switchgear_replace):
    query, evidence = switchgear_replace

    report = assemble_report(query, dataclasses.replace(evidence, outage_scope=[]), elapsed_seconds=1.0)

    assert report.outage_scope == NO_OUTAGE_SCOPE_TEXT


def test_safety_distance_is_converted_to_dict(switchgear_replace):
    query, evidence = switchgear_replace

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.safety_distance == {
        "voltage_level": "10kV",
        "no_outage_m": 0.70,
        "work_range_m": 0.35,
        "live_work_m": 0.40,
    }
    assert NO_SAFETY_DISTANCE_NOTE not in report.work_safety


def test_missing_safety_distance_appends_note_to_work_safety(transformer_repair):
    query, evidence = transformer_repair

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.safety_distance is None
    assert report.work_safety[-1] == NO_SAFETY_DISTANCE_NOTE


def test_team_and_spares_text(switchgear_replace):
    query, evidence = switchgear_replace

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.team == "开闭所运维班（4人），负责人：李工"
    assert report.spares == ["同型号备用断路器 VS1-12/1250-25（库存 1，备品库A-03）"]
    assert report.resource_note == DEMO_RESOURCE_NOTE


def test_team_and_spares_text_when_absent(transformer_repair):
    query, evidence = transformer_repair

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.team == NO_TEAM_TEXT
    assert report.spares == []
    assert report.resource_note == ""


def test_team_text_without_member_count(switchgear_replace):
    query, evidence = switchgear_replace
    evidence = dataclasses.replace(
        evidence, team=dataclasses.replace(evidence.team, member_count=None)
    )

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.team == "开闭所运维班，负责人：李工"


def test_resource_note_marks_demo_data_from_team_only(line_patrol):
    query, evidence = line_patrol

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert evidence.spares == []
    assert report.resource_note == DEMO_RESOURCE_NOTE


def test_references_deduplicate_template_clauses(switchgear_replace):
    query, evidence = switchgear_replace

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.references == [
        {"standard_no": "DL/T 408—2023", "clause_no": "7.3.1", "title": "停电、验电、接地"},
        {"standard_no": "DL/T 596—2021", "clause_no": "8.4", "title": "断路器试验"},
    ]


def test_references_fall_back_text_is_used_in_markdown(switchgear_replace):
    query, evidence = switchgear_replace
    evidence = dataclasses.replace(
        evidence, template=dataclasses.replace(evidence.template, clauses=[])
    )

    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    markdown = build_plan_markdown(report)

    assert report.references == []
    assert NO_REFERENCE_TEXT in markdown


def test_assemble_uses_query_task_type_and_falls_back_to_template(switchgear_replace):
    query, evidence = switchgear_replace

    without_task_type = assemble_report(
        dataclasses.replace(query, task_type=None), evidence, elapsed_seconds=1.0
    )
    with_query_task_type = assemble_report(
        dataclasses.replace(query, task_type=TASK_REPAIR), evidence, elapsed_seconds=1.0
    )

    assert without_task_type.task_type == TASK_REPLACE  # 回落模板的作业类型
    assert with_query_task_type.task_type == TASK_REPAIR  # 以用户意图为准


def test_assemble_without_template_yields_empty_sections(switchgear_replace):
    query, evidence = switchgear_replace

    report = assemble_report(query, dataclasses.replace(evidence, template=None), elapsed_seconds=1.0)

    assert report.step_sequence == []
    assert report.tools == []
    assert report.references == []
    # T12a：标题作业对象优先取 query.work_object（"断路器"），不再退回模板设备类型
    assert report.title == "3k18 断路器更换方案"


def test_assemble_fills_placeholders_in_every_section(switchgear_replace):
    query, evidence = switchgear_replace

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.isolation_measures.startswith("断开#3开闭所对应出线开关并上锁")
    assert report.step_sequence[0] == "断开#3开闭所侧电源并验电"
    assert report.personal_protection == "穿戴10kV绝缘手套、绝缘鞋、安全帽"
    assert report.grounding_requirements.startswith("在3k18电缆侧及断路器两侧")
    assert "{" not in " ".join([report.isolation_measures, *report.step_sequence, report.personal_protection])


def test_acceptance_unchanged_for_replace_task(switchgear_replace):
    query, evidence = switchgear_replace

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.acceptance == list(evidence.template.acceptance)


# ---------------------------------------------------------------------------
# 装配：试验 / 巡视项目补充与去重
# ---------------------------------------------------------------------------
def test_test_items_appended_to_acceptance_without_duplicates(switchgear_test):
    query, evidence = switchgear_test

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.acceptance == [
        "主回路绝缘电阻不低于1000MΩ",
        "交流耐压42kV/1min无闪络、无击穿",
        "回路电阻测量",
        f"测量铁心对地的绝缘电阻{TEST_ITEM_MARK}",
        f"测量绕组的直流电阻{TEST_ITEM_MARK}",
    ]


def test_patrol_items_appended_to_acceptance_without_duplicates(line_patrol):
    query, evidence = line_patrol

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.acceptance == [
        "巡视记录完整，无遗漏",
        "杆塔倾斜检查",
        f"导线及金具红外测温{PATROL_ITEM_MARK}",
        f"通道树木距离检查{PATROL_ITEM_MARK}",
    ]


def test_knowledge_items_not_appended_for_other_task_types(switchgear_replace):
    query, evidence = switchgear_replace
    evidence = dataclasses.replace(
        evidence, test_items=["测量回路电阻"], patrol_items=["外观检查"]
    )

    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert report.acceptance == list(evidence.template.acceptance)


# ---------------------------------------------------------------------------
# 思考过程
# ---------------------------------------------------------------------------
def test_thinking_counts_match_template(any_case):
    query, evidence = any_case

    thinking = build_thinking(query, evidence, elapsed_seconds=2.0)

    assert f"共{len(evidence.template.step_sequence)}道工序" in thinking
    assert f"共{len(evidence.template.tools) + len(evidence.template.materials)}项" in thinking


def test_thinking_follows_required_sentence_pattern(switchgear_replace):
    query, evidence = switchgear_replace

    thinking = build_thinking(query, evidence, elapsed_seconds=3.0)

    assert thinking.startswith("已思考（用时 3 秒）")
    assert "正在解析您提交的运维任务……识别任务类型为“更换”" in thinking
    assert "设备名称为“3k18”" in thinking
    assert "设备类型为“开关柜”" in thinking
    assert "电压等级为“10kV”" in thinking
    assert "正在按作业类型“更换断路器”和设备类型“开关柜”组合检索规程知识节点……" in thinking
    assert "检索完成，获取以下知识：" in thinking
    assert "• 检修前准备：" in thinking
    assert "• 安全措施：含个人防护要求、安全距离要求、验电接地要求" in thinking
    assert "同步通过知识图谱检索业务关系……" in thinking
    assert "验收标准已从规程知识节点的“验收标准”字段中提取。" in thinking
    assert "方案生成完成。" in thinking


def test_thinking_reports_team_and_spares_from_evidence(switchgear_replace):
    query, evidence = switchgear_replace

    thinking = build_thinking(query, evidence, elapsed_seconds=1.0)

    assert "运维班组“开闭所运维班（4人）”" in thinking
    assert "责任人员“李工”" in thinking
    assert "同型号备用断路器 VS1-12/1250-25（库存 1，备品库A-03）" in thinking


def test_thinking_reports_missing_team_and_spares(transformer_repair):
    query, evidence = transformer_repair

    thinking = build_thinking(query, evidence, elapsed_seconds=1.0)

    assert "未在知识图谱中检索到该设备关联的管辖班组与责任人员" in thinking
    assert "未在知识图谱中检索到该设备类型的备品备件" in thinking


def test_thinking_formats_elapsed_seconds(switchgear_replace):
    query, evidence = switchgear_replace

    assert build_thinking(query, evidence, elapsed_seconds=0.0).startswith("已思考（用时 0 秒）")
    assert build_thinking(query, evidence, elapsed_seconds=12.0).startswith("已思考（用时 12 秒）")
    assert build_thinking(query, evidence, elapsed_seconds=1.5).startswith("已思考（用时 1.5 秒）")


def test_thinking_appends_retrieval_log(switchgear_replace):
    query, evidence = switchgear_replace

    thinking = build_thinking(query, evidence, elapsed_seconds=1.0)

    assert "检索记录：" in thinking
    assert "• 设备定位：名称+层级路径打分命中唯一候选 3k18" in thinking


def test_thinking_omits_retrieval_log_section_when_empty(switchgear_replace):
    query, evidence = switchgear_replace

    thinking = build_thinking(query, dataclasses.replace(evidence, retrieval_log=[]), elapsed_seconds=1.0)

    assert "检索记录：" not in thinking


def test_thinking_omits_acceptance_line_without_template_acceptance(switchgear_replace):
    query, evidence = switchgear_replace
    evidence = dataclasses.replace(
        evidence, template=dataclasses.replace(evidence.template, acceptance=[])
    )

    thinking = build_thinking(query, evidence, elapsed_seconds=1.0)

    assert "验收标准已从规程知识节点的“验收标准”字段中提取。" not in thinking


# ---------------------------------------------------------------------------
# Markdown 兜底文本
# ---------------------------------------------------------------------------
def test_markdown_has_six_sections_in_order(any_case):
    query, evidence = any_case
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    markdown = build_plan_markdown(report)

    positions = [markdown.index(heading) for heading in SECTION_HEADINGS]
    assert positions == sorted(positions)
    assert markdown.startswith(f"# {report.title}")
    assert "**验收标准**" not in markdown  # 段标题已经写明，不再重复小标题


def test_markdown_contains_no_html_tags(any_case):
    query, evidence = any_case
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    markdown = build_plan_markdown(report)

    assert HTML_TAG.search(markdown) is None
    assert "<table" not in markdown
    assert "<br" not in markdown


def test_markdown_has_no_leftover_placeholder(any_case):
    query, evidence = any_case
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert "{" not in build_plan_markdown(report)


def test_markdown_renders_safety_distance_as_markdown_table(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    markdown = build_plan_markdown(report)

    assert "| 电压等级 | 带电作业最小安全距离（m） | 设备不停电安全距离（m） | 正常活动范围安全距离（m） |" in markdown
    assert "| --- | --- | --- | --- |" in markdown
    assert "| 10kV | 0.4 | 0.7 | 0.35 |" in markdown


def test_safety_distance_conditions_reach_report_and_markdown(switchgear_replace):
    """原始图谱备注中的遮栏与海拔条件必须随距离一同发布。"""

    query, evidence = switchgear_replace
    remark = "0.35 m 仅用于已采取绝缘遮蔽或安全遮栏措施的情况；海拔3000m以下为0.4m。"
    evidence.safety_distance = dataclasses.replace(evidence.safety_distance, remark=remark)
    payload = produce_maintenance_plan_answer(query, evidence, elapsed_seconds=0.1)
    assert payload["report"]["safety_distance"]["remark"] == remark
    assert payload["answer_markdown"].count(remark) == 1
    assert payload["answer_verified"] is True


def test_markdown_shows_safety_distance_note_exactly_once_when_missing(transformer_repair):
    query, evidence = transformer_repair
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    markdown = build_plan_markdown(report)

    assert markdown.count(NO_SAFETY_DISTANCE_NOTE) == 1


def test_markdown_ends_with_references_and_omits_thinking(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    markdown = build_plan_markdown(report)

    assert "## 依据规程" in markdown
    assert "- DL/T 408—2023 7.3.1 停电、验电、接地" in markdown
    assert "- DL/T 596—2021 8.4 断路器试验" in markdown
    assert "已思考（用时" not in markdown
    assert report.thinking not in markdown


def test_markdown_lists_team_spares_and_demo_note(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    markdown = build_plan_markdown(report)

    assert "**责任班组：**开闭所运维班（4人），负责人：李工" in markdown
    assert "- 同型号备用断路器 VS1-12/1250-25（库存 1，备品库A-03）" in markdown
    assert f"**说明：**{DEMO_RESOURCE_NOTE}" in markdown


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def test_verify_passes_for_all_fixtures(any_case):
    query, evidence = any_case
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert verify_report(report, evidence) is True


def test_verify_rejects_fabricated_technical_value(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    tampered = dataclasses.replace(report, tech_params=[*report.tech_params, "回路电阻不大于9.99μΩ"])

    assert verify_report(tampered, evidence) is False


def test_verify_rejects_tampered_safety_distance(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    tampered = dataclasses.replace(
        report, safety_distance={**report.safety_distance, "no_outage_m": 9.9}
    )

    assert verify_report(tampered, evidence) is False


def test_verify_rejects_fabricated_acceptance_value(line_patrol):
    query, evidence = line_patrol
    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    tampered = dataclasses.replace(report, acceptance=[*report.acceptance, "温升不大于999K"])

    assert verify_report(tampered, evidence) is False


def test_verify_rejects_clause_outside_template(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    # 6.2.1 出现在证据（安全距离依据）里，但不是模板的依据条款，只能被条款校验拦下。
    tampered = dataclasses.replace(
        report, references=[{"standard_no": "DL/T 408—2023", "clause_no": "6.2.1", "title": "安全距离"}]
    )

    assert verify_report(tampered, evidence) is False


def test_verify_rejects_fabricated_reference(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    tampered = dataclasses.replace(
        report, references=[{"standard_no": "DL/T 9999", "clause_no": "1.1", "title": ""}]
    )

    assert verify_report(tampered, evidence) is False


def test_verify_does_not_scan_thinking(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)
    tampered = dataclasses.replace(report, thinking="已思考（用时 999 秒）")

    assert verify_report(tampered, evidence) is True


def test_verify_returns_false_instead_of_raising(switchgear_replace):
    query, evidence = switchgear_replace
    report = assemble_report(query, evidence, elapsed_seconds=1.0)

    assert verify_report(report, object()) is False


# ---------------------------------------------------------------------------
# 工具载荷
# ---------------------------------------------------------------------------
def test_produce_answer_payload_shape(switchgear_replace):
    query, evidence = switchgear_replace

    payload = produce_maintenance_plan_answer(query, evidence, elapsed_seconds=1.2)

    assert set(payload) == {"status", "answer_source", "report", "answer_markdown", "answer_verified"}
    assert payload["status"] == "ok"
    assert payload["answer_source"] == "maintenance_plan"
    assert payload["answer_verified"] is True
    assert payload["report"]["title"] == "3k18 断路器更换方案"
    assert payload["report"]["task_type"] == TASK_REPLACE
    assert payload["answer_markdown"].startswith("# 3k18 断路器更换方案")


def test_produce_answer_payload_is_verified_for_all_fixtures(any_case):
    query, evidence = any_case

    payload = produce_maintenance_plan_answer(query, evidence, elapsed_seconds=0.5)

    assert payload["answer_verified"] is True
    assert payload["report"]["title"] in payload["answer_markdown"]


def test_produce_answer_payload_report_matches_markdown_sections(any_case):
    query, evidence = any_case

    payload = produce_maintenance_plan_answer(query, evidence, elapsed_seconds=0.5)

    markdown = payload["answer_markdown"]
    for heading in SECTION_HEADINGS:
        assert heading in markdown
    assert payload["report"]["thinking"] not in markdown
