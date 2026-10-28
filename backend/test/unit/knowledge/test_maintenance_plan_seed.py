"""seed.py 单元测试：全部使用假 driver，不连接图谱 / 数据库 / 大模型。

覆盖任务卡要求的离线证据：标签注册只在未注册时执行、模板 JSON 字段被序列化为字符串、
null 距离不 SET、条款号后缀归一匹配、未匹配条款/区域被收集、重复运行语句完全相同、
Cypher 里没有反引号也没有拼接用户数据、dry-run 不创建 driver、CLI 不设默认密码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuxi.knowledge.maintenance_plan import seed as seed_module
from yuxi.knowledge.maintenance_plan.seed import load_plan_seed, seed_plan_graph_kg

from maintenance_plan_fake_driver import FakeDriver

pytestmark = pytest.mark.unit

T10_DIR = Path(seed_module.__file__).resolve().parent
DATA_DIR = T10_DIR / "data"


# ---------------------------------------------------------------------------
# 夹具数据（结构取自 out/T3–T6，数值/条款号原样保留）
# ---------------------------------------------------------------------------
def sample_seed() -> dict[str, list[dict]]:
    return {
        "templates": [
            {
                "id": "TPL#开关柜#更换#断路器",
                "名称": "开关柜断路器（手车）更换",
                "作业类型": "更换",
                "设备类型": "开关柜",
                "作业对象": "断路器（手车）",
                "适用关键词": ["断路器", "开关", "手车"],
                "停电要求": "需停电",
                "安全隔离措施": "断开{设备名称}上级电源（{上级电源}）后验电接地。",
                "工序顺序": ["断开电源", "拆旧断路器"],
                "操作要点": ["逐芯编号"],
                "技术参数要求": ["回路电阻不大于制造厂规定值"],
                "个人防护要求": "穿戴{电压等级}绝缘手套",
                "验电接地要求": "两侧验电接地",
                "作业安全要求": ["专人监护"],
                "工器具清单": ["力矩扳手"],
                "材料清单": ["导电膏"],
                "验收标准": ["位置指示正确"],
                "注意事项": ["确认隔离挡板封闭"],
                "人员配置建议": "4人",
                "依据条款": [
                    {"standard_id": "DLT408", "条款号": "6.2.2", "用途": "停电"},
                    {"standard_id": "DLT393", "条款号": "6.21.3.4（表97）", "用途": "主回路电阻"},
                ],
                "来源说明": "通用检修工艺",
            }
        ],
        "safety_distances": [
            {
                "id": "SD#10kV#变电",
                "电压等级": "10kV",
                "设备不停电安全距离_m": None,
                "工作中正常活动范围安全距离_m": 0.35,
                "带电作业安全距离_m": 0.4,
                "依据标准": "DLT408",
                "条款号": "5.3.1.3 a)",
                "适用场所": "变电",
                "备注": "无低压数值",
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
                "型号规格": "VS1-12/1250-25",
                "库存数量": 2,
                "存放地点": "#3开闭所备品间",
                "适用设备类型": ["开关柜", "断路器"],
                "适用作业对象": ["断路器"],
                "数据来源": "演示数据",
            }
        ],
    }


def fake_graph(**overrides) -> FakeDriver:
    """默认假图谱：含 DLT408/6.2.2、DLT393/6.21.3.4 两条条款与一个 #1开闭所 设备。"""

    kwargs = dict(
        clauses={
            "DLT408": {"6.2.2": "CL#DLT408#6.2.2"},
            "DLT393": {"6.21.3.4": "CL#DLT393#6.21.3.4"},
        },
        devices={"#1开闭所": [{"id": "LGR#1", "path": "变电设施 > #1开闭所"}]},
    )
    kwargs.update(overrides)
    return FakeDriver(**kwargs)


def install(monkeypatch, driver: FakeDriver) -> FakeDriver:
    monkeypatch.setattr(seed_module, "_open_driver", lambda **kwargs: driver)
    return driver


def run_seed(seed=None) -> dict:
    return seed_plan_graph_kg(
        uri="bolt://fake:7688",
        graph="kg3",
        username="admin",
        password="not-used-in-test",
        seed=seed or sample_seed(),
    )


