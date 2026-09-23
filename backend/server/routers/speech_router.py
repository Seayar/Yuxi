"""语音交互路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse

from server.utils.auth_middleware import get_required_user
from yuxi.services.speech_service import MAX_AUDIO_BYTES, stream_transcription
from yuxi.storage.postgres.models_business import User

speech_router = APIRouter(prefix="/speech", tags=["speech"])


@speech_router.post("/transcriptions")
async def create_transcription(
    file: UploadFile = File(...),
    current_user: User = Depends(get_required_user),
):
    """接收音频文件，按 SSE 流式返回语音识别结果。"""

    # 多读一个字节即可判断是否超过上限，读取量不超过上限 + 1。
    audio = await file.read(MAX_AUDIO_BYTES + 1)
    return StreamingResponse(
        stream_transcription(audio=audio, filename=file.filename, content_type=file.content_type),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
