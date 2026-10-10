"""图谱存储后端的统一接口与共享 IO 辅助。

``MilvusGraphService`` 只负责抽取、并发与进度编排，具体的 Cypher 方言和
驱动交互由 ``GraphStore`` 实现承担。Neo4j 与 TuGraph 的差异（标签模型、
强 Schema、Cypher 函数支持）因此被限制在后端实现内。
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any, Protocol, runtime_checkable

GRAPH_QUERY_OFFLOAD_LIMIT = 8

_graph_query_offload_semaphore_refs: dict[
    int,
    tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], weakref.ReferenceType[asyncio.Semaphore]],
] = {}


def _get_graph_query_offload_semaphore() -> asyncio.Semaphore:
    """按事件循环懒加载有界信号量，限制阻塞式图查询的并发线程数。"""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    entry = _graph_query_offload_semaphore_refs.get(loop_id)
    if entry is not None:
        loop_ref, semaphore_ref = entry
        semaphore = semaphore_ref()
        if loop_ref() is loop and semaphore is not None:
            return semaphore

    semaphore = asyncio.Semaphore(GRAPH_QUERY_OFFLOAD_LIMIT)

    def cleanup(ref, stale_loop_id=loop_id):
        current_entry = _graph_query_offload_semaphore_refs.get(stale_loop_id)
        if current_entry is not None and current_entry[1] is ref:
            _graph_query_offload_semaphore_refs.pop(stale_loop_id, None)

    _graph_query_offload_semaphore_refs[loop_id] = (weakref.ref(loop), weakref.ref(semaphore, cleanup))
    return semaphore


async def run_graph_query_io(func, /, *args, **kwargs):
    """把同步图查询放到线程执行，并限制同事件循环的并发上限。"""
    semaphore = _get_graph_query_offload_semaphore()
    await semaphore.acquire()
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))

    def release_capacity(completed_task: asyncio.Task):
        semaphore.release()
        if completed_task.cancelled():
            return
        completed_task.exception()

    task.add_done_callback(release_capacity)
    return await asyncio.shield(task)


@runtime_checkable
class GraphStore(Protocol):
    """图数据库后端必须实现的写入、查询与维护接口（均为同步方法）。"""

    backend: str

    def ensure_ready(self, kb_id: str) -> None:
        """确保该知识库可以写入（TuGraph 需要先准备 Schema）。"""

    def write_chunk_graph(
        self, kb_id: str, chunk, normalized_result: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把单个 chunk 的抽取结果写入图数据库，返回实体与三元组记录。"""

    def delete_graph(self, kb_id: str) -> None:
        """删除该知识库的全部图数据。"""

    def delete_file_graph(
        self,
        kb_id: str,
        file_id: str,
        orphan_entity_ids: list[str] | None = None,
        orphan_triple_ids: list[str] | None = None,
    ) -> None:
        """删除该文件引用的边与孤儿实体/三元组。

        ``orphan_entity_ids`` 与 ``orphan_triple_ids`` 由 service 层基于 PostgreSQL
        计算（真实事实来源）；TuGraph 因限制需按这两个列表精确删除（三元组列表决定
        RELATION 边去留），Neo4j 可忽略并沿用图内重算。
        """

    def query_nodes(
        self,
        kb_id: str,
        *,
        keyword: str = "",
        max_depth: int = 1,
        max_nodes: int = 50,
        exclude_chunk: bool = False,
    ) -> dict[str, Any]:
        """按关键词/标签查询子图。"""

    def query_seed_subgraph(self, kb_id: str, *, entity_ids: list[str], max_nodes: int) -> dict[str, Any]:
        """以实体为种子取 1~2 跳子图。"""

    def get_labels(self, kb_id: str) -> list[str]:
        """返回图谱中的节点标签。"""

    def get_stats(self, kb_id: str) -> dict[str, Any]:
        """返回节点/边数量与实体类型分布。"""