def test_seed_read_queries_avoid_reserved_limit_parameter():
    """真实 TuGraph 不接受 $limit，条款和区域查询均使用非保留参数名。"""

    driver = fake_graph()
    seed_module._query_standard_clauses(driver, standard_id="DLT408")
    seed_module._query_devices_by_name(driver, name="#1开闭所")
    assert len(driver.calls) == 2
    for (cypher, params), expected in zip(
        driver.calls, (seed_module.CLAUSE_QUERY_LIMIT, seed_module.DEVICE_QUERY_LIMIT), strict=True
    ):
        assert "LIMIT $node_limit" in cypher
        assert "$limit" not in cypher
        assert params["node_limit"] == expected


# ---------------------------------------------------------------------------
# 标签注册
# ---------------------------------------------------------------------------
def test_schema_registered_only_when_missing(monkeypatch):
    """已注册的标签跳过，未注册的才建；全部属性与类型都显式声明。"""

    driver = install(monkeypatch, fake_graph(registered_vertex=("班组",), registered_edge=("适用模板",)))
    result = run_seed()

    vertex_cyphers = driver.created_vertex_labels()
    edge_cyphers = driver.created_edge_labels()
    # 班组 / 适用模板 已注册 → 不重复建
    assert not any("'班组'" in c for c in vertex_cyphers)
    assert not any("'适用模板'" in c for c in edge_cyphers)
    # 其余标签都建
    assert any("'作业模板'" in c for c in vertex_cyphers)
    assert any("'安全距离'" in c for c in vertex_cyphers)
    assert any("'备品备件'" in c for c in vertex_cyphers)
    assert any("'依据条款'" in c for c in edge_cyphers)
    assert any("'管辖'" in c for c in edge_cyphers)
    assert any("'配备备件'" in c for c in edge_cyphers)
    assert result["schema"] == {"vertex": 3, "edge": 3}

    template_label = next(c for c in vertex_cyphers if "'作业模板'" in c)
    # 强 schema：主键 id 必填，数组字段用 STRING，距离 DOUBLE，人数/库存 INT64
    assert template_label.startswith("CALL db.createVertexLabel('作业模板', 'id', 'id', 'STRING', false")
    for field in ("工序顺序", "操作要点", "技术参数要求", "适用关键词", "依据条款"):
        assert f"'{field}', 'STRING', true" in template_label
    assert "'INT64', true" in next(c for c in vertex_cyphers if "'备品备件'" in c)
    assert "'用途', 'STRING', true" in next(c for c in edge_cyphers if "'依据条款'" in c)


def test_schema_declares_all_property_types(monkeypatch):
    """全新图上注册：全部属性、正确类型、除 id 外 optional。"""

    driver = install(monkeypatch, fake_graph())
    run_seed()
    by_label = {c.split("'")[1]: c for c in driver.created_vertex_labels()}

    assert "'人数', 'INT64', true" in by_label["班组"]
    assert "'管辖区域', 'STRING', true" in by_label["班组"]
    assert "'库存数量', 'INT64', true" in by_label["备品备件"]
    assert "'适用设备类型', 'STRING', true" in by_label["备品备件"]
    assert "'适用作业对象', 'STRING', true" in by_label["备品备件"]
    distance = by_label["安全距离"]
    assert "'设备不停电安全距离_m', 'DOUBLE', true" in distance
    assert "'工作中正常活动范围安全距离_m', 'DOUBLE', true" in distance
    assert "'带电作业安全距离_m', 'DOUBLE', true" in distance
    assert "'电压等级', 'STRING', true" in distance
    assert "'备注', 'STRING', true" in distance
    # 除主键 id 外全部 optional=true
    for label, cypher in by_label.items():
        assert "'id', 'STRING', false" in cypher
        assert cypher.count(", false") == 1

    edge_by_label = {c.split("'")[1]: c for c in driver.created_edge_labels()}
    assert edge_by_label["依据条款"].startswith("CALL db.createEdgeLabel('依据条款', '[[\"作业模板\",\"标准条款\"]]'")
    assert edge_by_label["适用模板"].startswith("CALL db.createEdgeLabel('适用模板', '[[\"设备类型\",\"作业模板\"]]'")
    assert edge_by_label["管辖"].startswith("CALL db.createEdgeLabel('管辖', '[[\"班组\",\"设备实例\"]]'")
    assert edge_by_label["配备备件"].startswith("CALL db.createEdgeLabel('配备备件', '[[\"设备类型\",\"备品备件\"]]'")


