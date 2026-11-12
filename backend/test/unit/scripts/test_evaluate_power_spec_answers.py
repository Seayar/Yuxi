"""答案级评测脚本：精简工作簿与 --no-md 分支。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import evaluate_power_spec_answers as eval_module  # noqa: E402

MODERN_MD = """# Q99 · 测试验电器

> **E2E资格**：`READY_FOR_E2E`

**标准依据**：DL/T 408—2023
**标准章节/标准页/PDF页**：第 6.3.2 条

### 1. 用户问题

**验电前应确认什么？**

### 3. 参考答案（评分要点）

1. 验电前应先确认验电器良好。（依据：第6.3.2条）

### 4. 二元评分规则

- **1分**：回答验电器良好。
"""


def _write_compact_xlsx(path: Path) -> None:
    workbook = Workbook()
    single = workbook.active
    single.title = "单跳用例"
    single.append(["标题"])
    single.append(["序号", "问题", "引用文档", "引用章节", "引用原文", "标准答案", "评分判定标准"])
    single.append(
        [
            "SINGLE_01",
            "10kV 高压设备不停电时的最小安全距离是多少？",
            "DL/T 408-2023",
            "DL/T 408-2023 5.3.1.3 表1",
            "表1：10kV及以下 0.70m。",
            "0.70m。",
            "【Pass】答出 0.70m。",
        ]
    )

    supplement = workbook.create_sheet("补充用例")
    supplement.append(["标题"])
    supplement.append(["序号", "问题", "引用文档", "引用章节", "引用原文", "标准答案", "评分判定标准"])
    supplement.append(
        [
            "Q21",
            "验电前应确认什么？",
            "DL/T 408-2023",
            "DL/T 408-2023 6.3.2",
            "验电前应确认验电器良好。",
            "验电器良好。",
            "【Pass】答出验电器良好。",
        ]
    )
    workbook.save(path)


def test_no_md_skips_separate_markdown_file(tmp_path):
    xlsx_path = tmp_path / "compact.xlsx"
    md_path = tmp_path / "testset.md"
    _write_compact_xlsx(xlsx_path)
    md_path.write_text(MODERN_MD, encoding="utf-8")

    args = SimpleNamespace(
        xlsx=str(xlsx_path),
        md=str(md_path),
        ids="",
        offset=0,
        limit=0,
        no_md=True,
    )
    assert len(eval_module.load_questions(args)) == 2

    args.no_md = False
    assert len(eval_module.load_questions(args)) == 3
