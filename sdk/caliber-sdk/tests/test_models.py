"""Shared model helpers."""

from __future__ import annotations

from caliber_sdk import ErrorBody, Page, Stability


def test_page_reports_the_next_offset_and_whether_it_is_last() -> None:
    full = Page(items=[1, 2], limit=2, offset=0)
    assert not full.is_last
    assert full.next_offset == 2
    assert Page(items=[1], limit=2, offset=2).is_last


def test_stability_maps_a_tag_to_its_tier() -> None:
    tiers = Stability.from_payload({"ga": ["prompts"], "beta": ["aria"], "internal": ["metrics"]})
    assert tiers.tier_of("prompts") == "ga"
    assert tiers.tier_of("aria") == "beta"
    assert tiers.tier_of("metrics") == "internal"
    assert tiers.tier_of("nonexistent") is None


def test_stability_tolerates_a_missing_or_malformed_payload() -> None:
    assert Stability.from_payload(None).ga == ()
    assert Stability.from_payload({"ga": None}).ga == ()


def test_error_body_names_the_offending_field() -> None:
    body = ErrorBody.from_payload(
        {
            "detail": "request body validation failed",
            "status_code": 400,
            "errors": [{"loc": ["body", "name"], "msg": "field required", "type": "missing"}],
        }
    )
    assert body.status_code == 400
    assert body.errors[0].field == "body.name"


def test_error_body_defaults_a_whole_body_error() -> None:
    body = ErrorBody.from_payload({"detail": "bad", "status_code": 400, "errors": [{}]})
    assert body.errors[0].field == "<body>"
