from yuxi.knowledge.chunking.ragflow_like.dispatcher import MAX_MILVUS_CONTENT_CHARS, chunk_markdown


def test_chunk_dispatcher_splits_content_under_milvus_varchar_limit():
    chunks = chunk_markdown("x" * (MAX_MILVUS_CONTENT_CHARS + 10), "file-1", "doc.md", {})

    assert len(chunks) == 2
    assert max(len(chunk["content"]) for chunk in chunks) <= MAX_MILVUS_CONTENT_CHARS