def test_schema_skipped_when_all_registered(monkeypatch):
    driver = install(
        monkeypatch,
        fake_graph(
            registered_vertex=("作业模板", "安全距离", "班组", "备品备件"),
            registered_edge=("适用模板", "依据条款", "管辖", "配备备件"),
        ),
    )
    result = run_seed()
    assert driver.created_vertex_labels() == []
    assert driver.created_edge_labels() == []
    assert result["schema"] == {"vertex": 0, "edge": 0}


# ---------------------------------------------------------------------------
# 节点写入
# ---------------------------------------------------------------------------
def test_template_json_fields_serialized_as_strings(monkeypatch):
    driver = install(monkeypatch, fake_graph())
    run_seed()

    _cypher, params = driver.merges("作业模板")[0]
    assert params["pid"] == "TPL#开关柜#更换#断路器"
    assert params["name"] == "开关柜断路器（手车）更换"
    # 数组字段以 JSON 字符串写入，且能原样解析回来
    assert isinstance(params["step_sequence"], str)
    assert json.loads(params["step_sequence"]) == ["断开电源", "拆旧断路器"]
    assert json.loads(params["keywords"]) == ["断路器", "开关", "手车"]
    assert json.loads(params["clauses"])[0]["standard_id"] == "DLT408"
    # 版本缺省补 "1"
    assert params["version"] == "1"


def test_template_version_preserved(monkeypatch):
    seed = sample_seed()
    seed["templates"][0]["版本"] = "1.0"
    install(monkeypatch, fake_graph())
    run_seed(seed)
    assert seed_module._template_props(seed["templates"][0])["版本"] == "1.0"


def test_null_distance_not_set(monkeypatch):
    driver = install(monkeypatch, fake_graph())
    run_seed()

    cypher, params = driver.merges("安全距离")[0]
    assert "设备不停电安全距离_m" not in cypher
    assert "no_outage_m" not in params
    # 有值的距离照常 SET
    assert "工作中正常活动范围安全距离_m" in cypher
    assert params["work_range_m"] == pytest.approx(0.35)
    assert params["live_work_m"] == pytest.approx(0.4)


def test_team_and_spare_json_fields(monkeypatch):
    driver = install(monkeypatch, fake_graph())
    run_seed()

    _c, team_params = driver.merges("班组")[0]
    assert json.loads(team_params["areas"]) == ["#1开闭所"]
    assert team_params["member_count"] == 4
    _c, spare_params = driver.merges("备品备件")[0]
    assert json.loads(spare_params["device_types"]) == ["开关柜", "断路器"]
    assert spare_params["quantity"] == 2


# ---------------------------------------------------------------------------
# 边写入与匹配
# ---------------------------------------------------------------------------
def test_edges_written_with_two_match(monkeypatch):
    driver = install(monkeypatch, fake_graph())
    result = run_seed()

    template_edge = driver.edges("适用模板")[0]
    assert template_edge[0].count("MATCH") == 2
    assert template_edge[1] == {"device_type": "开关柜", "pid": "TPL#开关柜#更换#断路器"}
    assert result["edges"]["适用模板"] == 1
    assert result["edges"]["配备备件"] == 2  # 备件适用 开关柜 + 断路器
    assert result["edges"]["管辖"] == 1
    assert driver.closed is True


def test_clause_suffix_normalization_matches(monkeypatch):
    """条款号“6.21.3.4（表97）”去尾部后缀后命中 6.21.3.4。"""

    driver = install(monkeypatch, fake_graph())
    result = run_seed()

    assert result["unmatched_clauses"] == []
    assert result["edges"]["依据条款"] == 2
    clause_edges = driver.edges("依据条款")
    matched = {c[1]["cid"] for c in clause_edges}
    assert matched == {"CL#DLT408#6.2.2", "CL#DLT393#6.21.3.4"}
    assert any(c[1].get("usage") == "主回路电阻" for c in clause_edges)


