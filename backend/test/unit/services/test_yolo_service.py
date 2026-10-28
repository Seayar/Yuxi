"""YOLO 故障识别服务单元测试：解析契约与失败投影。"""

from __future__ import annotations

import json

import httpx
import pytest
from yuxi.services import yolo_service
from yuxi.services.yolo_service import (
    MAX_FAULT_IMAGE_BYTES,
    YoloServiceError,
)

pytestmark = pytest.mark.unit

DETECT_URL = "http://yolo.local/api/fault/detect"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\nfake-image-payload"


class _FakeOption:
    """替身配置项，让配置解析路径不依赖数据库。"""

    def __init__(self, value: dict) -> None:
        self._value = value

    async def get(self) -> dict:
        return self._value


def _use_config(monkeypatch: pytest.MonkeyPatch, *, server_url: str | None = "http://yolo.local") -> None:
    value = {"server_url": server_url} if server_url else {}
    monkeypatch.setattr(yolo_service, "fault_image_opts", _FakeOption(value))


def _detect_body(*, fault: bool, fault_type: str | None = None, fault_name: str | None = None) -> bytes:
    payload = {"code": 200, "message": "success", "fault": fault, "fault_type": fault_type, "fault_name": fault_name}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


async def _detect(**kwargs) -> yolo_service.YoloDetectResult:
    return await yolo_service.detect_fault_image(
        image=IMAGE_BYTES,
        filename="fault.png",
        content_type="image/png",
        **kwargs,
    )


async def test_detect_fault_returns_typed_result(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch)
    httpx_mock.add_response(
        url=DETECT_URL,
        content=_detect_body(fault=True, fault_type="insulator_damage", fault_name="绝缘子破损"),
    )

    result = await _detect()

    assert result.fault is True
    assert result.fault_type == "insulator_damage"
    assert result.fault_name == "绝缘子破损"
    request = httpx_mock.get_request()
    assert request.method == "POST"
    assert b"fault.png" in request.content


async def test_detect_no_fault_returns_null_categories(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch)
    httpx_mock.add_response(url=DETECT_URL, content=_detect_body(fault=False))

    result = await _detect()

    assert result.fault is False
    assert result.fault_type is None
    assert result.fault_name is None


@pytest.mark.parametrize(
    "body",
    [
        _detect_body(fault=True, fault_type="fire"),  # fault:true 缺 fault_name
        _detect_body(fault=True, fault_name="冒烟/着火"),  # fault:true 缺 fault_type
        _detect_body(fault=False, fault_type="fire", fault_name="冒烟/着火"),  # fault:false 却带类别
        json.dumps({"code": 200, "message": "success"}).encode(),  # 缺 fault
        json.dumps({"fault": "yes"}).encode(),  # fault 非 bool
    ],
)
async def test_detect_rejects_inconsistent_payload(monkeypatch: pytest.MonkeyPatch, httpx_mock, body: bytes):
    _use_config(monkeypatch)
    httpx_mock.add_response(url=DETECT_URL, content=body)

    with pytest.raises(YoloServiceError):
        await _detect()


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b"[1, 2, 3]",
    ],
)
async def test_detect_rejects_non_object_response(monkeypatch: pytest.MonkeyPatch, httpx_mock, body: bytes):
    _use_config(monkeypatch)
    httpx_mock.add_response(
        url=DETECT_URL, content=body, headers={"content-type": "application/json"}
    )

    with pytest.raises(YoloServiceError):
        await _detect()


async def test_detect_maps_upstream_error_status(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch)
    httpx_mock.add_response(url=DETECT_URL, status_code=500, content=b'{"detail":"boom"}')

    with pytest.raises(YoloServiceError):
        await _detect()


async def test_detect_maps_connection_error(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch)
    httpx_mock.add_exception(httpx.ConnectError("refused"))

    with pytest.raises(YoloServiceError):
        await _detect()


async def test_detect_rejects_empty_image_without_upstream_call(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch)

    with pytest.raises(YoloServiceError):
        await yolo_service.detect_fault_image(image=b"", filename=None, content_type=None)
    assert not httpx_mock.get_requests()


async def test_detect_rejects_oversized_image_without_upstream_call(monkeypatch: pytest.MonkeyPatch, httpx_mock):
    _use_config(monkeypatch)

    with pytest.raises(YoloServiceError):
        await yolo_service.detect_fault_image(
            image=b"x" * (MAX_FAULT_IMAGE_BYTES + 1),
            filename=None,
            content_type="image/png",
        )
    assert not httpx_mock.get_requests()


async def test_detect_fails_explicitly_when_service_not_configured(monkeypatch: pytest.MonkeyPatch):
    _use_config(monkeypatch, server_url=None)

    with pytest.raises(YoloServiceError, match="未配置"):
        await _detect()
