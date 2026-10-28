"""故障设备图片集（MinIO）读取。

对象按台账设备名称命名，落在模块默认桶的 ``device-images/`` 前缀下：
``fault-assets/device-images/{设备名称}.{jpg|jpeg|png|webp}``。运行时只读，单图
上限 10 MB；没有对象时返回 ``None``，存储不可用或对象超限时抛 ``DeviceImageError``。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from minio.error import S3Error
from yuxi.storage.minio import get_minio_client
from yuxi.storage.minio.client import StorageError
from yuxi.utils import logger

IMAGE_BUCKET = os.getenv("FAULT_TRACE_IMAGE_BUCKET", "fault-assets")
IMAGE_PREFIX = os.getenv("FAULT_TRACE_IMAGE_PREFIX", "device-images/")
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp")
IMAGE_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class DeviceImageError(Exception):
    """设备图片不可用；``code`` 对应用户可解释的失败类型。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeviceImage:
    """一次设备图片读取结果。"""

    bucket: str
    object_key: str
    content_type: str
    data: bytes


async def resolve_device_image(device_name: str) -> DeviceImage | None:
    """按设备名称查找图片对象；多个扩展名时按固定顺序取第一个。"""

    name = str(device_name or "").strip()
    if not name:
        return None
    client = get_minio_client()
    for object_key in _candidate_keys(name):
        try:
            stat = await asyncio.to_thread(client.client.stat_object, IMAGE_BUCKET, object_key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                continue
            raise DeviceImageError("storage_unavailable", "设备图片存储暂不可用") from exc
        if stat.size > MAX_IMAGE_BYTES:
            raise DeviceImageError("image_too_large", f"设备图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 上限")
        extension = object_key.rsplit(".", 1)[-1].lower()
        try:
            data = await client.adownload_file(IMAGE_BUCKET, object_key)
        except (S3Error, StorageError) as exc:
            # storage 客户端把 S3Error 包装成自己的 StorageError，两者都要按“存储不可用”降级。
            logger.warning(f"下载设备图片失败: {IMAGE_BUCKET}/{object_key}: {exc}")
            raise DeviceImageError("storage_unavailable", "设备图片存储暂不可用") from exc
        return DeviceImage(
            bucket=IMAGE_BUCKET,
            object_key=object_key,
            content_type=IMAGE_CONTENT_TYPES.get(extension, "application/octet-stream"),
            data=data,
        )
    return None


def _candidate_keys(device_name: str) -> list[str]:
    suffix = device_name.rsplit(".", 1)[-1].lower()
    if "." in device_name and suffix in IMAGE_CONTENT_TYPES:
        return [f"{IMAGE_PREFIX}{device_name}"]
    return [f"{IMAGE_PREFIX}{device_name}.{extension}" for extension in IMAGE_EXTENSIONS]
