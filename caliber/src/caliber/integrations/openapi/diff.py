"""Deterministic diff between two imported OpenAPI versions.

Spec drift (§6 of the proposal) is only actionable if CALIBER can say precisely
what changed between the version a published tool was generated from and the
version just imported. This compares two already-normalized operation lists —
the persisted ``normalized_operation``/``side_effect_level``/etc. rows — not raw
documents, so the diff is stable across re-imports of byte-identical specs and
independent of key ordering.
"""

from __future__ import annotations

from typing import Any

#: Operation fields whose change is worth surfacing to a reviewer. Deliberately a
#: fixed list rather than "everything": free-text fields like ``description`` churn
#: on every doc pass and would drown the fields that change tool behavior.
_TRACKED_FIELDS = (
    "summary",
    "side_effect_level",
    "deprecated",
    "auth_schemes",
    "request_body_required",
    "request_content_types",
    "response_statuses",
    "tags",
)


def diff_operations(
    from_operations: list[dict[str, Any]],
    to_operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare two normalized-operation lists keyed by ``operation_key``.

    Each item is expected to carry at least ``operation_key`` plus the fields in
    ``_TRACKED_FIELDS``; extra fields are ignored. Works equally over ORM rows
    (via ``_field`` attribute access) or plain dicts from a fresh normalization.
    """

    from_index = {_field(item, "operation_key"): item for item in from_operations}
    to_index = {_field(item, "operation_key"): item for item in to_operations}
    from_keys = set(from_index)
    to_keys = set(to_index)

    added = sorted(to_keys - from_keys)
    removed = sorted(from_keys - to_keys)
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    breaking: list[dict[str, Any]] = []

    for key in sorted(from_keys & to_keys):
        before = from_index[key]
        after = to_index[key]
        field_changes = _field_changes(before, after)
        if field_changes:
            changed.append({"operation_key": key, "changes": field_changes})
            breaking_reason = _breaking_reason(field_changes)
            if breaking_reason:
                breaking.append({"operation_key": key, "reason": breaking_reason})
        else:
            unchanged.append(key)

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "breaking": breaking,
        "summary": {
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
            "unchanged_count": len(unchanged),
            "breaking_count": len(breaking),
        },
    }


def _field_changes(before: Any, after: Any) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for name in _TRACKED_FIELDS:
        old_value = _field(before, name)
        new_value = _field(after, name)
        if _normalize_for_compare(old_value) != _normalize_for_compare(new_value):
            changes[name] = {"from": old_value, "to": new_value}
    return changes


def _breaking_reason(field_changes: dict[str, dict[str, Any]]) -> str | None:
    """Which changed field, if any, should block silent re-publication.

    A removed operation is the obvious break; these are the subtler ones that a
    diff on raw text would not call out: the operation got safer to expose
    (``deprecated`` newly true) or its shape shifted under a published draft
    (auth requirement or required body changed).
    """

    if "deprecated" in field_changes and field_changes["deprecated"]["to"] is True:
        return "operation was deprecated"
    if "auth_schemes" in field_changes:
        return "authentication requirement changed"
    if "request_body_required" in field_changes:
        return "request body requirement changed"
    if "side_effect_level" in field_changes:
        return "side-effect classification changed"
    return None


def _normalize_for_compare(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(str(item) for item in value)
    return value


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)
