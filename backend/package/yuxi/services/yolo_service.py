"""YOLO 设备图片故障类型识别服务客户端。

对接口档：``srs/YOLO设备图片故障类型识别服务 · 接口文档V1.0.0.pdf``。
本模块只做协议转译与失败投影：识别到故障时返回 ``fault_type``/``fault_name``，
未识别到故障时两者为 ``None``；服务不可达、非 200、响应不合法时一律抛出
``YoloServiceError``，绝不把失败伪装成“未识别到故障”。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from yuxi.config.options import fault_image_opts
from yuxi.utils import get_docker_safe_url, logger

DETECT_PATH = "/api/fault/detect"
HEALTH_PATH = "/api/fault/health"
# 直连 YOLO 服务时的图片上限；反向代理另有 client_max_body_size，两者按各自边界独立生效。
MAX_FAULT_IMAGE_BYTES = 10 * 1024 * 1024
# 识别耗时体现在 read；连接与写入保持短超时，避免上游僵死连接长期占用请求。
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
_ERROR_DETAIL_LIMIT = 300


class YoloServiceError(Exception):
    """故障识别失败，携带可直接展示给用户的说明。"""


@dataclass(frozen=True)
class YoloDetectResult:
    """一次图片识别得到的结果。"""

    fault: bool
    fault_type: str | None
    fault_name: str | None


async def detect_fault_image(
    *,
    image: bytes,
    filename: str | None,
    content_type: str | None,
) -> YoloDetectResult:
    """调用 YOLO 故障识别服务，返回结构化结果。

    空图片与超限图片在调用上游前拒绝；上游非 200、响应不是 JSON 对象、
    ``fault`` 字段缺失或“识别到故障但缺类别”时按失败处理。
    """

    if not image:
        raise YoloServiceError("图片内容为空")
    if len(image) > MAX_FAULT_IMAGE_BYTES:
        raise YoloServiceError(f"图片超过 {MAX_FAULT_IMAGE_BYTES // (1024 * 1024)} MB 上限")

    base_url = await _resolve_base_url()
    if base_url is None:
        logger.warning("故障识别服务未配置：缺少服务地址（YOLO_FAULT_BASE_URL / fault_image_opts.server_url）")
        raise YoloServiceError("故障识别服务未配置，请联系管理员")

    files = {"file": (filename or "image.jpg", image, content_type or "image/jpeg")}
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            response = await client.post(f"{base_url}{DETECT_PATH}", files=files)
    except httpx.HTTPError as exc:
        logger.warning(f"故障识别服务不可达: {exc}")
        raise YoloServiceError("无法连接故障识别服务，请检查服务地址与服务运行状态") from exc

    if response.status_code != 200:
        detail = response.text[:_ERROR_DETAIL_LIMIT]
        logger.warning(f"故障识别服务返回 {response.status_code}: {detail}")
        raise YoloServiceError(f"故障识别服务返回 {response.status_code}，请检查服务运行状态")

    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(f"故障识别服务响应不是 JSON: {response.text[:_ERROR_DETAIL_LIMIT]}")
        raise YoloServiceError("故障识别服务返回格式异常") from exc
    if not isinstance(payload, dict):
        raise YoloServiceError("故障识别服务返回格式异常")
    return _parse_detect_payload(payload)


def _parse_detect_payload(payload: dict[str, Any]) -> YoloDetectResult:
    """按接口文档解析识别结果；字段语义不一致时按失败处理。"""

    fault = payload.get("fault")
    fault_type = payload.get("fault_type")
    fault_name = payload.get("fault_name")
    if not isinstance(fault, bool):
        raise YoloServiceError("故障识别服务返回缺少 fault 字段")
    if not fault:
        # 未识别到故障时类别必须为空，避免把无故障图误判成带类别。
        if fault_type is not None or fault_name is not None:
            raise YoloServiceError("故障识别服务返回字段不一致")
        return YoloDetectResult(fault=False, fault_type=None, fault_name=None)
    if not isinstance(fault_type, str) or not fault_type or not isinstance(fault_name, str) or not fault_name:
        raise YoloServiceError("故障识别服务返回缺少故障类别")
    return YoloDetectResult(fault=True, fault_type=fault_type, fault_name=fault_name)


async def yolo_service_health() -> dict[str, Any]:
    """读取 YOLO 服务健康状态，供 `/api/fault/health` 透传。"""

    base_url = await _resolve_base_url()
    if base_url is None:
        raise YoloServiceError("故障识别服务未配置，请联系管理员")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)) as client:
            response = await client.get(f"{base_url}{HEALTH_PATH}")
    except httpx.HTTPError as exc:
        raise YoloServiceError("无法连接故障识别服务，请检查服务地址与服务运行状态") from exc
    if response.status_code != 200:
        raise YoloServiceError(f"故障识别服务返回 {response.status_code}，请检查服务运行状态")
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise YoloServiceError("故障识别服务返回格式异常") from exc
    if not isinstance(payload, dict):
        raise YoloServiceError("故障识别服务返回格式异常")
    return payload


async def _resolve_base_url() -> str | None:
    """读取管理员配置；服务地址缺失时返回 None，由调用方投影为失败。"""

    resolved = await fault_image_opts.get()
    base_url = str(resolved.get("server_url") or "").strip()
    if not base_url:
        return None
    return get_docker_safe_url(base_url.rstrip("/"))
