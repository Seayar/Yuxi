"""设备图片集（MinIO）读取单元测试。"""

from __future__ import annotations

import pytest
from minio.error import S3Error
from yuxi.knowledge.fault_trace import images as images_module
from yuxi.knowledge.fault_trace.images import DeviceImageError, resolve_device_image
from yuxi.storage.minio.client import StorageError

pytestmark = pytest.mark.unit


class _Stat:
    def __init__(self, size: int) -> None:
        self.size = size


class _FakeMinio:
    def __init__(self, objects: dict[str, int] | None = None, *, error: S3Error | None = None) -> None:
        self.objects = objects or {}
        self.error = error
        self.stat_calls: list[str] = []

    def stat_object(self, _bucket: str, object_key: str):
        self.stat_calls.append(object_key)
        if self.error is not None:
            raise self.error
        if object_key not in self.objects:
            raise S3Error(None, "NoSuchKey", "not found", object_key, "req", "host")
        return _Stat(self.objects[object_key])


class _FakeStorage:
    def __init__(self, minio: _FakeMinio, *, download_error: Exception | None = None) -> None:
        self.client = minio
        self.download_error = download_error

    async def adownload_file(self, _bucket: str, object_key: str) -> bytes:
        if self.download_error is not None:
            raise self.download_error
        return f"data:{object_key}".encode()


def _patch(monkeypatch, minio: _FakeMinio, *, download_error: Exception | None = None) -> None:
    monkeypatch.setattr(images_module, "get_minio_client", lambda: _FakeStorage(minio, download_error=download_error))


@pytest.mark.asyncio
async def test_resolve_device_image_uses_device_name_and_first_extension(monkeypatch):
    minio = _FakeMinio({"device-images/#1主变压器.jpg": 10})
    _patch(monkeypatch, minio)

    image = await resolve_device_image("#1主变压器")

    assert image is not None
    assert image.object_key == "device-images/#1主变压器.jpg"
    assert image.content_type == "image/jpeg"
    assert image.data == "data:device-images/#1主变压器.jpg".encode()


@pytest.mark.asyncio
async def test_resolve_device_image_tries_next_extension(monkeypatch):
    minio = _FakeMinio({"device-images/#1主变压器.png": 10})
    _patch(monkeypatch, minio)

    image = await resolve_device_image("#1主变压器")

    assert image is not None
    assert image.object_key == "device-images/#1主变压器.png"
    assert image.content_type == "image/png"
    assert minio.stat_calls == [
        "device-images/#1主变压器.jpg",
        "device-images/#1主变压器.jpeg",
        "device-images/#1主变压器.png",
    ]


@pytest.mark.asyncio
async def test_resolve_device_image_returns_none_when_absent(monkeypatch):
    minio = _FakeMinio()
    _patch(monkeypatch, minio)

    assert await resolve_device_image("#1主变压器") is None
    assert len(minio.stat_calls) == 4


@pytest.mark.asyncio
async def test_resolve_device_image_rejects_oversized(monkeypatch):
    minio = _FakeMinio({"device-images/#1主变压器.jpg": images_module.MAX_IMAGE_BYTES + 1})
    _patch(monkeypatch, minio)

    with pytest.raises(DeviceImageError) as excinfo:
        await resolve_device_image("#1主变压器")

    assert excinfo.value.code == "image_too_large"


@pytest.mark.asyncio
async def test_resolve_device_image_maps_storage_error(monkeypatch):
    minio = _FakeMinio(error=S3Error(None, "AccessDenied", "denied", "bucket", "req", "host"))
    _patch(monkeypatch, minio)

    with pytest.raises(DeviceImageError) as excinfo:
        await resolve_device_image("#1主变压器")

    assert excinfo.value.code == "storage_unavailable"


@pytest.mark.asyncio
async def test_download_failure_is_reported_as_storage_unavailable(monkeypatch):
    """stat 成功但下载失败时必须降级为 DeviceImageError，不能把原始异常透出工具。"""

    minio = _FakeMinio({"device-images/#1主变压器.jpg": 10})
    _patch(monkeypatch, minio, download_error=StorageError("下载文件失败: boom"))

    with pytest.raises(DeviceImageError) as excinfo:
        await resolve_device_image("#1主变压器")

    assert excinfo.value.code == "storage_unavailable"


@pytest.mark.asyncio
async def test_s3_error_on_download_also_degrades(monkeypatch):
    minio = _FakeMinio({"device-images/#1主变压器.jpg": 10})
    _patch(monkeypatch, minio, download_error=S3Error(None, "NoSuchKey", "gone", "bucket", "req", "host"))

    with pytest.raises(DeviceImageError) as excinfo:
        await resolve_device_image("#1主变压器")

    assert excinfo.value.code == "storage_unavailable"
