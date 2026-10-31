"""Trusted causal-link span attributes for governed effects (B11.3a, #737).

A governed effect crosses four persisted records: the effect *proposal*, the policy *decision* that
authorised the run, the runtime *approval* that was consumed, and the *effect* outcome (the verified
receipt).  A span that names those records lets an operator walk from a trace to the rows without
reading a payload.  Because a later task (B11.3b) reads these attributes back as a causal chain, the
only values allowed here are identifiers the platform has itself read from a persisted row:

* **Fixed allow-list.**  Exactly four keys exist (:data:`CAUSAL_ATTRIBUTE_KEYS`); nothing else is
  accepted from a caller, so this module cannot be used to attach a payload to a span.
* **Bounded and typed.**  A value must be a plain ``str`` that is an opaque identifier (ASCII,
  starts with a letter, at most :data:`MAX_ID_LENGTH` characters, no whitespace or ``@``).  Anything
  else is dropped, never truncated or redacted into a lookalike, and never raised: tracing must not
  change what a governed effect does.
* **Not forgeable from a manifest.**  :func:`without_reserved_keys` removes these keys from
  user-supplied tag maps (a manifest's ``mlflow.trace_group_tags``), so only trusted code sets them.
* **No messages.**  :func:`governed_span` records a failure as a stable ``caliber.refusal_code`` and
  the exception *type* only.  An exception message can carry arguments or a driver's SQL parameters,
  so it never reaches the span (MLflow would otherwise record it on the span's exception event).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from caliber.observability.mlflow_tracing import TraceSpan, get_tracer

logger = logging.getLogger(__name__)

#: The only keys this module emits, by the argument name callers use.
CAUSAL_ATTRIBUTE_KEYS: Final[Mapping[str, str]] = {
    "proposal_id": "caliber.proposal_id",
    "decision_id": "caliber.decision_id",
    "approval_id": "caliber.approval_id",
    "effect_id": "caliber.effect_id",
}
#: The span key for the stable code of a refused governed step.
REFUSAL_CODE_KEY: Final = "caliber.refusal_code"
#: Attribute keys a manifest author must not be able to set through trace-group tags.
RESERVED_SPAN_KEYS: Final[frozenset[str]] = frozenset(CAUSAL_ATTRIBUTE_KEYS.values()) | {
    REFUSAL_CODE_KEY
}

MAX_ID_LENGTH: Final = 128
# Every id the platform mints is ``<PREFIX>-<hex>`` / ``pd_<hex>``.  Starting with a letter keeps a
# bare number (an SSN or phone-like value) from ever passing as an identifier.
_IDENTIFIER: Final = re.compile(rf"[A-Za-z][A-Za-z0-9._:-]{{0,{MAX_ID_LENGTH - 1}}}", re.ASCII)
_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)


def _identifier(value: object) -> str | None:
    if type(value) is str and _IDENTIFIER.fullmatch(value):
        return value
    return None


def causal_attributes(
    *,
    proposal_id: object = None,
    decision_id: object = None,
    approval_id: object = None,
    effect_id: object = None,
) -> dict[str, str]:
    """The ``caliber.*`` causal-link attributes for the identifiers given.

    ``None`` is "not known here" and is omitted.  A value that is not an opaque identifier (wrong
    type, too long, whitespace, non-ASCII, an email address, a leading digit) is also omitted; the
    value itself is never logged.
    """
    given = {
        "proposal_id": proposal_id,
        "decision_id": decision_id,
        "approval_id": approval_id,
        "effect_id": effect_id,
    }
    attributes: dict[str, str] = {}
    for name, value in given.items():
        if value is None:
            continue
        clean = _identifier(value)
        if clean is None:
            logger.debug("dropping a non-identifier value for span attribute %s", name)
            continue
        attributes[CAUSAL_ATTRIBUTE_KEYS[name]] = clean
    return attributes


def _platform_refusals() -> tuple[type[BaseException], ...]:
    """The refusal types whose ``code`` is a stable platform vocabulary.

    Imported lazily: each of these modules opens spans through this one.  Any other exception that
    happens to carry a ``code`` attribute (a driver, a client library) is foreign and its code is
    not copied to a span.
    """
    from caliber.effects.dispatcher import EffectRefused  # noqa: PLC0415 - import cycle
    from caliber.effects.records import ProposalError  # noqa: PLC0415 - import cycle
    from caliber.workflows.approval_policy import ApprovalCommitRefusedError  # noqa: PLC0415

    return (ProposalError, EffectRefused, ApprovalCommitRefusedError)


def without_reserved_keys(tags: Mapping[str, str]) -> dict[str, str]:
    """``tags`` without any key only trusted code may set (a manifest cannot assert a causal link)."""
    return {key: value for key, value in tags.items() if key not in RESERVED_SPAN_KEYS}


class GovernedSpan:
    """A span that can only be given identifiers that pass :func:`causal_attributes`."""

    def __init__(self, span: TraceSpan) -> None:
        self._span = span

    @property
    def attributes(self) -> dict[str, Any]:
        """The attributes recorded so far (a copy)."""
        return dict(self._span.attributes)

    def bind(
        self,
        *,
        proposal_id: object = None,
        decision_id: object = None,
        approval_id: object = None,
        effect_id: object = None,
    ) -> None:
        """Record the identifiers the caller has just read from a persisted row."""
        for key, value in causal_attributes(
            proposal_id=proposal_id,
            decision_id=decision_id,
            approval_id=approval_id,
            effect_id=effect_id,
        ).items():
            # Tracing must never change what a governed effect does, and this runs after the effect's own
            # transaction has committed: a redactor or tracer failure is logged and the attribute is absent.
            try:
                self._span.set_attribute(key, value)
            except Exception:
                logger.debug("failed binding span attribute %s", key, exc_info=True)

    def record_failure(self, exc: BaseException) -> None:
        """Mark the span failed from the exception's *type* and, for a platform refusal, its stable ``code``.

        The message is never recorded, and neither is the ``code`` of a foreign exception.
        """
        code = getattr(exc, "code", None) if isinstance(exc, _platform_refusals()) else None
        label = code if isinstance(code, str) and _CODE.fullmatch(code) else None
        try:
            if label is not None:
                self._span.set_attribute(REFUSAL_CODE_KEY, label)
            self._span.set_attribute("caliber.error_type", type(exc).__name__)
            self._span.mark_error(label or type(exc).__name__)
        except Exception:  # never let tracing replace the exception being recorded
            logger.debug("failed recording a failure on span %s", self._span.name, exc_info=True)


@contextmanager
def governed_span(
    name: str,
    *,
    span_type: str = "CHAIN",
    proposal_id: object = None,
    decision_id: object = None,
    approval_id: object = None,
    effect_id: object = None,
) -> Iterator[GovernedSpan]:
    """Open a span for one governed step; the exception it raises is re-raised unchanged.

    Identifiers passed here must already be read from a persisted row; ones learned later are added
    with :meth:`GovernedSpan.bind`.  A failure inside the block is recorded without its message (see
    the module docstring) and then propagates *after* the span has closed, so the tracer's own
    exception recording, which keeps the message, never sees it.  Tracing is inert when no tracer is
    configured.
    """
    failure: Exception | None = None
    with get_tracer().span(name, span_type=span_type) as span:
        handle = GovernedSpan(span)
        # Bound after the span opens, not passed to it: opening sanitizes its start attributes with no guard.
        handle.bind(
            proposal_id=proposal_id,
            decision_id=decision_id,
            approval_id=approval_id,
            effect_id=effect_id,
        )
        try:
            yield handle
        except Exception as exc:
            handle.record_failure(exc)
            failure = exc
    if failure is not None:
        raise failure


__all__ = [
    "CAUSAL_ATTRIBUTE_KEYS",
    "MAX_ID_LENGTH",
    "REFUSAL_CODE_KEY",
    "RESERVED_SPAN_KEYS",
    "GovernedSpan",
    "causal_attributes",
    "governed_span",
    "without_reserved_keys",
]
