# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The serialization seam.

Tool payloads are serialized as TOON (Token-Oriented Object Notation) to cut MCP client token
usage, the same choice the owner's Bedrock server makes. TOON drops JSON's braces, quotes, and
repeated keys and renders uniform object arrays as a header plus CSV-style rows.

This module is the single seam: it converts pydantic models to plain JSON-compatible data and
hands them to the encoder. The whole project encodes through :func:`encode`, so swapping the
encoder later -- or falling back to JSON -- is a one-function change here and nowhere else.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DecodeError(Exception):
    """A TOON document could not be parsed. The seam-level error, so callers never import the encoder."""


def to_plain(data: Any) -> Any:
    """Recursively convert pydantic models to JSON-compatible Python data.

    Models are dumped in JSON mode (enums become their string values) with ``None`` fields
    dropped, so the encoded payload stays compact. Plain dicts, lists, and scalars pass through.
    """
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json", exclude_none=True)
    if isinstance(data, dict):
        return {key: to_plain(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [to_plain(item) for item in data]
    return data


def encode(data: Any) -> str:
    """Encode ``data`` as a TOON string. The single swap point for the serialization format.

    ``None`` encodes to ``null`` and an empty payload to ``"(no content)"`` so a tool never
    returns an empty text block.
    """
    if data is None:
        return "null"
    from toon import encode as _toon_encode

    text = _toon_encode(to_plain(data))
    return text if text else "(no content)"


def decode(text: str) -> Any:
    """Decode a TOON string into plain Python data: the read counterpart to :func:`encode`.

    Raises :class:`DecodeError` (not the encoder's own exception) on malformed input, so the rest
    of the project depends on this seam rather than on the TOON library directly.
    """
    from toon import ToonDecodeError
    from toon import decode as _toon_decode

    try:
        return _toon_decode(text)
    except ToonDecodeError as exc:
        raise DecodeError(str(exc)) from exc
