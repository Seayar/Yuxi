"""处置方案装配与校验单元测试（不连接图谱/数据库/大模型）。"""

from __future__ import annotations

import logging
from dataclasses import replace

from yuxi.knowledge.maintenance_plan.answer import (
    assemble_report,
    build_plan_markdown,
    format_resource,
    format_safety,
    format_step,
    produce_maintenance_plan_answer,
    verify_report,
)
from yuxi.knowledge.maintenance_plan.contracts import (
    BranchSelection,
    DeviceCandidate,
    PersonnelAssignment,
    PersonnelRequirement,
    ResourceRequirement,
    SafetyMeasure,
    Scenario,
    ScenarioEvidence,
    Step,
)


def make_evidence(*, branch_source: str = "redis") -> ScenarioEvidence:
    device = DeviceCandidate(
        "LGR#3k18",
        "3k18",
        "开关柜",
        "10kV",
        "#3开闭所 > 3k18",
        ledger_ids=("L1",),
    )
    scenario = Scenario(
        "SCENE_A2",
        "开关柜整柜更换",
        description="柜体锈蚀、五防失效",
        level="A级",
        branch_dimension="布置位置",
        branch1_condition="站内开关室",
        branch2_condition="箱变内",
    )
    branch = BranchSelection("布置位置", "站内开关室", "分支1", source=branch_source, ledger_id="L1")
    return ScenarioEvidence(
        device=device,
        scenario=scenario,
        branch=branch,
        steps=[
            Step("S1", 1, "停电、验电"),
            Step("S2", 2, "拆除旧柜"),
        ],
        safety=[
            SafetyMeasure("SAFE1", "个人防护", "安全帽", common=True),
            SafetyMeasure("SAFE2", "分支措施", "站内围栏", common=False, applicable_branch="分支1"),
        ],
        resources=[
            ResourceRequirement("RES1", "吊车", 2, unit="台", common=True, in_stock=True, stock_note="在库"),
            ResourceRequirement(
                "RES2",
                "绝缘隔板",
                0,
                quantity_requirement="4 块／1 套",
                common=False,
                applicable_branch="分支1",
                stock_note="清单未匹配",
            ),
        ],
        personnel=[
            PersonnelRequirement("PER1", "工作负责人", 1, common=True),
            PersonnelRequirement("PER2", "检修工", 3, common=True),
            PersonnelRequirement("PER3", "专责监护人", 2, common=False, applicable_branch="分支1"),
        ],
    )


def make_assignment() -> PersonnelAssignment:
    return PersonnelAssignment(team="1号站监控调度班组", leader="王伟", total=6)


def test_format_helpers_cover_quantity_and_branch_fields():
    assert format_step(Step("S1", 1, "停电、验电")) == "1. 停电、验电"
    assert format_safety(SafetyMeasure("S", "个人防护", "安全帽", True)) == "个人防护：安全帽"
    assert format_resource(ResourceRequirement("R", "吊车", 2, unit="台")) == "吊车 2 台"
    assert format_resource(ResourceRequirement("R", "绝缘隔板", 0, quantity_requirement="4 块／1 套")) == "绝缘隔板（4 块／1 套）"
    assert format_resource(ResourceRequirement("R", "吊车", 2, unit="台", stock_note="在库")) == "吊车 2 台【在库】"
    assert format_resource(ResourceRequirement("R", "绝缘靴", 1)) == "绝缘靴 1"


def test_assemble_report_and_markdown_include_branch_and_sections():
    report = assemble_report(make_evidence(), make_assignment(), data_notes=[], elapsed_seconds=1.2)
    markdown = build_plan_markdown(report)

    assert report.branch_dimension == "布置位置"
    assert report.branch_condition == "站内开关室"
    assert report.branch == "分支1"
    assert report.personnel_total == 6
    assert "执行分支：布置位置 = 站内开关室（分支1）" in markdown
    assert "## 一、检修步骤" in markdown
    assert "## 二、安全措施" in markdown
    assert "## 三、所需工器具及材料" in markdown
    assert "## 四、责任班组与人员安排" in markdown
    assert "- 责任班组：1号站监控调度班组" in markdown
    assert "- 所需人数：6 人" in markdown
    assert "- 工作负责人：王伟" in markdown
    assert "## 五、数据说明" not in markdown
    assert "吊车 2 台【在库】" in markdown


