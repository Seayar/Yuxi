from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from server.routers import knowledge_router
from yuxi.storage.minio.client import StorageError


@pytest.mark.asyncio
async def test_power_spec_pdf_download_reads_powerspec_bucket(monkeypatch):
    minio_response = SimpleNamespace(
        read=Mock(return_value=b""),
        close=Mock(),
        release_conn=Mock(),
    )
    client = SimpleNamespace(adownload_response=AsyncMock(return_value=minio_response))
    monkeypatch.setattr(knowledge_router, "get_minio_client", lambda: client)

    response = await knowledge_router.download_power_spec_pdf(
        "DL_T408-2023.pdf",
        current_user=SimpleNamespace(uid="user-1"),
    )

    client.adownload_response.assert_awaited_once_with(
        bucket_name="powerspec",
        object_name="DL_T408-2023.pdf",
    )
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="DL_T408-2023.pdf"'


@pytest.mark.asyncio
async def test_power_spec_pdf_download_rejects_path_traversal(monkeypatch):
    monkeypatch.setattr(knowledge_router, "get_minio_client", lambda: SimpleNamespace())

    with pytest.raises(HTTPException) as caught:
        await knowledge_router.download_power_spec_pdf(
            "../DL_T408-2023.pdf",
            current_user=SimpleNamespace(uid="user-1"),
        )

    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_power_spec_pdf_download_returns_404_when_object_is_missing(monkeypatch):
    client = SimpleNamespace(adownload_response=AsyncMock(side_effect=StorageError("missing")))
    monkeypatch.setattr(knowledge_router, "get_minio_client", lambda: client)

    with pytest.raises(HTTPException) as caught:
        await knowledge_router.download_power_spec_pdf(
            "DL_T408-2023.pdf",
            current_user=SimpleNamespace(uid="user-1"),
        )

    assert caught.value.status_code == 404