def test_clause_full_scan_normalization(monkeypatch):
    """精确与归一化精确都不中时，按标准整取条款再归一化匹配。"""

    driver = install(
        monkeypatch,
        fake_graph(clauses={"DLT408": {"6.2.2 a)": "CL#DLT408#6.2.2"}}),
    )
    seed = sample_seed()
    seed["templates"][0]["依据条款"] = [{"standard_id": "DLT408", "条款号": "6.2.2（表1）", "用途": "停电"}]
    result = run_seed(seed)

    assert result["unmatched_clauses"] == []
    assert result["edges"]["依据条款"] == 1
    assert driver.edges("依据条款")[0][1]["cid"] == "CL#DLT408#6.2.2"


def test_unmatched_clauses_collected(monkeypatch):
    install(monkeypatch, fake_graph(clauses={"DLT408": {"6.2.2": "CL#DLT408#6.2.2"}}))
    result = run_seed()

    assert result["edges"]["依据条款"] == 1
    assert len(result["unmatched_clauses"]) == 1
    unmatched = result["unmatched_clauses"][0]
    assert unmatched["standard_id"] == "DLT393"
    assert unmatched["条款号"] == "6.21.3.4（表97）"


def test_unmatched_area_when_no_device(monkeypatch):
    install(monkeypatch, fake_graph(devices={}))
    result = run_seed()
    assert result["edges"]["管辖"] == 0
    assert len(result["unmatched_areas"]) == 1
    assert "未在设备台账中找到" in result["unmatched_areas"][0]["reason"]


def test_duplicate_area_not_silently_chosen(monkeypatch):
    """重名区域且无路径时不得静默任选：记入 unmatched_areas，且不建 管辖 边。"""

    install(
        monkeypatch,
        fake_graph(
            devices={
                "#1开闭所": [
                    {"id": "LGR#A", "path": "变电设施 > #1开闭所"},
                    {"id": "LGR#B", "path": "输电设施 > #1开闭所"},
                ]
            }
        ),
    )
    result = run_seed()
    assert result["edges"]["管辖"] == 0
    assert len(result["unmatched_areas"]) == 1
    assert "重名" in result["unmatched_areas"][0]["reason"]


def test_duplicate_area_disambiguated_by_path(monkeypatch):
    driver = install(
        monkeypatch,
        fake_graph(
            devices={
                "#1开闭所": [
                    {"id": "LGR#A", "path": "变电设施 > #1开闭所"},
                    {"id": "LGR#B", "path": "输电设施 > #1开闭所"},
                ]
            }
        ),
    )
    seed = sample_seed()
    seed["teams"][0]["管辖区域路径"] = {"#1开闭所": "输电设施 > #1开闭所"}
    result = run_seed(seed)

    assert result["unmatched_areas"] == []
    assert result["edges"]["管辖"] == 1
    assert driver.edges("管辖")[0][1]["did"] == "LGR#B"


# ---------------------------------------------------------------------------
# 幂等与安全
# ---------------------------------------------------------------------------
def test_repeated_run_generates_identical_statements(monkeypatch):
    driver_a = install(monkeypatch, fake_graph())
    run_seed()
    driver_b = install(monkeypatch, fake_graph())
    run_seed()

    assert driver_a.cyphers() == driver_b.cyphers()
    assert [p for _, p in driver_a.calls] == [p for _, p in driver_b.calls]


def test_cypher_has_no_backticks_and_no_user_data(monkeypatch):
    driver = install(monkeypatch, fake_graph())
    run_seed()

    for cypher, _params in driver.calls:
        assert "`" not in cypher
        # 用户数据（模板名称、班组负责人）不得出现在语句文本里，只能走参数
        assert "开关柜断路器（手车）更换" not in cypher
        assert "李工" not in cypher
        assert "真空断路器手车" not in cypher

    joined_params = json.dumps([p for _, p in driver.calls], ensure_ascii=False)
    assert "开关柜断路器（手车）更换" in joined_params
    assert "李工" in joined_params


# ---------------------------------------------------------------------------
# dry-run 与 CLI
# ---------------------------------------------------------------------------
def test_dry_run_does_not_create_driver(monkeypatch):
    class _ExplodingGraphDatabase:
        @staticmethod
        def driver(*args, **kwargs):  # pragma: no cover - dry-run 不应走到
            raise AssertionError("dry-run 不得创建 driver")

    monkeypatch.setattr(seed_module, "GraphDatabase", _ExplodingGraphDatabase)

    def _explode(**kwargs):  # pragma: no cover - dry-run 不应走到
        raise AssertionError("dry-run 不得创建 driver")

    monkeypatch.setattr(seed_module, "_open_driver", _explode)

    result = seed_plan_graph_kg(
        uri="bolt://fake:7688",
        graph="kg3",
        username="admin",
        password="",
        seed=sample_seed(),
        dry_run=True,
        kg_ref={"areas": [{"name": "#1开闭所"}]},
    )
    assert result["status"] == "dry_run"
    assert result["templates"] == 1
    assert result["edges"]["依据条款"] == 2
    assert result["statements"]["nodes"] == 4
    assert result["unmatched_clauses"] == []
    assert result["preflight"]["ok"] is True


