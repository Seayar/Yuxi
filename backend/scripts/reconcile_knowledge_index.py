"""对账知识库的 PostgreSQL 分块与 Milvus 向量实体，报告并可选修复缺失的入库数据。

用法（在 api 容器内执行）：
    python scripts/reconcile_knowledge_index.py audit [--kb KB_ID] [--json PATH]
    python scripts/reconcile_knowledge_index.py repair [--kb KB_ID]

`audit` 在发现「PostgreSQL 记为 indexed、Milvus 却没有对应实体」这类分歧时以退出码 1
结束，可直接用于重建后的收尾核对。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text as sqltext

APP_ROOT = Path(__file__).resolve().parents[1]
for import_path in (APP_ROOT, APP_ROOT / "package"):
    import_path_str = str(import_path)
    if import_path_str not in sys.path:
        sys.path.insert(0, import_path_str)


MILVUS_URI = "http://milvus:19530"


async def _kb_ids(kb_id: str | None) -> list[str]:
    """返回待对账的知识库标识；未指定时列出全部向量库知识库。"""

    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as db:
        if kb_id:
            return [kb_id]
        rows = (
            (await db.execute(sqltext("SELECT kb_id FROM knowledge_bases WHERE kb_type = 'milvus' ORDER BY id")))
            .scalars()
            .all()
        )
    return list(rows)


async def _file_states(kb_id: str) -> dict[str, dict]:
    """返回知识库内每个非目录文件的分块数与派生元数据状态。"""

    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as db:
        files = (
            (
                await db.execute(
                    sqltext(
                        "SELECT file_id, filename, status, "
                        "(processing_params ? 'power_spec') AS has_power_spec "
                        "FROM knowledge_files WHERE kb_id = :kb AND is_folder IS NOT TRUE"
                    ),
                    {"kb": kb_id},
                )
            )
            .mappings()
            .all()
        )
        chunks = (
            (
                await db.execute(
                    sqltext(
                        "SELECT file_id, count(*) AS total FROM knowledge_chunks WHERE kb_id = :kb GROUP BY file_id"
                    ),
                    {"kb": kb_id},
                )
            )
            .mappings()
            .all()
        )
    chunk_counts = {row["file_id"]: row["total"] for row in chunks}
    return {
        row["file_id"]: {
            "filename": row["filename"],
            "status": row["status"],
            "pg_chunks": chunk_counts.get(row["file_id"], 0),
            "has_power_spec": bool(row["has_power_spec"]),
        }
        for row in files
    }


def _milvus_chunk_counts(kb_id: str) -> tuple[int, dict[str, int]]:
    """返回知识库在 Milvus 中的实体总数与按文件统计的实体数。"""

    from pymilvus import Collection, connections

    connections.connect(alias="reconcile", uri=MILVUS_URI)
    try:
        try:
            collection = Collection(name=kb_id, using="reconcile")
        except Exception as exc:  # noqa: BLE001 - 集合不存在时按「全部缺失」处理
            print(f"[warn] {kb_id}: 无法打开 Milvus 集合（{exc}）")
            return 0, {}
        collection.load()
        counts: dict[str, int] = {}
        total = 0
        iterator = collection.query_iterator(expr="chunk_id != ''", output_fields=["file_id"], batch_size=2000)
        while True:
            rows = iterator.next()
            if not rows:
                iterator.close()
                break
            total += len(rows)
            for row in rows:
                file_id = row.get("file_id")
                counts[file_id] = counts.get(file_id, 0) + 1
        return total, counts
    finally:
        connections.disconnect("reconcile")


async def _reconcile_kb(kb_id: str) -> dict:
    """对账单个知识库并返回分歧清单。"""

    file_states = await _file_states(kb_id)
    milvus_total, milvus_counts = _milvus_chunk_counts(kb_id)
    diverged = []
    for file_id, state in file_states.items():
        actual = milvus_counts.get(file_id, 0)
        if actual != state["pg_chunks"]:
            diverged.append({**state, "file_id": file_id, "milvus_chunks": actual})
    diverged.sort(key=lambda item: item["file_id"])
    return {
        "kb_id": kb_id,
        "pg_files": len(file_states),
        "pg_chunks": sum(state["pg_chunks"] for state in file_states.values()),
        "milvus_entities": milvus_total,
        "diverged": diverged,
    }


def _print_report(report: dict) -> None:
    diverged = report["diverged"]
    missing = [item for item in diverged if item["milvus_chunks"] == 0 and item["status"] == "indexed"]
    print(
        f"{report['kb_id']}: pg_files={report['pg_files']} pg_chunks={report['pg_chunks']} "
        f"milvus_entities={report['milvus_entities']} diverged={len(diverged)} "
        f"(记为 indexed 但向量库无数据={len(missing)})"
    )
    for item in diverged[:20]:
        print(
            f"  - {item['file_id']} status={item['status']} pg={item['pg_chunks']} "
            f"milvus={item['milvus_chunks']} power_spec={item['has_power_spec']} {item['filename']}"
        )
    if len(diverged) > 20:
        print(f"  ... 其余 {len(diverged) - 20} 个省略")


async def _repair(report: dict) -> int:
    """按对账结果重新入库已解析但向量库缺数据的文件。"""

    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.services.knowledge_task_service import attach_power_spec_rebuild

    plan = [item for item in report["diverged"] if item["status"] == "indexed" and item["pg_chunks"] > 0]
    print(f"[repair] {report['kb_id']}: 需要重新入库 {len(plan)} 个文件")
    repaired = failed = 0
    for item in plan:
        try:
            result = await knowledge_base.index_file(report["kb_id"], item["file_id"], operator_id="system")
            await attach_power_spec_rebuild(report["kb_id"], item["file_id"], result)
            repaired += 1
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不应中断整批
            failed += 1
            print(f"  [fail] {item['file_id']}: {type(exc).__name__}: {exc}")
    print(f"[repair] 完成 {repaired} 个，失败 {failed} 个")
    return failed


async def main() -> int:
    parser = argparse.ArgumentParser(description="知识库分块与向量实体对账")
    parser.add_argument("command", choices=("audit", "repair"))
    parser.add_argument("--kb", default=None, help="只处理指定知识库；缺省为全部 milvus 知识库")
    parser.add_argument("--json", default=None, help="audit 报告写入路径")
    args = parser.parse_args()

    from yuxi.storage.postgres.manager import pg_manager

    pg_manager.initialize()
    kb_ids = await _kb_ids(args.kb)

    reports = []
    for kb_id in kb_ids:
        report = await _reconcile_kb(kb_id)
        _print_report(report)
        reports.append(report)

    if args.json:
        Path(args.json).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {args.json}")

    if args.command == "repair":
        failed = 0
        for report in reports:
            failed += await _repair(report)
        return 1 if failed else 0

    return 1 if any(report["diverged"] for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
