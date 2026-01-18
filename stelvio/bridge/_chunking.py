"""
Chunking utilities for splitting large messages across AppSync Events.

AppSync Events has a 240KB per-message limit, but Lambda supports 6MB (sync).
This module provides utilities to split large messages into chunks, transmit
them over AppSync, and reassemble them on the other end.
"""

import base64
import json
import time
import uuid
from dataclasses import dataclass, field

# 200KB chunk size (leaves ~40KB for AppSync overhead/metadata)
MAX_CHUNK_SIZE = 200_000

# Timeout for incomplete chunk transfers (30 seconds)
CHUNK_TIMEOUT = 30


@dataclass
class ChunkBuffer:
    """Buffer for collecting chunks of a chunked message."""

    chunk_id: str
    request_id: str
    total_chunks: int
    chunks: dict[int, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def is_complete(self) -> bool:
        """Check if all chunks have been received."""
        return len(self.chunks) == self.total_chunks

    def get_payload(self) -> str:
        """Reassemble and return the complete payload."""
        if not self.is_complete:
            raise ValueError("Cannot get payload from incomplete buffer")
        # Concatenate chunks in order
        ordered_chunks = [self.chunks[i] for i in range(self.total_chunks)]
        combined_b64 = "".join(ordered_chunks)
        return base64.b64decode(combined_b64).decode("utf-8")


def is_chunked_message(msg: dict) -> bool:
    """Check if a message is a chunked message."""
    return msg.get("chunked") is True


def split_message(msg: dict, request_id: str) -> list[dict]:
    """
    Split a message into chunks if it exceeds MAX_CHUNK_SIZE.

    Returns a list of chunk messages, or a list with the original message
    if chunking is not needed.
    """
    serialized = json.dumps(msg)
    serialized_bytes = serialized.encode("utf-8")

    if len(serialized_bytes) <= MAX_CHUNK_SIZE:
        return [msg]

    # Encode to base64 for safe transport
    payload_b64 = base64.b64encode(serialized_bytes).decode("ascii")

    # Calculate chunk size for base64 payload
    # We need to account for the chunk wrapper overhead
    chunk_wrapper_overhead = 200  # JSON wrapper for chunk metadata
    effective_chunk_size = MAX_CHUNK_SIZE - chunk_wrapper_overhead

    # Split the base64 payload into chunks
    chunk_id = str(uuid.uuid4())
    chunks = []
    offset = 0

    while offset < len(payload_b64):
        chunk_data = payload_b64[offset : offset + effective_chunk_size]
        chunks.append(chunk_data)
        offset += effective_chunk_size

    # Create chunk messages
    chunk_messages = []
    for i, chunk_data in enumerate(chunks):
        chunk_msg = {
            "chunked": True,
            "chunkId": chunk_id,
            "requestId": request_id,
            "chunkIndex": i,
            "totalChunks": len(chunks),
            "payload": chunk_data,
        }
        chunk_messages.append(chunk_msg)

    return chunk_messages


def reassemble_chunk(chunk: dict, buffers: dict[str, ChunkBuffer]) -> tuple[dict | None, bool]:
    """
    Process a chunk and attempt to reassemble the complete message.

    Args:
        chunk: The chunk message to process
        buffers: Dictionary of chunk_id -> ChunkBuffer for tracking incomplete transfers

    Returns:
        Tuple of (complete_message, is_complete):
        - If all chunks received: (reassembled_message_dict, True)
        - If still waiting for chunks: (None, False)
    """
    chunk_id = chunk["chunkId"]
    request_id = chunk["requestId"]
    chunk_index = chunk["chunkIndex"]
    total_chunks = chunk["totalChunks"]
    payload = chunk["payload"]

    # Get or create buffer for this chunk_id
    if chunk_id not in buffers:
        buffers[chunk_id] = ChunkBuffer(
            chunk_id=chunk_id,
            request_id=request_id,
            total_chunks=total_chunks,
        )

    buffer = buffers[chunk_id]

    # Store the chunk (handles duplicates by overwriting)
    buffer.chunks[chunk_index] = payload

    # Check if complete
    if buffer.is_complete:
        # Reassemble the message
        complete_payload = buffer.get_payload()
        complete_message = json.loads(complete_payload)

        # Clean up the buffer
        del buffers[chunk_id]

        return complete_message, True

    return None, False


def cleanup_stale_buffers(buffers: dict[str, ChunkBuffer]) -> list[str]:
    """
    Remove stale buffers that have timed out.

    Args:
        buffers: Dictionary of chunk_id -> ChunkBuffer

    Returns:
        List of chunk_ids that were cleaned up
    """
    now = time.time()
    stale_ids = []

    for chunk_id, buffer in list(buffers.items()):
        if now - buffer.created_at > CHUNK_TIMEOUT:
            stale_ids.append(chunk_id)
            del buffers[chunk_id]

    return stale_ids
