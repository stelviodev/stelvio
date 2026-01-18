import json
import time

import pytest

from stelvio.bridge._chunking import (
    CHUNK_TIMEOUT,
    MAX_CHUNK_SIZE,
    ChunkBuffer,
    cleanup_stale_buffers,
    reassemble_chunk,
    split_message,
)


def roundtrip(message: dict, request_id: str = "req-123") -> dict:
    """Split a message and reassemble it, simulating the full chunking cycle."""
    chunks = split_message(message, request_id)

    if len(chunks) == 1 and "chunked" not in chunks[0]:
        # Small message, passed through unchanged
        return chunks[0]

    # Reassemble chunks
    buffers = {}
    for chunk in chunks:
        result, is_complete = reassemble_chunk(chunk, buffers)
        if is_complete:
            return result

    raise AssertionError("Failed to reassemble chunks")


# === Core behavior: messages survive the chunking round-trip ===


def test_small_message_passes_through_unchanged():
    original = {"requestId": "req-123", "data": "small payload"}

    result = roundtrip(original)

    assert result == original


def test_message_just_under_limit_passes_through():
    data = "x" * (MAX_CHUNK_SIZE - 100)  # Just under limit
    original = {"data": data}

    result = roundtrip(original)

    assert result == original


def test_message_exactly_at_limit_passes_through():
    # Create message that serializes to exactly MAX_CHUNK_SIZE
    json_overhead = len(json.dumps({"data": ""}))  # Use actual json.dumps overhead
    data = "x" * (MAX_CHUNK_SIZE - json_overhead)
    original = {"data": data}
    assert len(json.dumps(original)) == MAX_CHUNK_SIZE

    result = roundtrip(original)

    assert result == original


def test_message_one_byte_over_limit_gets_chunked():
    # Create message just over the limit - should be chunked
    json_overhead = len(json.dumps({"data": ""}))
    data = "x" * (MAX_CHUNK_SIZE - json_overhead + 1)
    original = {"data": data}
    assert len(json.dumps(original)) == MAX_CHUNK_SIZE + 1

    chunks = split_message(original, "req-boundary")

    assert len(chunks) > 1
    assert chunks[0].get("chunked") is True


def test_large_message_splits_and_reassembles():
    data = "x" * (MAX_CHUNK_SIZE * 2)
    original = {"requestId": "req-123", "data": data}

    result = roundtrip(original)

    assert result == original


@pytest.mark.parametrize("size_multiplier", [1.5, 2, 3, 5, 10])
def test_various_large_sizes(size_multiplier):
    data = "x" * int(MAX_CHUNK_SIZE * size_multiplier)
    original = {"requestId": "req-123", "data": data}

    result = roundtrip(original)

    assert result == original


def test_unicode_content_survives_roundtrip():
    original = {
        "requestId": "req-unicode",
        "message": "Hello 世界 🎉",
        "data": "🚀" * 50000 + "日本語" * 10000,
    }

    result = roundtrip(original)

    assert result == original


def test_nested_json_structure_survives_roundtrip():
    data = "x" * (MAX_CHUNK_SIZE * 2)
    original = {
        "requestId": "req-nested",
        "success": True,
        "result": {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": data,
            "nested": {"deep": {"deeper": [1, 2, 3]}},
        },
    }

    result = roundtrip(original)

    assert result == original


# === Edge case: out-of-order chunk delivery ===


def test_chunks_reassemble_when_received_out_of_order():
    data = "x" * (MAX_CHUNK_SIZE * 2)
    original = {"requestId": "req-ooo", "data": data}

    chunks = split_message(original, "req-ooo")
    assert len(chunks) > 1

    # Reverse the order
    reversed_chunks = list(reversed(chunks))

    buffers = {}
    result = None
    for chunk in reversed_chunks:
        result, is_complete = reassemble_chunk(chunk, buffers)
        if is_complete:
            break

    assert result == original


# === Edge case: stale buffer cleanup prevents memory leaks ===


def test_stale_incomplete_transfers_get_cleaned_up():
    old_buffer = ChunkBuffer(
        chunk_id="stale-transfer",
        request_id="req-abandoned",
        total_chunks=5,
        chunks={0: "partial"},
        created_at=time.time() - CHUNK_TIMEOUT - 1,
    )
    recent_buffer = ChunkBuffer(
        chunk_id="active-transfer",
        request_id="req-active",
        total_chunks=3,
        chunks={0: "partial"},
        created_at=time.time(),
    )
    buffers = {
        "stale-transfer": old_buffer,
        "active-transfer": recent_buffer,
    }

    cleanup_stale_buffers(buffers)

    assert "stale-transfer" not in buffers
    assert "active-transfer" in buffers
