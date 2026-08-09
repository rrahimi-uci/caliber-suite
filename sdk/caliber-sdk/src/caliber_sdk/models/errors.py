"""Typed views over CALIBER's two error body shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldError:
    """One entry of a structured validation failure."""

    loc: tuple[Any, ...] = ()
    msg: str = ""
    type: str = ""

    @property
    def field(self) -> str:
        """Dotted path of the offending field, or ``<body>`` for whole-body errors."""
        return ".".join(str(part) for part in self.loc) or "<body>"

    @classmethod
    def from_payload(cls, payload: Any) -> FieldError:
        if not isinstance(payload, dict):
            return cls()
        raw_loc = payload.get("loc")
        return cls(
            loc=tuple(raw_loc) if isinstance(raw_loc, list) else (),
            msg=str(payload.get("msg") or ""),
            type=str(payload.get("type") or ""),
        )


@dataclass(frozen=True)
class ErrorBody:
    """``{"detail", "status_code"}``, plus ``errors`` when present."""

    detail: str = ""
    status_code: int = 0
    errors: list[FieldError] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Any) -> ErrorBody:
        if not isinstance(payload, dict):
            return cls()
        raw_errors = payload.get("errors")
        return cls(
            detail=str(payload.get("detail") or ""),
            status_code=int(payload.get("status_code") or 0),
            errors=[FieldError.from_payload(item) for item in raw_errors]
            if isinstance(raw_errors, list)
            else [],
        )


__all__ = ["ErrorBody", "FieldError"]