def test_verify_report_accepts_assembled_report():
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    assert verify_report(report, evidence, assignment) is True


def test_verify_report_rejects_tampered_branch(caplog):
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, branch_condition="箱变内")
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(tampered, evidence, assignment) is False

    assert "分支与证据不一致" in caplog.text


def test_verify_report_rejects_tampered_resource_list():
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, resources=[*report.resources, "伪造 99 台"])
    assert verify_report(tampered, evidence, assignment) is False


def test_produce_payload_marks_verified_and_source():
    payload = produce_maintenance_plan_answer(
        make_evidence(), make_assignment(), data_notes=[], elapsed_seconds=0.5
    )

    assert payload["answer_source"] == "maintenance_plan"
    assert payload["answer_verified"] is True
    assert payload["answer_markdown"].startswith("# 3k18 开关柜整柜更换处置方案")


# ---------------------------------------------------------------------------
# verify_report 的每一类检查各配一个负向用例，并断言对应的 warning 文案，
# 保证用例因正确原因失败（注释掉任一检查即有对应用例失败）。
# ---------------------------------------------------------------------------
def test_verify_report_rejects_tampered_steps(caplog):
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, steps=[*report.steps, "3. 伪造步骤"])
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(tampered, evidence, assignment) is False

    assert "步骤与证据不一致" in caplog.text


def test_verify_report_rejects_tampered_safety(caplog):
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, safety=["个人防护：伪造内容"])
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(tampered, evidence, assignment) is False

    assert "安全措施与证据不一致" in caplog.text


def test_verify_report_rejects_personnel_roster_mismatch(caplog):
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, personnel=["工作负责人：伪造"])
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(tampered, evidence, assignment) is False

    assert "人员安排与证据不一致" in caplog.text


def test_verify_report_rejects_tampered_team(caplog):
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, team="伪造班组")
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(tampered, evidence, assignment) is False

    assert "责任班组与证据不一致" in caplog.text


def test_verify_report_rejects_personnel_total_mismatch(caplog):
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, personnel_total=5)
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(tampered, evidence, assignment) is False

    assert "人员合计与证据不一致" in caplog.text


def test_verify_report_rejects_fabricated_number(caplog):
    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, title=f"{report.title}（2099 版）")
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(tampered, evidence, assignment) is False

    assert "2099 不在证据中" in caplog.text


def test_verify_report_rejects_removed_resource_line(caplog):
    """删除一行资源（不带新数字）必须打穿资源检查本身，而不是被数字门误认领。"""

    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    tampered = replace(report, resources=report.resources[:-1])
    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(tampered, evidence, assignment) is False

    assert "资源清单与证据不一致" in caplog.text


def test_verify_report_accepts_single_leader_for_total_headcount(caplog):
    """所需人数来自人员需求合计；只点名工作负责人不再要求名单数量等于人数。"""

    evidence = make_evidence()
    assignment = make_assignment()
    report = assemble_report(evidence, assignment, data_notes=[], elapsed_seconds=1.0)

    with caplog.at_level(logging.WARNING, logger="yuxi.knowledge.maintenance_plan.answer"):
        assert verify_report(report, evidence, assignment) is True

    assert "人员名单数量与需求人数不一致" not in caplog.text


def test_assemble_report_marks_missing_leader():
    """人员 JSON 不可用时只保留责任班组与所需人数，工作负责人显示占位。"""

    assignment = PersonnelAssignment(team="电力巡护班组", leader="", total=6, note="人员数据未接入 MinIO")

    markdown = build_plan_markdown(
        assemble_report(make_evidence(), assignment, data_notes=[], elapsed_seconds=1.0)
    )

    assert "- 责任班组：电力巡护班组" in markdown
    assert "- 工作负责人：未接入" in markdown
