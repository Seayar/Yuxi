"""故障溯源相关 HTTP 接口：受理前端上传的故障图片并调用 YOLO 服务识别。

接口契约与 YOLO 服务接口文档保持一致：识别到故障时返回
``fault``/``fault_type``/``fault_name``，未识别到故障时类别为 ``null``。
上游失败（不可达、非 200、配置缺失）以 502 结构化返回，不把失败伪装成
“未识别到故障”。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from yuxi.services.yolo_service import (
    MAX_FAULT_IMAGE_BYTES,
    YoloDetectResult,
    YoloServiceError,
    detect_fault_image,
    yolo_service_health,
)
from yuxi.storage.postgres.models_business import User

from server.utils.auth_middleware import get_required_user

fault_router = APIRouter(prefix="/fault", tags=["fault"])


@fault_router.post("/images")
async def recognize_fault_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_required_user),
):
    """受理前端上传的故障图片，返回 YOLO 识别结果（fault/fault_type/fault_name）。

    只接受 ``image/*`` 的上传；图片上限与 YOLO 无关系的本地校验在调用上游前完成。
    """

    content_type = (file.content_type or "").strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="只能上传图片")
    image = await file.read(MAX_FAULT_IMAGE_BYTES + 1)
    if not image:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(image) > MAX_FAULT_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail=f"图片超过 {MAX_FAULT_IMAGE_BYTES // (1024 * 1024)} MB 上限")
    try:
        result = await detect_fault_image(image=image, filename=file.filename, content_type=content_type)
    except YoloServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _detect_response(result)


@fault_router.get("/health")
async def fault_health(current_user: User = Depends(get_required_user)):
    """透传 YOLO 服务健康状态，供前端诊断。"""

    try:
        return await yolo_service_health()
    except YoloServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _detect_response(result: YoloDetectResult) -> dict:
    """转发 YOLO 识别结果，保持与上游契约一致的字段。"""

    return {
        "code": 200,
        "message": "success",
        "fault": result.fault,
        "fault_type": result.fault_type,
        "fault_name": result.fault_name,
    }
