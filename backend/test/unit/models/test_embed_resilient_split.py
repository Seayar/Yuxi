"""embedding 超限时的自适应拆批与单条分片均值。"""

import httpx
import pytest

from yuxi.models.embed import EMBEDDING_MAX_SPLIT_DEPTH, OtherEmbedding


def _model() -> OtherEmbedding:
    return OtherEmbedding(
        model="test-model",
        base_url="https://example.com/v1",
        api_key="test-key",
        dimension=4,
    )


def _payload_too_large() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com/v1/embeddings")
    response = httpx.Response(413, request=request)
    return httpx.HTTPStatusError("payload too large", request=request, response=response)


def _items(message) -> list[str]:
    return [message] if isinstance(message, str) else list(message)


@pytest.mark.asyncio
async def test_splits_batch_until_each_request_fits(monkeypatch):
    """批量超限时二分拆批，输出与输入保持一对一且顺序不变。"""

    model = _model()
    seen = []

    async def fake_aencode(message):
        batch = _items(message)
        seen.append(batch)
        if len(batch) > 1:
            raise _payload_too_large()
        return [[float(len(batch[0]))] * 4]

    monkeypatch.setattr(model, "aencode", fake_aencode)

    vectors = await model._aencode_resilient(["a", "bb", "ccc", "dddd"])

    assert [vector[0] for vector in vectors] == [1.0, 2.0, 3.0, 4.0]
    assert seen[0] == ["a", "bb", "ccc", "dddd"]


@pytest.mark.asyncio
async def test_averages_and_normalizes_slices_for_single_oversized_text(monkeypatch):
    """单条超长文本以两段分片向量均值返回，且结果为单位向量。"""

    model = _model()

    async def fake_aencode(message):
        text = _items(message)[0]
        if len(text) > 4:
            raise _payload_too_large()
        return [[1.0, 0.0, 0.0, 0.0] if text.startswith("a") else [0.0, 1.0, 0.0, 0.0]]

    monkeypatch.setattr(model, "aencode", fake_aencode)

    vectors = await model._aencode_resilient(["aaaabbbb"])

    assert len(vectors) == 1
    # 两段各为轴向量，均值归一化后两维各为 1/sqrt(2)
    assert vectors[0] == pytest.approx([2**-0.5, 2**-0.5, 0.0, 0.0], abs=1e-6)


@pytest.mark.asyncio
async def test_stops_splitting_at_depth_limit(monkeypatch):
    """达到拆批深度上限后不再递归，直接抛出原始错误。"""

    model = _model()

    async def always_too_large(message):
        raise _payload_too_large()

    monkeypatch.setattr(model, "aencode", always_too_large)

    with pytest.raises(httpx.HTTPStatusError):
        await model._aencode_resilient(["aaaa"], depth=EMBEDDING_MAX_SPLIT_DEPTH)
