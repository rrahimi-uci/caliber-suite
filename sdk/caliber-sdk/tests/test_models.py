"""Shared model helpers."""

from __future__ import annotations

import dataclasses

from caliber_sdk import ErrorBody, Page, Stability
from caliber_sdk.models import core as core_models


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


#: Every model dataclass that carries an `extra` field, and the exact
#: field order that existed the last time each was checked. A
#: `@dataclass` field's declaration position is also its positional-
#: constructor position, so a field inserted *before* an existing one
#: silently rebinds an existing caller's positional argument to the new
#: field instead -- the exact defect a GitHub Copilot review caught and
#: fixed for `Project.archived_at`/`archived_by` (#297), and one this
#: session's own `PersonalAccessToken.project_id` addition repeated and
#: had to fix again in the same PR that adds this test. New fields must
#: only ever be *appended* here (never inserted mid-list) as this dict is
#: updated alongside them -- the assertion below fails by name the moment
#: that discipline lapses, rather than silently shipping a rebound field.
_EXPECTED_FIELD_ORDER: dict[str, tuple[str, ...]] = {
    "PersonalAccessToken": (
        "token_id",
        "user_id",
        "name",
        "scopes",
        "created_at",
        "created_by",
        "expires_at",
        "last_used_at",
        "revoked_at",
        "revoked_reason",
        "rotated_from",
        "active",
        "extra",
        "project_id",
    ),
    "Project": (
        "project_id",
        "name",
        "description",
        "owner",
        "status",
        "storage_backend",
        "created_at",
        "updated_at",
        "file_count",
        "access_role",
        "permissions",
        "extra",
        "archived_at",
        "archived_by",
    ),
}


def test_model_dataclass_field_order_is_pinned() -> None:
    for class_name, expected in _EXPECTED_FIELD_ORDER.items():
        obj = getattr(core_models, class_name)
        actual = tuple(f.name for f in dataclasses.fields(obj))
        assert actual == expected, (
            f"{class_name}'s field order changed: {actual} vs pinned {expected}. If a "
            f"field was deliberately added, append it to `_EXPECTED_FIELD_ORDER` here "
            f"-- inserting it anywhere but the end would rebind an existing caller's "
            f"positional argument (see this test's own docstring/#297)."
        )
