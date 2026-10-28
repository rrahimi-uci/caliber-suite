"""Signing and binding rules of the receipt callback (B05.4b, #725): pure, no database and no network."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from caliber.config import CaliberConfig, ConfigError
from caliber.effects.callbacks import (
    TOLERANCE_SECONDS,
    CallbackBinding,
    check_signature,
    describe,
    parse_bindings,
    sign,
    signed_string,
)

SECRET = "s3cret-value-for-tests"  # gitleaks:allow
NOW = 1_800_000_000.0
BODY = b'{"idempotency_key": "k"}'


def _binding(**overrides: Any) -> dict[str, Any]:
    fields = {
        "connector": "document-request",
        "project_id": "PRJ-1",
        "secret_source": "env://DOC_SECRET",
        "base_url": "https://documents.example.com/api",
    }
    fields.update(overrides)
    return fields


def _check(**overrides: Any) -> bool:
    """Check a request whose parts are ``overrides`` against the signature made for the *baseline* parts (unless a
    ``signature`` is given), so each override is a claimed part that differs from what was signed."""
    signature = overrides.pop("signature", None)
    secret = overrides.pop("secret", SECRET)
    timestamp = overrides.pop("timestamp", str(int(NOW)))
    if signature is None:
        signature = sign(secret, "document-request", "PRJ-1", int(NOW), "delivery-1", BODY)
    fields: dict[str, Any] = {
        "connector": "document-request",
        "project_id": "PRJ-1",
        "delivery": "delivery-1",
        "body": BODY,
        "now": NOW,
    }
    fields.update(overrides)
    return check_signature(SECRET, timestamp=timestamp, signature=signature, **fields)


def test_the_signature_is_an_hmac_over_every_part_that_matters_and_only_the_digest_of_the_body() -> (
    None
):
    expected = hmac.new(
        SECRET.encode(),
        b"v1\ndocument-request\nPRJ-1\n1800000000\ndelivery-1\n"
        + hashlib.sha256(BODY).hexdigest().encode(),
        hashlib.sha256,
    ).hexdigest()
    assert (
        sign(SECRET, "document-request", "PRJ-1", 1_800_000_000, "delivery-1", BODY)
        == f"v1={expected}"
    )
    assert (
        signed_string("c", "p", "1", "d", b"")
        == b"v1\nc\np\n1\nd\n" + hashlib.sha256(b"").hexdigest().encode()
    )


def test_a_valid_signature_in_the_window_is_accepted_and_every_part_is_bound() -> None:
    assert _check() is True
    assert _check(connector="other-connector") is False, "the connector is signed"
    assert _check(project_id="PRJ-2") is False, "the project is signed"
    assert _check(delivery="delivery-2") is False, "the delivery id is signed"
    assert _check(body=b'{"idempotency_key": "other"}') is False, "the body is signed"
    good = sign(SECRET, "document-request", "PRJ-1", int(NOW), "delivery-1", BODY)
    assert _check(timestamp=str(int(NOW) + 1), signature=good) is False, "the timestamp is signed"
    assert _check(secret="another-secret") is False, "the secret is the key"


@pytest.mark.parametrize("offset", [0, 1, -1, TOLERANCE_SECONDS, -TOLERANCE_SECONDS])
def test_a_timestamp_inside_the_window_is_accepted(offset: int) -> None:
    stamp = int(NOW) + offset
    assert _check(
        timestamp=str(stamp),
        signature=sign(SECRET, "document-request", "PRJ-1", stamp, "delivery-1", BODY),
    )


@pytest.mark.parametrize("offset", [TOLERANCE_SECONDS + 1, -TOLERANCE_SECONDS - 1, 10**6, -(10**6)])
def test_a_stale_or_future_timestamp_is_refused_even_with_a_valid_signature(offset: int) -> None:
    stamp = int(NOW) + offset
    signature = sign(SECRET, "document-request", "PRJ-1", stamp, "delivery-1", BODY)
    assert _check(timestamp=str(stamp), signature=signature) is False


@pytest.mark.parametrize(
    "timestamp",
    ["", " 1800000000", "1800000000 ", "-5", "1.8e9", "+1800000000", "٣٣", "1" * 13, "abc"],
)
def test_a_malformed_timestamp_is_refused(timestamp: str) -> None:
    assert _check(timestamp=timestamp, signature="v1=" + "0" * 64) is False


@pytest.mark.parametrize(
    "signature",
    [
        "",
        "v1=",
        "v1=abc",
        "v2=" + "0" * 64,
        "V1=" + "0" * 64,
        "v1=" + "G" * 64,
        "v1=" + "0" * 65,
        "0" * 64,
        "v1=" + "A" * 64,
    ],
)
def test_a_malformed_signature_is_refused(signature: str) -> None:
    assert _check(signature=signature) is False


@pytest.mark.parametrize(
    "delivery", ["", "d" * 129, "has space", "tab\t", "new\nline", "café", "\x00"]
)
def test_a_malformed_delivery_id_is_refused(delivery: str) -> None:
    good = sign(SECRET, "document-request", "PRJ-1", int(NOW), delivery, BODY)
    assert _check(delivery=delivery, signature=good) is False


def test_the_delivery_id_boundary_lengths() -> None:
    delivery = "d" * 128
    signature = sign(SECRET, "document-request", "PRJ-1", int(NOW), delivery, BODY)
    assert _check(delivery=delivery, signature=signature) is True


def test_the_comparison_is_constant_time_and_the_header_value_is_never_a_prefix_match() -> None:
    good = sign(SECRET, "document-request", "PRJ-1", int(NOW), "delivery-1", BODY)
    assert _check(signature=good[:-1]) is False
    assert _check(signature=good + "0") is False


def test_bindings_parse_with_their_four_keys_and_a_trailing_slash_is_dropped() -> None:
    bindings = parse_bindings(json.dumps([_binding(base_url="https://documents.example.com/api/")]))
    assert bindings == (
        CallbackBinding(
            "document-request", "PRJ-1", "env://DOC_SECRET", "https://documents.example.com/api"
        ),
    )
    assert parse_bindings("[]") == ()
    assert describe(bindings) == [{"connector": "document-request", "project_id": "PRJ-1"}], (
        "no secret source or URL"
    )
    two = parse_bindings(json.dumps([_binding(), _binding(project_id="PRJ-2")]))
    assert [b.project_id for b in two] == ["PRJ-1", "PRJ-2"]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        "null",
        '"x"',
        "[1]",
        "[[]]",
        json.dumps([{**_binding(), "extra": "x"}]),
        json.dumps([{k: v for k, v in _binding().items() if k != "base_url"}]),
        json.dumps([_binding(connector="")]),
        json.dumps([_binding(connector="has space")]),
        json.dumps([_binding(connector="-leading")]),
        json.dumps([_binding(connector="c" * 129)]),
        json.dumps([_binding(connector=5)]),
        json.dumps([_binding(project_id="a/b")]),
        json.dumps([_binding(secret_source="")]),
        json.dumps([_binding(secret_source="   ")]),
        json.dumps([_binding(secret_source=None)]),
        json.dumps([_binding(base_url="")]),
        json.dumps([_binding(base_url="ftp://x.example.com")]),
        json.dumps([_binding(base_url="https://")]),
        json.dumps([_binding(base_url="https://user:pass@x.example.com")]),
        json.dumps([_binding(base_url="https://x.example.com/?q=1")]),
        json.dumps([_binding(base_url="https://x.example.com/#frag")]),
        json.dumps([_binding(base_url="https://[::1")]),
        json.dumps([_binding(), _binding()]),
    ],
)
def test_a_malformed_binding_list_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_bindings(raw)


def test_a_bad_binding_list_stops_the_service_at_startup_and_the_default_is_empty() -> None:
    assert CaliberConfig.load(environ={}).effect_callback_connectors == "[]"
    ok = json.dumps([_binding()])
    assert (
        CaliberConfig.load(
            environ={"CALIBER_EFFECT_CALLBACK_CONNECTORS": ok}
        ).effect_callback_connectors
        == ok
    )
    with pytest.raises(ConfigError, match="invalid CALIBER configuration"):
        CaliberConfig.load(environ={"CALIBER_EFFECT_CALLBACK_CONNECTORS": "{}"})
