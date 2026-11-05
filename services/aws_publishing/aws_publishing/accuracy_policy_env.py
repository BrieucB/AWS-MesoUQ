"""Bounded transport for the backend's private accuracy policy environment."""
from __future__ import annotations

import base64
import binascii
import json
import math
import zlib

PREFIX = "mesouq-zlib-v1:"
MAX_ENCODED_BYTES = 64 * 1024
MAX_DECODED_BYTES = 256 * 1024
ERROR = "Invalid accuracy policy configuration"


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(ERROR)
        value[key] = item
    return value


def _finite_json(_value):
    raise ValueError(ERROR)


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(ERROR)
    return result


def decode_policies(value):
    """Accept legacy JSON or one explicitly versioned, bounded zlib stream."""
    try:
        if not isinstance(value, str):
            raise ValueError(ERROR)
        if value.startswith(PREFIX):
            if len(value) > MAX_ENCODED_BYTES:
                raise ValueError(ERROR)
            encoded = value[len(PREFIX):].encode("ascii")
            compressed = base64.b64decode(encoded, validate=True)
            if base64.b64encode(compressed) != encoded:
                raise ValueError(ERROR)
            stream = zlib.decompressobj()
            raw = stream.decompress(compressed, MAX_DECODED_BYTES + 1)
            if (len(raw) > MAX_DECODED_BYTES or not stream.eof
                    or stream.unconsumed_tail or stream.unused_data):
                raise ValueError(ERROR)
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=_finite_json, parse_float=_finite_float)
        else:
            # Existing plaintext configuration retains json.loads semantics.
            result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError(ERROR)
        return result
    except (ValueError, TypeError, binascii.Error, zlib.error,
            RecursionError, OverflowError):
        raise ValueError(ERROR) from None


def encode_policies(value):
    """Encode an operator-reviewed policy mapping for the environment value."""
    try:
        if not isinstance(value, dict):
            raise ValueError(ERROR)
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
        if len(raw) > MAX_DECODED_BYTES:
            raise ValueError(ERROR)
        result = PREFIX + base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")
        if len(result) > MAX_ENCODED_BYTES:
            raise ValueError(ERROR)
        return result
    except (ValueError, TypeError, binascii.Error, zlib.error,
            RecursionError, OverflowError):
        raise ValueError(ERROR) from None
