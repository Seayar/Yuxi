from __future__ import annotations

import asyncio
import time
from typing import Any

from yuxi.knowledge.graphs.extractors import GraphExtractor, GraphExtractorFactory, normalize_extraction_result
from yuxi.knowledge.graphs.graph_stores import GraphStore, create_graph_store, run_graph_query_io
from yuxi.knowledge.graphs.milvus_graph_vector_store import MilvusGraphVectorStore
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.storage.graph_connection import GraphConnectionManager, get_shared_graph_connection
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_isoformat

GRAPH_CONFIG_KEY = "graph_build_config"
GRAPH_TASK_TYPE = "knowledge_graph_index"
# 数据库游标每次预取的 chunk 数量边界，只控制查询频率和单页内存，不限制 LLM 并发。
# 实际 LLM 并发只由 extractor_options.concurrency_count 决定；预取量会按其两倍动态计算后落在此范围内。
GRAPH_BUILD_FETCH_MIN_SIZE = 100
GRAPH_BUILD_FETCH_MAX_SIZE = 1000
# 构建任务 INFO 汇总日志与前端进度更新间隔；单个 chunk 的耗时明细使用 DEBUG 日志。
GRAPH_BUILD_LOG_INTERVAL_SECONDS = 5.0
GRAPH_VECTOR_BATCH_SIZE = 100
GRAPH_VECTOR_LEASE_SECONDS = 300
GRAPH_VECTOR_FLUSH_INTERVAL_SECONDS = 0.2
GRAPH_EXTRACTION_MAX_ATTEMPTS = 3
GRAPH_EXTRACTION_RETRY_DELAYS_SECONDS = (2.0, 10.0)