def test_main_requires_password_env(monkeypatch, capsys):
    monkeypatch.delenv("KG_PASSWORD", raising=False)
    monkeypatch.setattr(
        seed_module, "_open_driver", lambda **kwargs: pytest.fail("缺密码时不得创建 driver")
    )
    monkeypatch.setattr(
        seed_module,
        "load_plan_seed",
        lambda data_dir: sample_seed(),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["seed.py", "--uri", "bolt://fake:7688", "--data-dir", str(DATA_DIR)],
    )
    assert seed_module.main() == 1
    assert "KG_PASSWORD" in capsys.readouterr().err


def test_main_dry_run_offline(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        seed_module, "_open_driver", lambda **kwargs: pytest.fail("dry-run 不得创建 driver")
    )
    monkeypatch.setattr(seed_module, "load_plan_seed", lambda data_dir: sample_seed())
    kg_ref = tmp_path / "kg_ref.json"
    kg_ref.write_text(json.dumps({"areas": [{"name": "#1开闭所"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["seed.py", "--uri", "bolt://fake:7688", "--dry-run", "--kg-ref", str(kg_ref)],
    )
    assert seed_module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["statements"]["total"] > 0


def test_main_requires_kg_ref(monkeypatch, capsys):
    """缺少 kg_ref 时导入必须被拒绝，不能静默跳过管辖区域检查。"""

    monkeypatch.setattr(seed_module, "load_plan_seed", lambda data_dir: sample_seed())
    monkeypatch.setenv("KG_PASSWORD", "unused")
    monkeypatch.setattr(
        seed_module, "_open_driver", lambda **kwargs: pytest.fail("缺 kg_ref 时不得创建 driver")
    )
    monkeypatch.setattr("sys.argv", ["seed.py", "--uri", "bolt://fake:7688", "--dry-run"])

    assert seed_module.main() == 1
    assert "kg-ref" in capsys.readouterr().err


def test_edge_readback_reports_edges_that_were_not_written(monkeypatch):
    """设备类型节点由主知识库维护；缺失时边没写成，回读必须把它记成未写入。"""

    install(monkeypatch, fake_graph())

    result = run_seed()

    assert result["edges"][seed_module.REL_HAS_TEMPLATE] == 1
    assert result["edges_verified"][seed_module.REL_HAS_TEMPLATE] == 0
    assert result["edges_verified"][seed_module.REL_TEMPLATE_CLAUSE] == 2
    assert result["edges_verified"][seed_module.REL_MANAGES] == 1
    assert {
        (item["relation"], item["source"], item["target"]) for item in result["unmatched_edges"]
    } == {
        (seed_module.REL_HAS_TEMPLATE, "开关柜", "TPL#开关柜#更换#断路器"),
        (seed_module.REL_SPARE, "开关柜", "SPARE#开关柜#断路器手车"),
        (seed_module.REL_SPARE, "断路器", "SPARE#开关柜#断路器手车"),
    }


def test_edge_readback_confirms_edges_when_endpoints_exist(monkeypatch):
    """设备类型节点存在时，回读确认的边数应与发出的边数一致。"""

    install(monkeypatch, fake_graph(device_types=("开关柜", "断路器")))

    result = run_seed()

    assert result["edges_verified"] == result["edges"]
    assert result["unmatched_edges"] == []


def test_load_plan_seed_reads_real_data():
    seed = load_plan_seed(DATA_DIR)
    assert len(seed["templates"]) == 59
    assert len(seed["safety_distances"]) == 14
    assert len(seed["teams"]) == 10
    assert len(seed["spares"]) == 37


def test_load_plan_seed_missing_file_raises(tmp_path):
    with pytest.raises(seed_module.KgSeedError):
        load_plan_seed(tmp_path)
