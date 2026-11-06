"""Synthetic Lambda environment transport controls for private accuracy policies."""
from copy import deepcopy
import base64
import json
import sys
import types
import zlib

import pytest

from aws_mesouq import benchmark_accuracy_protocol as protocol
from aws_publishing import accuracy_policy_env as codec, handler
from aws_publishing.accuracy import policies_from_environment
from test_accuracy import accuracy_lab, create_accuracy
from test_benchmark import lab, specification as legacy_specification


def packed(raw):
    return codec.PREFIX + base64.b64encode(zlib.compress(raw)).decode("ascii")


def configure(monkeypatch, suite, value):
    monkeypatch.setattr(handler, "_application", None)
    monkeypatch.setattr(handler, "DynamoStore", lambda *_args: suite[1])
    monkeypatch.setattr(handler, "S3Objects", lambda *_args: suite[2])
    monkeypatch.setattr(handler, "SQSNotifications", lambda *_args: None)
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *_a, **_k: object()))
    monkeypatch.setenv("STATE_TABLE", "synthetic-table")
    monkeypatch.setenv("DATA_BUCKET", "synthetic-bucket")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://queue.invalid/synthetic")
    legacy = {"p": {"adapter_sha256": "1" * 64, "science_revision": "2" * 40,
                    "archive_root": "/project/legacy", "scenario_families": ["baseline"]}}
    monkeypatch.setenv("BENCHMARK_POLICIES", json.dumps(legacy))
    monkeypatch.setenv("BENCHMARK_ACCURACY_POLICIES", value)
    return legacy


@pytest.mark.parametrize("compressed", [False, True])
def test_lambda_entrypoint_preserves_old_and_new_contexts_and_v1(
    accuracy_lab, monkeypatch, compressed
):
    old = deepcopy(accuracy_lab[5])
    legacy = configure(monkeypatch, accuracy_lab, json.dumps({"p": [old]}))
    previous = handler.application()
    campaign = create_accuracy((previous, *accuracy_lab[1:]), "old-context")

    old["enabled"] = False
    new = deepcopy(old)
    new.update(enabled=True, science_revision="9" * 40)
    new["id"] = protocol.context_id(new)
    policies = {"p": [old, new]}
    value = codec.encode_policies(policies) if compressed else json.dumps(policies)
    configure(monkeypatch, accuracy_lab, value)
    current = handler.application()
    assert current is handler.application()
    assert current.benchmark_policies == legacy
    frozen = next(item for item in accuracy_lab[1].state["accuracy_campaigns"].values()
                  if item["id"] == campaign["id"])
    assert current.accuracy._context(frozen) == protocol.validate_context(old)
    assert current.accuracy._contexts("p", "alice") == [protocol.validate_context(new)]
    fresh = create_accuracy((current, *accuracy_lab[1:]), "new-context")
    created = next(item for item in accuracy_lab[1].state["accuracy_campaigns"].values()
                   if item["id"] == fresh["id"])
    assert created["execution_context_id"] == new["id"]
    status, detail = current.handle("GET", "/v1/projects/p/benchmark-accuracy/campaigns/" + campaign["id"],
                                    {"sub": "alice"}, None)
    assert status == 200 and detail["campaign"]["id"] == campaign["id"]
    status, fixed = current.handle("POST", "/v1/projects/p/benchmarks", {"sub": "alice"},
                                   legacy_specification("legacy-still-works"))
    assert status == 202
    assert fixed["campaign"]["id"]


def test_compressed_policies_still_use_normal_context_validator(accuracy_lab, monkeypatch):
    configure(monkeypatch, accuracy_lab, codec.encode_policies({"p": [{"id": "payload-canary"}]}))
    with pytest.raises(ValueError) as caught:
        handler.application()
    assert "payload-canary" not in str(caught.value)


def test_plaintext_default_and_existing_json_semantics(monkeypatch):
    monkeypatch.delenv("BENCHMARK_ACCURACY_POLICIES", raising=False)
    assert policies_from_environment() == {}
    monkeypatch.setenv("BENCHMARK_ACCURACY_POLICIES", ' { "p": [], "p": [1] } ')
    assert policies_from_environment() == {"p": [1]}