class MilvusGraphService:
    def __init__(
        self,
        *,
        kb_id: str | None = None,
        kb_repo: KnowledgeBaseRepository | None = None,
        chunk_repo: KnowledgeChunkRepository | None = None,
        graph_repo: KnowledgeGraphRepository | None = None,
        graph_vector_store: MilvusGraphVectorStore | None = None,
        graph_connection: GraphConnectionManager | None = None,
        graph_store: GraphStore | None = None,
    ):
        self.kb_id = kb_id
        self.kb_repo = kb_repo or KnowledgeBaseRepository()
        self.chunk_repo = chunk_repo or KnowledgeChunkRepository()
        self.graph_repo = graph_repo or KnowledgeGraphRepository()
        self._graph_vector_store = graph_vector_store
        self._graph_vector_store_lock = asyncio.Lock()
        self._connection = graph_connection
        self._graph_store = graph_store

    @property
    def connection(self) -> GraphConnectionManager:
        if self._connection is None:
            self._connection = get_shared_graph_connection()
        return self._connection

    @property
    def store(self) -> GraphStore:
        if self._graph_store is None:
            self._graph_store = create_graph_store(self.connection)
        return self._graph_store

    @property
    def graph_vector_store(self) -> MilvusGraphVectorStore:
        if self._graph_vector_store is None:
            self._graph_vector_store = MilvusGraphVectorStore()
        return self._graph_vector_store

    async def get_graph_vector_store(self) -> MilvusGraphVectorStore:
        if self._graph_vector_store is not None:
            return self._graph_vector_store
        async with self._graph_vector_store_lock:
            if self._graph_vector_store is None:
                self._graph_vector_store = await asyncio.to_thread(MilvusGraphVectorStore)
        return self._graph_vector_store

    @property
    def driver(self):
        return self.connection.driver

    async def get_status(self, kb_id: str, *, tasker: Any = None) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        params = dict(kb.additional_params or {})
        config = params.get(GRAPH_CONFIG_KEY) or {}
        status_values = await asyncio.gather(
            self.chunk_repo.count_by_kb_id(kb_id),
            self.chunk_repo.count_graph_pending_by_kb_id(kb_id),
            self.chunk_repo.count_graph_indexed_by_kb_id(kb_id),
            self.chunk_repo.count_graph_structure_indexed_by_kb_id(kb_id),
            self.chunk_repo.count_graph_extraction_statuses_by_kb_id(kb_id),
            self.graph_repo.count_by_kb_id(kb_id),
            self.graph_repo.count_vector_statuses_by_kb_id(kb_id),
        )
        (
            total_chunks,
            pending_chunks,
            indexed_chunks,
            structured_chunks,
            extraction_counts,
            graph_counts,
            vector_counts,
        ) = status_values
        entity_count, relationship_count = graph_counts

        build_task_status = None
        build_task_progress = 0
        if tasker is not None:
            latest_task = await tasker.find_task_by_payload(
                task_type=GRAPH_TASK_TYPE,
                payload_match={"kb_id": kb_id},
                statuses=None,
            )
            if latest_task and latest_task.status in {"pending", "running"}:
                build_task_status = latest_task.status
                build_task_progress = round(latest_task.progress)
            elif latest_task and latest_task.status == "success":
                build_task_status = "completed"
                build_task_progress = 100
            elif latest_task and latest_task.status in {"failed", "cancelled"}:
                build_task_status = "failed"

        return {
            "kb_id": kb_id,
            "kb_type": kb.kb_type,
            "configured": bool(config),
            "locked": bool(config.get("locked")),
            "config": self._public_config(config),
            "total_chunks": total_chunks,
            "pending_chunks": pending_chunks,
            "indexed_chunks": indexed_chunks,
            "structured_chunks": structured_chunks,
            "extraction_counts": extraction_counts,
            "vector_counts": vector_counts,
            "entity_count": entity_count,
            "relationship_count": relationship_count,
            "build_task_status": build_task_status,
            "build_task_progress": build_task_progress,
        }

    async def configure(
        self,
        kb_id: str,
        extractor_type: str,
        extractor_options: dict[str, Any],
        created_by: str,
    ) -> dict:
        kb = await self._get_milvus_kb(kb_id)
        additional_params = dict(kb.additional_params or {})
        existing_config = additional_params.get(GRAPH_CONFIG_KEY) or {}
        normalized_extractor_type = (extractor_type or "").lower()
        if existing_config.get("locked"):
            existing_extractor_type = (existing_config.get("extractor_type") or "").lower()
            if normalized_extractor_type != existing_extractor_type:
                raise ValueError("图谱抽取器类型已锁定，只能修改模型、Schema 等抽取参数")

        extractor_options = extractor_options or {}
        if normalized_extractor_type == "llm" and extractor_options.get("prompt"):
            raise ValueError("LLM 图谱抽取器不支持自定义完整 Prompt，请使用 schema 配置抽取约束")
        GraphExtractorFactory.create(normalized_extractor_type, extractor_options)
        config = {
            "locked": True,
            "extractor_type": normalized_extractor_type,
            "extractor_options": extractor_options or {},
            "created_at": existing_config.get("created_at") or utc_isoformat(),
            "created_by": existing_config.get("created_by") or created_by,
        }
        if existing_config.get("locked"):
            config["updated_at"] = utc_isoformat()
            config["updated_by"] = created_by
        additional_params[GRAPH_CONFIG_KEY] = config
        await self.kb_repo.update(kb_id, {"additional_params": additional_params})
        return config

    async def get_failed_chunk_samples(self, kb_id: str, limit: int = 10) -> dict[str, Any]:
        await self._get_milvus_kb(kb_id)
        samples = await self.chunk_repo.list_graph_extraction_failed_samples(kb_id, limit)
        return {"kb_id": kb_id, "samples": samples}

    async def build_pending_chunks(self, kb_id: str, *, context=None) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        config = self._get_locked_config(kb.additional_params or {})
        extractor_options = self._runtime_extractor_options(config)
        extractor = GraphExtractorFactory.create(config["extractor_type"], extractor_options)
        worker_count = self._get_worker_count(config)
        total_pending = await self.chunk_repo.count_graph_pending_by_kb_id(kb_id)
        initially_indexed = await self.chunk_repo.count_graph_indexed_by_kb_id(kb_id)
        processed = 0
        extraction_failed = 0
        write_failed = 0
        extraction_completed = 0
        write_completed = 0
        active_extractions = 0
        fetch_size = max(GRAPH_BUILD_FETCH_MIN_SIZE, min(worker_count * 2, GRAPH_BUILD_FETCH_MAX_SIZE))
        extraction_queue: asyncio.Queue[Any | None] = asyncio.Queue(maxsize=max(worker_count * 2, 1))
        write_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=max(worker_count * 2, 1))
        reporter_stop = asyncio.Event()
        structure_done = asyncio.Event()
        vector_wakeup = asyncio.Event()
        started_at = time.monotonic()

        logger.info(
            f"图谱构建开始 kb_id={kb_id} pending={total_pending} "
            f"extraction_concurrency={worker_count} fetch_size={fetch_size}"
        )

        async def put_queue_item(queue: asyncio.Queue, item) -> None:
            while True:
                if context is not None:
                    await context.raise_if_cancelled()
                try:
                    await asyncio.wait_for(queue.put(item), timeout=0.5)
                    return
                except TimeoutError:
                    continue

        async def extraction_worker(worker_index: int) -> None:
            nonlocal active_extractions, extraction_completed, extraction_failed
            while True:
                chunk = await extraction_queue.get()
                try:
                    if chunk is None:
                        return
                    if context is not None:
                        await context.raise_if_cancelled()
                    active_extractions += 1
                    extraction_started_at = time.monotonic()
                    try:
                        await self._get_chunk_extraction_result(kb_id, chunk, extractor)
                        await put_queue_item(write_queue, chunk.chunk_id)
                    except Exception as exc:
                        extraction_failed += 1
                        logger.error(
                            f"Chunk 图谱抽取失败 kb_id={kb_id} chunk_id={chunk.chunk_id} worker={worker_index}: {exc}"
                        )
                    finally:
                        active_extractions -= 1
                        extraction_completed += 1
                        logger.debug(
                            f"Chunk 图谱抽取结束 kb_id={kb_id} chunk_id={chunk.chunk_id} "
                            f"worker={worker_index} duration={time.monotonic() - extraction_started_at:.2f}s"
                        )
                finally:
                    extraction_queue.task_done()

        async def write_worker() -> None:
            nonlocal processed, write_failed, write_completed
            while True:
                chunk_id = await write_queue.get()
                try:
                    if chunk_id is None:
                        return
                    if context is not None:
                        await context.raise_if_cancelled()
                    chunk = await self.chunk_repo.get_by_chunk_id(chunk_id)
                    if chunk is None:
                        raise ValueError(f"图谱写入找不到 chunk: {chunk_id}")
                    extraction_result = await self._get_chunk_extraction_result(kb_id, chunk, extractor)
                    write_started_at = time.monotonic()
                    entities, triples = await asyncio.to_thread(
                        self.write_chunk_graph,
                        kb_id,
                        chunk,
                        extraction_result,
                    )
                    await self.graph_repo.upsert_chunk_graph(
                        kb_id=kb_id,
                        file_id=chunk.file_id,
                        chunk_id=chunk.chunk_id,
                        entities=entities,
                        triples=triples,
                    )
                    await self.chunk_repo.mark_graph_structure_indexed(
                        chunk.chunk_id,
                        ent_ids=[entity["entity_id"] for entity in entities],
                    )
                    vector_wakeup.set()
                    processed += 1
                    logger.debug(
                        f"Chunk 图谱写入结束 kb_id={kb_id} chunk_id={chunk.chunk_id} "
                        f"entities={len(entities)} triples={len(triples)} "
                        f"duration={time.monotonic() - write_started_at:.2f}s"
                    )
                except Exception as exc:
                    write_failed += 1
                    logger.error(f"Chunk 图谱写入失败 kb_id={kb_id} chunk_id={chunk_id}: {exc}")
                finally:
                    if chunk_id is not None:
                        write_completed += 1
                    write_queue.task_done()

        async def index_vector_batch(record_type: str) -> int:
            lock_token, records = await self.graph_repo.claim_vector_records(
                kb_id=kb_id,
                record_type=record_type,
                limit=GRAPH_VECTOR_BATCH_SIZE,
                lease_seconds=GRAPH_VECTOR_LEASE_SECONDS,
            )
            if not records:
                return 0
            record_ids = [record["id"] for record in records]
            try:
                graph_vector_store = await self.get_graph_vector_store()
                await graph_vector_store.upsert_graph_records(
                    kb_id=kb_id,
                    embedding_model_spec=kb.embedding_model_spec,
                    record_type=record_type,
                    records=records,
                )
                await self.graph_repo.mark_vector_records_indexed(
                    record_type=record_type,
                    record_ids=record_ids,
                    lock_token=lock_token,
                )
            except asyncio.CancelledError as exc:
                await self.graph_repo.mark_vector_records_failed(
                    record_type=record_type,
                    record_ids=record_ids,
                    lock_token=lock_token,
                    error=str(exc) or "cancelled",
                )
                raise
            except Exception as exc:
                await self.graph_repo.mark_vector_records_failed(
                    record_type=record_type,
                    record_ids=record_ids,
                    lock_token=lock_token,
                    error=str(exc),
                )
                logger.error(f"图谱向量索引失败 kb_id={kb_id} type={record_type} count={len(records)}: {exc}")
            return len(records)

        async def vector_worker() -> None:
            while True:
                if context is not None:
                    await context.raise_if_cancelled()
                entity_count, triple_count = await asyncio.gather(
                    index_vector_batch("entity"),
                    index_vector_batch("triple"),
                )
                if entity_count or triple_count:
                    await self.graph_repo.finalize_graph_indexed_chunks(kb_id)
                    continue

                vector_counts = await self.graph_repo.count_vector_statuses_by_kb_id(kb_id)
                if structure_done.is_set() and not vector_counts["pending"] and not vector_counts["processing"]:
                    await self.graph_repo.finalize_graph_indexed_chunks(kb_id)
                    return
                vector_wakeup.clear()
                try:
                    await asyncio.wait_for(vector_wakeup.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                if vector_wakeup.is_set():
                    await asyncio.sleep(GRAPH_VECTOR_FLUSH_INTERVAL_SECONDS)

        async def report_progress() -> None:
            while True:
                try:
                    await asyncio.wait_for(reporter_stop.wait(), timeout=GRAPH_BUILD_LOG_INTERVAL_SECONDS)
                except TimeoutError:
                    pass

                elapsed = max(time.monotonic() - started_at, 0.001)
                extraction_rate = extraction_completed / elapsed
                vector_counts = await self.graph_repo.count_vector_statuses_by_kb_id(kb_id)
                message = (
                    f"图谱构建：抽取 {extraction_completed}/{total_pending} "
                    f"(活跃 {active_extractions}/{worker_count})，写入 {write_completed}/{total_pending}，"
                    f"向量待处理 {vector_counts['pending'] + vector_counts['processing']}，"
                    f"向量失败 {vector_counts['failed']}，抽取失败 {extraction_failed}，"
                    f"写入失败 {write_failed}，抽取吞吐 {extraction_rate:.2f} chunk/s"
                )
                logger.info(f"kb_id={kb_id} {message}")
                if context is not None:
                    extraction_progress = extraction_completed / max(total_pending, 1) * 40.0
                    structure_progress = write_completed / max(total_pending, 1) * 20.0
                    indexed_chunks = await self.chunk_repo.count_graph_indexed_by_kb_id(kb_id)
                    newly_indexed = max(indexed_chunks - initially_indexed, 0)
                    vector_progress = newly_indexed / max(total_pending, 1) * 35.0
                    await context.set_progress(
                        5.0 + min(extraction_progress + structure_progress + vector_progress, 95.0), message
                    )
                if reporter_stop.is_set():
                    return

        extraction_workers = [
            asyncio.create_task(extraction_worker(index + 1), name=f"graph-extractor-{index + 1}")
            for index in range(worker_count)
        ]
        writer_task = asyncio.create_task(write_worker(), name="graph-writer")
        vector_task = asyncio.create_task(vector_worker(), name="graph-vector-indexer")
        reporter_task = asyncio.create_task(report_progress(), name="graph-progress-reporter")

        try:
            after_id = 0
            while True:
                if context is not None:
                    await context.raise_if_cancelled()
                chunks = await self.chunk_repo.list_graph_pending_by_kb_id(
                    kb_id,
                    fetch_size,
                    after_id=after_id,
                )
                if not chunks:
                    break
                for chunk in chunks:
                    after_id = chunk.id
                    if getattr(chunk, "graph_structure_indexed", False):
                        extraction_completed += 1
                        write_completed += 1
                        vector_wakeup.set()
                    elif chunk.extraction_result:
                        extraction_completed += 1
                        await put_queue_item(write_queue, chunk.chunk_id)
                    else:
                        await put_queue_item(extraction_queue, chunk)

            for _ in extraction_workers:
                await put_queue_item(extraction_queue, None)
            await asyncio.gather(*extraction_workers)
            await put_queue_item(write_queue, None)
            await writer_task
            structure_done.set()
            vector_wakeup.set()
            await vector_task
        except BaseException:
            for task in [*extraction_workers, writer_task, vector_task]:
                task.cancel()
            await asyncio.gather(*extraction_workers, writer_task, vector_task, return_exceptions=True)
            raise
        finally:
            reporter_stop.set()
            await asyncio.gather(reporter_task, return_exceptions=True)

        remaining = await self.chunk_repo.count_graph_pending_by_kb_id(kb_id)
        extraction_counts = await self.chunk_repo.count_graph_extraction_statuses_by_kb_id(kb_id)
        vector_counts = await self.graph_repo.count_vector_statuses_by_kb_id(kb_id)
        logger.info(
            f"图谱构建结束 kb_id={kb_id} success={processed} extraction_failed={extraction_failed} "
            f"write_failed={write_failed} remaining={remaining} "
            f"duration={time.monotonic() - started_at:.2f}s"
        )
        incomplete = max(remaining - extraction_counts["failed"], 0)
        result = {
            "kb_id": kb_id,
            "success": max(total_pending - remaining, 0),
            "failed": extraction_failed + write_failed,
            "extraction_failed": extraction_failed,
            "write_failed": write_failed,
            "remaining": remaining,
            "vector_failed": vector_counts["failed"],
        }
        if incomplete or write_failed or vector_counts["failed"]:
            raise RuntimeError(
                f"图谱构建执行异常：chunk_incomplete={incomplete}, "
                f"write_failed={write_failed}, vector_failed={vector_counts['failed']}"
            )
        return result

    @staticmethod
    def _get_worker_count(config: dict[str, Any]) -> int:
        if (config.get("extractor_type") or "").lower() != "llm":
            return 1
        try:
            worker_count = int((config.get("extractor_options") or {}).get("concurrency_count") or 1)
        except (TypeError, ValueError):
            return 1
        return max(1, min(worker_count, 1000))

    @staticmethod
    def _runtime_extractor_options(config: dict[str, Any]) -> dict[str, Any]:
        options = dict(config.get("extractor_options") or {})
        options.pop("prompt", None)
        return options

    async def _get_chunk_extraction_result(self, kb_id: str, chunk, extractor: GraphExtractor) -> dict[str, Any]:
        extractor_type = extractor.extractor_type
        if chunk.extraction_result:
            return normalize_extraction_result(chunk.extraction_result, extractor_type)

        details = getattr(chunk, "graph_extraction_details", None) or {}
        if details.get("status") == "failed":
            await self.chunk_repo.mark_graph_extraction_pending(chunk.chunk_id)

        metadata = {
            "kb_id": kb_id,
            "chunk_id": chunk.chunk_id,
            "file_id": chunk.file_id,
            "chunk_index": chunk.chunk_index,
        }
        for attempt in range(1, GRAPH_EXTRACTION_MAX_ATTEMPTS + 1):
            try:
                extraction_result = await extractor.extract(chunk.content, chunk_metadata=metadata)
                normalized_result = normalize_extraction_result(extraction_result, extractor_type)
                await self.chunk_repo.update_extraction_result(chunk.chunk_id, normalized_result, attempt)
                return normalized_result
            except Exception as exc:
                if attempt >= GRAPH_EXTRACTION_MAX_ATTEMPTS:
                    await self.chunk_repo.mark_graph_extraction_failed(chunk.chunk_id, attempt, str(exc))
                    raise
                delay = GRAPH_EXTRACTION_RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(
                    f"Chunk 图谱抽取重试 kb_id={kb_id} chunk_id={chunk.chunk_id} "
                    f"attempt={attempt}/{GRAPH_EXTRACTION_MAX_ATTEMPTS} delay={delay:.1f}s: {exc}"
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"Chunk 图谱抽取未返回结果: {chunk.chunk_id}")

    def write_chunk_graph(
        self,
        kb_id: str,
        chunk,
        normalized_result: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """将单个 chunk 的抽取结果写入图数据库（后端由 GraphStore 决定）。"""
        return self.store.write_chunk_graph(kb_id, chunk, normalized_result)

    async def reset(self, kb_id: str, *, clear_extraction_result: bool, clear_config: bool) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        await asyncio.to_thread(self.delete_graph, kb_id)
        await self.graph_repo.delete_by_kb_id(kb_id)
        reset_chunks = await self.chunk_repo.reset_graph_state_by_kb_id(kb_id, clear_extraction_result)
        if clear_config:
            additional_params = dict(kb.additional_params or {})
            additional_params.pop(GRAPH_CONFIG_KEY, None)
            await self.kb_repo.update(kb_id, {"additional_params": additional_params})
        return {
            "message": "图谱构建状态已重置",
            "status": "success",
            "reset_chunks": reset_chunks,
            "clear_extraction_result": clear_extraction_result,
            "clear_config": clear_config,
        }

    async def reconcile_vectors(self, kb_id: str, *, all_vectors: bool) -> dict[str, Any]:
        await self._get_milvus_kb(kb_id)
        reset_records = await self.graph_repo.reconcile_vector_records(kb_id, all_vectors=all_vectors)
        if all_vectors:
            graph_vector_store = await self.get_graph_vector_store()
            await asyncio.to_thread(graph_vector_store.drop_graph_collections, kb_id)
        return {
            "kb_id": kb_id,
            "mode": "all_vectors" if all_vectors else "failed",
            "reset_records": reset_records,
        }

    def delete_graph(self, kb_id: str) -> None:
        self.store.delete_graph(kb_id)
        self.graph_vector_store.drop_graph_collections(kb_id)

    async def delete_file_graph(self, kb_id: str, file_id: str) -> None:
        orphan_entity_ids, orphan_triple_ids = await self.graph_repo.delete_file_references(file_id)
        await self.graph_vector_store.delete_graph_records(
            kb_id,
            entity_ids=orphan_entity_ids,
            triple_ids=orphan_triple_ids,
        )
        await asyncio.to_thread(self.store.delete_file_graph, kb_id, file_id, orphan_entity_ids, orphan_triple_ids)

    async def query_nodes(
        self,
        kb_id: str | None = None,
        *,
        keyword: str = "",
        max_depth: int = 1,
        max_nodes: int = 50,
        exclude_chunk: bool = False,
    ) -> dict[str, Any]:
        """查询子图；后端差异由 GraphStore 承担。"""
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return {"nodes": [], "edges": []}
        return await run_graph_query_io(
            self.store.query_nodes,
            effective_kb_id,
            keyword=keyword,
            max_depth=max_depth,
            max_nodes=max_nodes,
            exclude_chunk=exclude_chunk,
        )

    async def query_seed_subgraph(
        self,
        kb_id: str,
        *,
        entity_ids: list[str],
        max_nodes: int,
    ) -> dict[str, Any]:
        return await run_graph_query_io(
            self.store.query_seed_subgraph,
            kb_id,
            entity_ids=entity_ids,
            max_nodes=max_nodes,
        )

    async def query_and_rank_chunks_by_ppr(
        self,
        kb_id: str,
        seed_weights: dict[str, float],
        *,
        max_nodes: int,
        top_k: int,
        damping: float,
    ) -> list[tuple[str, float]]:
        if not seed_weights:
            return []
        subgraph = await self.query_seed_subgraph(
            kb_id,
            entity_ids=list(seed_weights.keys()),
            max_nodes=max_nodes,
        )
        return self.rank_chunks_by_ppr(subgraph, seed_weights, top_k=top_k, damping=damping)

    @staticmethod
    def rank_chunks_by_ppr(
        subgraph: dict[str, Any],
        seed_weights: dict[str, float],
        *,
        top_k: int,
        damping: float,
    ) -> list[tuple[str, float]]:
        nodes = subgraph.get("nodes") or []
        edges = subgraph.get("edges") or []
        if not nodes:
            return []

        import networkx as nx

        node_ids = [node["id"] for node in nodes]
        index_by_id = {node_id: index for index, node_id in enumerate(node_ids)}
        edge_indices = [
            (index_by_id[edge["source_id"]], index_by_id[edge["target_id"]])
            for edge in edges
            if edge.get("source_id") in index_by_id and edge.get("target_id") in index_by_id
        ]
        if not edge_indices:
            return []

        # networkx 节点 id 直接用节点索引（与 igraph 的 reset 向量语义对齐）。
        graph = nx.Graph()
        graph.add_nodes_from(range(len(nodes)))
        graph.add_edges_from(edge_indices)
        reset = [0.0] * len(nodes)
        chunk_node_indexes: list[tuple[int, str]] = []
        for index, node in enumerate(nodes):
            properties = node.get("properties") or {}
            if node.get("type") == "Chunk" and properties.get("chunk_id"):
                chunk_node_indexes.append((index, properties["chunk_id"]))
                continue
            entity_id = properties.get("entity_id")
            if entity_id in seed_weights:
                reset[index] = seed_weights[entity_id]

        reset_total = sum(reset)
        if reset_total <= 0 or not chunk_node_indexes:
            return []
        reset = [value / reset_total for value in reset]
        # networkx 的 personalization 必须覆盖全部节点（缺节点会抛错），0 值节点贡献为 0；
        # 默认 dangling=None 时跳转分布使用 personalization，与 igraph 的 reset 语义一致。
        personalization = {index: value for index, value in enumerate(reset)}
        scores = nx.pagerank(graph, alpha=min(max(damping, 0.1), 0.99), personalization=personalization)
        ranked = sorted(
            ((chunk_id, float(scores[index])) for index, chunk_id in chunk_node_indexes),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[:top_k]

    async def get_labels(self, kb_id: str | None = None) -> list[str]:
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return []
        return await run_graph_query_io(self.store.get_labels, effective_kb_id)

    async def get_stats(self, kb_id: str | None = None) -> dict[str, Any]:
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}
        return await run_graph_query_io(self.store.get_stats, effective_kb_id)

    async def _get_milvus_kb(self, kb_id: str):
        kb = await self.kb_repo.get_by_kb_id(kb_id)
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        if (kb.kb_type or "").lower() != "milvus":
            raise ValueError("仅 Milvus 知识库支持独立图谱构建")
        return kb

    def _get_locked_config(self, additional_params: dict[str, Any]) -> dict[str, Any]:
        config = additional_params.get(GRAPH_CONFIG_KEY) or {}
        if not config.get("locked"):
            raise ValueError("请先确认并锁定图谱抽取配置")
        if not config.get("extractor_type"):
            raise ValueError("图谱抽取配置缺少 extractor_type")
        return config

    def _public_config(self, config: dict[str, Any]) -> dict[str, Any] | None:
        if not config:
            return None
        return {
            "locked": bool(config.get("locked")),
            "extractor_type": config.get("extractor_type"),
            "extractor_options": self._runtime_extractor_options(config),
            "created_at": config.get("created_at"),
            "created_by": config.get("created_by"),
            "updated_at": config.get("updated_at"),
            "updated_by": config.get("updated_by"),
        }
