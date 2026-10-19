"""preflight.py 单元测试：离线预检逻辑（不连接图谱 / 数据库 / 大模型）。

含一条对包内真实数据的结构预检；管辖区域检查使用明确的台账名称夹具，
以及各单项检查在坏数据上必须报错的用例。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuxi.knowledge.maintenance_plan.preflight import preflight_seed, run_preflight
from yuxi.knowledge.maintenance_plan import preflight as preflight_module

pytestmark = pytest.mark.unit

DATA_DIR = Path(preflight_module.__file__).resolve().parent / "data"
KG_REF = {"areas": [{"name": "#1开闭所"}]}


def valid_seed() -> dict[str, list[dict]]:
    return {
        "templates": [
            {
                "id": "TPL#开关柜#更换#断路器",
                "名称": "开关柜断路器更换",
                "作业类型": "更换",
                "设备类型": "开关柜",
                "作业对象": "断路器",
                "适用关键词": ["断路器"],
                "停电要求": "需停电",
                "安全隔离措施": "断开{设备名称}上级电源（{上级电源}）并验电接地，位置{所在位置}。",
                "工序顺序": ["断开电源"],
                "操作要点": ["逐芯编号"],
                "技术参数要求": ["回路电阻≤20%"],
                "个人防护要求": "穿戴{电压等级}绝缘手套",
                "验电接地要求": "两侧验电接地",
                "作业安全要求": ["专人监护"],
                "工器具清单": ["力矩扳手"],
                "材料清单": ["导电膏"],
                "验收标准": ["位置指示正确"],
                "注意事项": ["确认隔离挡板封闭"],
                "人员配置建议": "4人",
                "依据条款": [{"standard_id": "DLT408", "条款号": "6.2.2", "用途": "停电"}],
                "来源说明": "通用",
            }
        ],
        "safety_distances": [
            {
                "id": "SD#10kV#变电",
                "电压等级": "10kV",
                "设备不停电安全距离_m": 0.7,
                "工作中正常活动范围安全距离_m": 0.35,
                "带电作业安全距离_m": 0.4,
                "依据标准": "DLT408",
                "条款号": "5.3.1.3",
                "适用场所": "变电",
                "备注": "",
            }
        ],
        "teams": [
            {
                "id": "TEAM#开闭所运维一班",
                "名称": "开闭所运维一班",
                "班组类型": "运维",
                "人数": 4,
                "负责人": "李工",
                "数据来源": "演示数据",
                "管辖区域": ["#1开闭所"],
            }
        ],
        "spares": [
            {
                "id": "SPARE#开关柜#断路器手车",
                "名称": "真空断路器手车",
                "型号规格": "VS1-12",
                "库存数量": 2,
                "存放地点": "#3开闭所备品间",
                "适用设备类型": ["开关柜", "断路器"],
                "适用作业对象": ["断路器"],
                "数据来源": "演示数据",
            }
        ],
    }


def test_real_data_preflight_ok():
    """包内六份真实数据的结构检查通过；区域匹配由独立夹具验证。"""

    report = run_preflight(DATA_DIR)
    assert report["ok"] is True, report["issues"]
    for name, check in report["checks"].items():
        assert check["ok"], (name, check["problems"])
    # 七项检查都在
    assert set(report["checks"]) == {
        "键名齐全",
        "id 唯一",
        "模板设备类型合法",
        "班组管辖区域在台账中",
        "备件适用设备类型合法",
        "模板占位符合法",
        "模板依据条款非空",
    }


def test_valid_synthetic_seed_ok():
    report = preflight_seed(valid_seed())
    assert report["ok"] is True
    assert report["checks"]["班组管辖区域在台账中"]["note"] == "未提供 kg_ref.json，跳过"


def test_missing_key_detected():
    seed = valid_seed()
    del seed["templates"][0]["作业对象"]
    report = preflight_seed(seed)
    assert report["ok"] is False
    assert any("缺键名" in issue and "作业对象" in issue for issue in report["issues"])


def test_duplicate_id_detected():
    seed = valid_seed()
    seed["teams"].append(dict(seed["teams"][0]))
    report = preflight_seed(seed)
    assert report["ok"] is False
    assert any("id 重复" in issue for issue in report["issues"])


def test_bad_template_device_type_detected():
    seed = valid_seed()
    seed["templates"][0]["设备类型"] = "不存在的类型"
    report = preflight_seed(seed)
    assert report["ok"] is False
    assert any("DEVICE_TYPES" in issue for issue in report["issues"])


def test_bad_spare_device_type_detected():
    seed = valid_seed()
    seed["spares"][0]["适用设备类型"] = ["开关柜", "未知类型"]
    report = preflight_seed(seed)
    assert report["ok"] is False
    assert any("适用设备类型" in issue for issue in report["issues"])


def test_team_area_not_in_kg_ref_detected():
    seed = valid_seed()
    seed["teams"][0]["管辖区域"] = ["#1开闭所", "不存在的区域"]
    kg_ref = KG_REF
    report = preflight_seed(seed, kg_ref=kg_ref)
    assert report["ok"] is False
    assert any("不存在的区域" in issue for issue in report["issues"])


def test_illegal_placeholder_detected():
    seed = valid_seed()
    seed["templates"][0]["安全隔离措施"] = "断开{设备名称}并检查{未知占位符}"
    report = preflight_seed(seed)
    assert report["ok"] is False
    assert any("未知占位符" in issue for issue in report["issues"])


def test_clause_json_braces_not_treated_as_placeholder():
    """依据条款是结构化 JSON，其中的花括号不得被当成占位符。"""

    seed = valid_seed()
    report = preflight_seed(seed)
    assert report["checks"]["模板占位符合法"]["ok"] is True


def test_template_without_clause_detected():
    seed = valid_seed()
    seed["templates"][0]["依据条款"] = []
    report = preflight_seed(seed)
    assert report["ok"] is False
    assert any("没有依据条款" in issue for issue in report["issues"])
