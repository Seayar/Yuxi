"""处置方案 seed 数据的离线预检（不连接图谱 / 数据库 / 大模型）。

预检在把 ``data/`` 下的六份 JSON 写入 TuGraph ``kg3`` 图之前运行，只做纯离线校验：
键名齐全、id 唯一、模板/备件的设备类型都在契约 ``DEVICE_TYPES`` 内、班组管辖区域都在
``ref/kg_ref.json`` 的台账名称中、模板占位符只用四个合法占位符、每个模板至少 1 条依据条款。

预检只报告问题，不做修复；``main`` 在发现问题时以非 0 退出码结束，供 seed CLI 在连图前拦截。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from yuxi.knowledge.maintenance_plan.contracts import DEVICE_TYPES

# 六份 seed 数据文件（三份作业模板合并为 templates）
TEMPLATE_FILES = (
    "templates_switchgear.json",
    "templates_transformer.json",
    "templates_line.json",
)
SAFETY_DISTANCE_FILE = "safety_distance.json"
TEAM_FILE = "teams.json"
SPARE_FILE = "spares.json"

# 各节点类型必须齐全的键名（“版本”缺省由 seed 补 "1"，不列为必需）
TEMPLATE_REQUIRED_KEYS = (
    "id",
    "名称",
    "作业类型",
    "设备类型",
    "作业对象",
    "适用关键词",
    "停电要求",
    "安全隔离措施",
    "工序顺序",
    "操作要点",
    "技术参数要求",
    "个人防护要求",
    "验电接地要求",
    "作业安全要求",
    "工器具清单",
    "材料清单",
    "验收标准",
    "注意事项",
    "人员配置建议",
    "依据条款",
    "来源说明",
)
SAFETY_DISTANCE_REQUIRED_KEYS = ("id", "电压等级", "依据标准", "条款号", "适用场所", "备注")
TEAM_REQUIRED_KEYS = ("id", "名称", "班组类型", "人数", "负责人", "数据来源", "管辖区域")
SPARE_REQUIRED_KEYS = (
    "id",
    "名称",
    "型号规格",
    "库存数量",
    "存放地点",
    "适用设备类型",
    "适用作业对象",
    "数据来源",
)

# 模板正文允许的占位符（00_总体设计.md 第 4 节）；依据条款是结构化 JSON，不参与占位符检查
LEGAL_PLACEHOLDERS = frozenset({"设备名称", "电压等级", "所在位置", "上级电源"})
PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]*)\}")
# 占位符只可能出现在这些自由文本/文本列表字段里（其余字段是标识、枚举或结构化 JSON）
PLACEHOLDER_FIELDS = (
    "安全隔离措施",
    "个人防护要求",
    "验电接地要求",
    "停电要求",
    "工序顺序",
    "操作要点",
    "技术参数要求",
    "作业安全要求",
    "工器具清单",
    "材料清单",
    "验收标准",
    "注意事项",
)

# kg_ref.json 中可作为区域名称来源的字段（台账里区域节点以 名称 / 层级路径 表达）
KG_REF_NAME_KEYS = ("name", "path", "parent", "child", "src", "dst", "type")


class PreflightError(Exception):
    """预检数据无法读取（文件缺失 / 非法 JSON）。"""


def load_kg_ref_names(kg_ref: dict[str, Any] | None) -> set[str]:
    """从 kg_ref.json 收集全部已知台账/区域名称（名称与层级路径逐段拆分）。"""

    names: set[str] = set()
    if not isinstance(kg_ref, dict):
        return names
    for value in kg_ref.values():
        rows = value if isinstance(value, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in KG_REF_NAME_KEYS:
                raw = row.get(key)
                if not isinstance(raw, str):
                    continue
                for part in raw.split(" > "):
                    text = part.strip()
                    if text:
                        names.add(text)
    return names


def preflight_seed(seed: dict[str, list[dict]], *, kg_ref: dict[str, Any] | None = None) -> dict[str, Any]:
    """对已加载的 seed 数据做全部离线检查，返回 ``{"ok","checks","issues"}``。

    ``kg_ref`` 为 None 时“班组管辖区域在台账中”无法被证明，按未通过处理：调用方要么提供
    ``ref/kg_ref.json``，要么显式接受预检失败，不能静默跳过。
    """

    templates = seed.get("templates") or []
    safety_distances = seed.get("safety_distances") or []
    teams = seed.get("teams") or []
    spares = seed.get("spares") or []

    checks: dict[str, dict[str, Any]] = {}
    issues: list[str] = []

    def record(name: str, problems: list[str], *, note: str = "") -> None:
        checks[name] = {"ok": not problems, "count": len(problems), "problems": problems[:50], "note": note}
        issues.extend(problems)

    # 1) 键名齐全
    record("键名齐全", _check_required_keys(templates, safety_distances, teams, spares))

    # 2) id 唯一（分类内唯一 + 跨类不冲突）
    record("id 唯一", _check_unique_ids(templates, safety_distances, teams, spares))

    # 3) 模板设备类型 ∈ DEVICE_TYPES
    record(
        "模板设备类型合法",
        [
            f"模板 {t.get('id')} 的设备类型“{t.get('设备类型')}”不在契约 DEVICE_TYPES 内"
            for t in templates
            if t.get("设备类型") not in DEVICE_TYPES
        ],
    )

    # 4) 班组管辖区域都在台账（kg_ref.json）中；缺少台账时该检查无法证明，按未通过处理
    if kg_ref is None:
        record("班组管辖区域在台账中", ["未提供 kg_ref.json，无法证明班组管辖区域在台账中"])
    else:
        known = load_kg_ref_names(kg_ref)
        record(
            "班组管辖区域在台账中",
            [
                f"班组 {team.get('id')} 的管辖区域“{area}”不在 kg_ref.json 的台账名称中"
                for team in teams
                for area in (team.get("管辖区域") or [])
                if area not in known
            ],
        )

    # 5) 备件适用设备类型 ∈ DEVICE_TYPES
    record(
        "备件适用设备类型合法",
        [
            f"备件 {spare.get('id')} 的适用设备类型“{device_type}”不在契约 DEVICE_TYPES 内"
            for spare in spares
            for device_type in (spare.get("适用设备类型") or [])
            if device_type not in DEVICE_TYPES
        ],
    )

    # 6) 模板占位符只用四个合法占位符
    record("模板占位符合法", _check_placeholders(templates))

    # 7) 每个模板至少 1 条依据条款
    record(
        "模板依据条款非空",
        [f"模板 {t.get('id')} 没有依据条款" for t in templates if not (t.get("依据条款") or [])],
    )

    return {"ok": not issues, "checks": checks, "issues": issues}


def run_preflight(data_dir: str | Path, *, kg_ref_path: str | Path | None = None) -> dict[str, Any]:
    """读取 ``data_dir`` 下的六份 JSON 与可选 kg_ref.json 后运行预检。

    显式给出 ``kg_ref_path`` 但文件不存在时直接失败，避免“想校验台账却静默跳过”。
    """

    # 延迟导入，避免 seed ↔ preflight 的模块级循环依赖
    from yuxi.knowledge.maintenance_plan.seed import load_plan_seed

    seed = load_plan_seed(data_dir)
    kg_ref: dict[str, Any] | None = None
    if kg_ref_path is not None:
        ref_path = Path(kg_ref_path)
        if not ref_path.is_file():
            raise PreflightError(f"kg_ref 文件不存在: {ref_path}")
        try:
            with ref_path.open(encoding="utf-8") as handle:
                kg_ref = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PreflightError(f"kg_ref 文件不是合法 JSON: {ref_path}: {exc}") from exc
    return preflight_seed(seed, kg_ref=kg_ref)


# ---------------------------------------------------------------------------
# 各项检查的实现
# ---------------------------------------------------------------------------
def _check_required_keys(
    templates: list[dict],
    safety_distances: list[dict],
    teams: list[dict],
    spares: list[dict],
) -> list[str]:
    """逐类检查必需键名是否齐全。"""

    groups = (
        ("作业模板", templates, TEMPLATE_REQUIRED_KEYS),
        ("安全距离", safety_distances, SAFETY_DISTANCE_REQUIRED_KEYS),
        ("班组", teams, TEAM_REQUIRED_KEYS),
        ("备品备件", spares, SPARE_REQUIRED_KEYS),
    )
    problems: list[str] = []
    for label, rows, required in groups:
        for row in rows:
            missing = [key for key in required if key not in row]
            if missing:
                problems.append(f"{label} {row.get('id')} 缺键名：{'、'.join(missing)}")
    return problems


def _check_unique_ids(
    templates: list[dict],
    safety_distances: list[dict],
    teams: list[dict],
    spares: list[dict],
) -> list[str]:
    """分类内 id 必须唯一；四类 id 之间也不得冲突。"""

    problems: list[str] = []
    seen: dict[str, str] = {}
    groups = (
        ("作业模板", templates),
        ("安全距离", safety_distances),
        ("班组", teams),
        ("备品备件", spares),
    )
    for label, rows in groups:
        local: set[str] = set()
        for row in rows:
            row_id = str(row.get("id") or "").strip()
            if not row_id:
                problems.append(f"{label} 存在空 id")
                continue
            if row_id in local:
                problems.append(f"{label} 内 id 重复：{row_id}")
            local.add(row_id)
            if row_id in seen and seen[row_id] != label:
                problems.append(f"id 跨类型重复：{row_id}（{seen[row_id]} / {label}）")
            seen.setdefault(row_id, label)
    return problems


def _check_placeholders(templates: list[dict]) -> list[str]:
    """模板正文（不含结构化 JSON 字段）的占位符必须都在 LEGAL_PLACEHOLDERS 内。"""

    problems: list[str] = []
    for template in templates:
        for field in PLACEHOLDER_FIELDS:
            for text in _field_texts(template.get(field)):
                for name in PLACEHOLDER_PATTERN.findall(text):
                    if name not in LEGAL_PLACEHOLDERS:
                        problems.append(f"模板 {template.get('id')} 字段“{field}”使用了非法占位符 {{{name}}}")
    return problems


def _field_texts(value: Any) -> list[str]:
    """把一个字段值展开成待检查的字符串列表。"""

    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="处置方案 seed 数据离线预检（不连接图谱）")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parent / "data"), help="seed 数据目录")
    parser.add_argument("--kg-ref", default="", help="ref/kg_ref.json 路径（提供时校验班组管辖区域）")
    args = parser.parse_args()

    try:
        report = run_preflight(args.data_dir, kg_ref_path=args.kg_ref or None)
    except PreflightError as exc:
        print(f"预检失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
