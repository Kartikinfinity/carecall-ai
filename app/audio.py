"""Telephone audio helpers.

Phone networks carry speech as G.711 mu-law: 8,000 samples a second, one byte
per sample. Gnani's streaming speech-to-text wants 16-bit linear PCM, so every
inbound chunk is expanded here. (Python 3.13 removed the old `audioop` module,
which is why this is written out.)
"""
from __future__ import annotations

import struct

PHONE_RATE = 8000
_BIAS = 0x84
_CLIP = 32635


def _decode_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        u = ~byte & 0xFF
        sample = (((u & 0x0F) << 3) + _BIAS) << ((u >> 4) & 0x07)
        sample -= _BIAS
        table.append(-sample if u & 0x80 else sample)
    return tuple(table)


_ULAW_TO_PCM = _decode_table()
_PCM_BYTES = tuple(struct.pack("<h", s) for s in _ULAW_TO_PCM)


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(sample), _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_to_pcm16(data: bytes) -> bytes:
    """mu-law bytes -> signed 16-bit little-endian PCM (twice as many bytes)."""
    return b"".join(_PCM_BYTES[b] for b in data)


def pcm16_to_ulaw(data: bytes) -> bytes:
    """Signed 16-bit little-endian PCM -> mu-law bytes."""
    count = len(data) // 2
    return bytes(_encode_sample(s) for s in struct.unpack(f"<{count}h", data[:count * 2]))


def ulaw_seconds(data: bytes) -> float:
    return len(data) / PHONE_RATE


ULAW_SILENCE = b"\xff"