def test_encoder_is_deterministic_and_roundtrips_unicode():
    one = {"p": [{"label": "synthetic-µ", "enabled": False}], "other": []}
    two = {"other": [], "p": [{"enabled": False, "label": "synthetic-µ"}]}
    assert codec.encode_policies(one) == codec.encode_policies(two)
    assert codec.decode_policies(codec.encode_policies(one)) == one


def test_sixteen_contexts_fit_transport_bound(accuracy_lab):
    contexts = []
    for index in range(16):
        context = deepcopy(accuracy_lab[5])
        context["native_root"] += "/" + str(index)
        context["id"] = protocol.context_id(context)
        contexts.append(context)
    policies = {"p": contexts}
    assert codec.decode_policies(codec.encode_policies(policies)) == policies


def noncanonical_padding():
    encoded = base64.b64encode(zlib.compress(b"{}")).decode("ascii")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    position = len(encoded.rstrip("=")) - 1
    replacement = alphabet[alphabet.index(encoded[position]) | 1]
    altered = encoded[:position] + replacement + encoded[position + 1:]
    assert altered != encoded
    assert base64.b64decode(altered, validate=True) == base64.b64decode(encoded)
    return codec.PREFIX + altered


@pytest.mark.parametrize("value", [
    "mesouq-zlib-v2:payload-canary",
    codec.PREFIX,
    codec.PREFIX + "payload-canary!",
    codec.PREFIX + "eA==\n",
    codec.PREFIX + "µ",
    noncanonical_padding(),
    codec.PREFIX + base64.b64encode(b"not-zlib-payload-canary").decode("ascii"),
    codec.PREFIX + base64.b64encode(zlib.compress(b"{}")[:-1]).decode("ascii"),
    codec.PREFIX + base64.b64encode(zlib.compress(b"{}") + b"payload-canary").decode("ascii"),
    codec.PREFIX + base64.b64encode(zlib.compress(b"{}") + zlib.compress(b"{}")).decode("ascii"),
    packed(b"\xff"),
    packed(b'{"secret": "payload-canary"'),
    packed(b'{"p":[],"p":[]}'),
    packed(b'{"p": NaN}'),
    packed(b'{"p": Infinity}'),
    packed(b'{"p": 1e999}'),
    packed(b'{"p": -1e999}'),
    packed(b"[]"),
    packed(b"null"),
    "[]",
    "{payload-canary",
    packed(b'{"p":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}"),
])
def test_invalid_environment_values_have_fixed_payload_free_errors(value, monkeypatch):
    monkeypatch.setenv("BENCHMARK_ACCURACY_POLICIES", value)
    with pytest.raises(ValueError) as caught:
        policies_from_environment()
    assert str(caught.value) == codec.ERROR
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_decompression_bomb_is_rejected_during_bounded_inflate(monkeypatch):
    value = packed(b'{"p":"' + b"A" * (codec.MAX_DECODED_BYTES * 8) + b'"}')
    assert len(value) < codec.MAX_ENCODED_BYTES
    monkeypatch.setenv("BENCHMARK_ACCURACY_POLICIES", value)
    with pytest.raises(ValueError, match="^" + codec.ERROR + "$"):
        policies_from_environment()


def test_decoded_exact_byte_limit_and_overflow():
    exact = b'{"p":"' + b"A" * (codec.MAX_DECODED_BYTES - 8) + b'"}'
    assert len(exact) == codec.MAX_DECODED_BYTES
    assert codec.decode_policies(packed(exact)) == {"p": "A" * (codec.MAX_DECODED_BYTES - 8)}
    with pytest.raises(ValueError, match="^" + codec.ERROR + "$"):
        codec.decode_policies(packed(exact + b" "))


def test_encoded_limit_and_encoder_limits():
    with pytest.raises(ValueError, match="^" + codec.ERROR + "$"):
        codec.decode_policies(codec.PREFIX + "A" * codec.MAX_ENCODED_BYTES)
    with pytest.raises(ValueError, match="^" + codec.ERROR + "$"):
        codec.encode_policies({"p": "A" * codec.MAX_DECODED_BYTES})
    with pytest.raises(ValueError, match="^" + codec.ERROR + "$"):
        codec.encode_policies({"p": float("nan")})
    with pytest.raises(ValueError, match="^" + codec.ERROR + "$"):
        codec.encode_policies([])
    with pytest.raises(ValueError, match="^" + codec.ERROR + "$"):
        codec.decode_policies(None)
