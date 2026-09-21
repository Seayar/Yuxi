"""电力规范派生元数据的 PostgreSQL 适配器。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.power_spec.ingestion import build_clause_extraction, build_document_extraction
from yuxi.knowledge.power_spec.ingestion import PowerSpecDocument
from yuxi.storage.postgres.models_knowledge import KnowledgeFile


class PowerSpecRepository:
    """只拥有电力规范派生字段，不改变通用知识库记录结构。"""

    async def save_power_spec_extraction(
        self,
        db: AsyncSession,
        *,
        kb_id: str,
        file_id: str,
        document: PowerSpecDocument,
        metadata: dict[str, Any],
    ) -> None:
        """校验文件归属并保存规范派生元数据。"""

        result = await db.execute(
            select(KnowledgeFile).where(
                KnowledgeFile.kb_id == kb_id,
                KnowledgeFile.file_id == file_id,
            )
        )
        knowledge_file = result.scalar_one_or_none()
        if knowledge_file is None:
            raise ValueError(f"知识库文件不存在或不属于知识库: {kb_id}/{file_id}")

        processing_params = dict(knowledge_file.processing_params or {})
        power_spec_metadata = {**build_document_extraction(document), **metadata}
        power_spec_metadata["clauses"] = [build_clause_extraction(document, clause) for clause in document.clauses]
        processing_params["power_spec"] = power_spec_metadata
        knowledge_file.processing_params = processing_params
        knowledge_file.updated_by = "power_spec"
        await db.flush()

    async def get_power_spec_extraction(
        self,
        db: AsyncSession,
        *,
        kb_id: str,
        file_id: str,
    ) -> dict[str, Any] | None:
        """读取属于指定知识库文件的规范派生元数据。"""

        result = await db.execute(
            select(KnowledgeFile.processing_params).where(
                KnowledgeFile.kb_id == kb_id,
                KnowledgeFile.file_id == file_id,
            )
        )
        processing_params = result.scalar_one_or_none() or {}
        power_spec_metadata = processing_params.get("power_spec")
        return dict(power_spec_metadata) if isinstance(power_spec_metadata, dict) else None

    async def list_power_spec_extractions(
        self,
        db: AsyncSession,
        *,
        kb_id: str,
    ) -> dict[str, dict[str, Any]]:
        """读取知识库中文件级电力规范派生元数据。"""

        result = await db.execute(
            select(KnowledgeFile.file_id, KnowledgeFile.processing_params).where(
                KnowledgeFile.kb_id == kb_id,
                KnowledgeFile.is_folder.is_(False),
            )
        )
        metadata_by_file_id: dict[str, dict[str, Any]] = {}
        for file_id, processing_params in result.all():
            if not isinstance(processing_params, dict):
                continue
            power_spec_metadata = processing_params.get("power_spec")
            if isinstance(power_spec_metadata, dict):
                metadata_by_file_id[str(file_id)] = dict(power_spec_metadata)
        return metadata_by_file_id

    async def list_file_names(
        self,
        db: AsyncSession,
        *,
        kb_id: str,
        file_ids: set[str],
    ) -> dict[str, str | None]:
        """读取命中文件名，供惰性规范提取记录来源文件。"""

        if not file_ids:
            return {}
        result = await db.execute(
            select(KnowledgeFile.file_id, KnowledgeFile.filename).where(
                KnowledgeFile.kb_id == kb_id,
                KnowledgeFile.file_id.in_(file_ids),
                KnowledgeFile.is_folder.is_(False),
            )
        )
        return {str(file_id): filename for file_id, filename in result.all()}

    async def get_file_name(self, db: AsyncSession, *, kb_id: str, file_id: str) -> str | None:
        """读取单个知识库文件名。"""

        result = await db.execute(
            select(KnowledgeFile.filename).where(
                KnowledgeFile.kb_id == kb_id,
                KnowledgeFile.file_id == file_id,
                KnowledgeFile.is_folder.is_(False),
            )
        )
        return result.scalar_one_or_none()
