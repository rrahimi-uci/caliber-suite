"""No-op-safe MLflow tracing helpers shared across CALIBER subsystems.

This module generalizes the guarded span/run pattern that previously lived only
in :mod:`caliber.assistant.tracing` and :mod:`caliber.orchestrator.worker` so the
workflow runtime, the orchestrator, and the assistant can all emit MLflow traces
through one helper (golden-path roadmap, Wave 0 — the "shared tracing helper").

Design contract:

* **Guarded / no-op-safe.** Every MLflow touch is wrapped so that when MLflow is
  unavailable, tracing is disabled, or an MLflow call raises, the helper degrades
  to a no-op and never changes the behavior of the traced code.
* **On-but-inert by default.** ``tracing_enabled`` defaults true; with no MLflow
  installed the tracer is inert. Autolog only patches provider SDKs that are
  actually importable.
* **PII-redacted.** All span attributes / run tags pass through the audit
  redactor (:func:`caliber.audit.get_redactor`) and a byte cap before they reach
  MLflow — tracing is a new data-emission surface, so it must not leak raw PII.
* **Never swallow body exceptions.** ``trace_run`` / ``span`` only guard the
  MLflow setup/teardown; an exception raised by the traced code propagates
  unchanged (and is recorded on the span as ``caliber.status=failed``).
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from caliber.audit import get_redactor

logger = logging.getLogger(__name__)

_MISSING = object()
DEFAULT_MAX_ATTRIBUTE_BYTES = 4096
_CACHED_PRICING_RATE_COUNT = 3

ModelPricingRates = tuple[float, float] | tuple[float, float, float]

# Approximate USD per 1K tokens as ``(prompt, completion[, cached_prompt])``.
# Used only for span / run cost attribution; unknown models contribute 0.0 cost
# (we never guess). The table is intentionally small and matched by longest-
# prefix so versioned model ids (``gpt-4o-2024-08-06``) resolve to their family
# rate. Models whose cached-input rate is omitted fall back to the standard
# prompt-input rate for conservative accounting.
DEFAULT_MODEL_PRICING: dict[str, ModelPricingRates] = {
    "gpt-5.5": (0.005, 0.03, 0.0005),
    "gpt-5.4-mini": (0.00075, 0.0045, 0.000075),
    "gpt-5.4": (0.0025, 0.015, 0.00025),
    "gpt-4o-mini": (0.00015, 0.0006, 0.000075),
    "gpt-4o": (0.0025, 0.01, 0.00125),
    "gpt-4.1-mini": (0.0004, 0.0016, 0.0001),
    "gpt-4.1": (0.002, 0.008, 0.0005),
    "o4-mini": (0.0011, 0.0044),
    "claude-opus-4": (0.015, 0.075),
    "claude-sonnet-4": (0.003, 0.015),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
}


# --- Operator pricing overrides ----------------------------------------------
# DB-backed per-model rates (``caliber_llm_model_pricing``) override / extend the
# defaults above. Resolved lazily + cached (short TTL) so a rate edit applies to
# new cost attribution promptly without a per-span DB hit. When no DB source is
# registered (tests, scripts) cost falls back to DEFAULT_MODEL_PRICING — so the
# pure-default behavior is unchanged anywhere a source hasn't been wired.
_PRICING_TTL_SECONDS = 60.0
_pricing_lock = threading.Lock()
_pricing_session_factory: Callable[[], Any] | None = None
_pricing_cache: tuple[float, dict[str, ModelPricingRates]] | None = None  # (expires_at, table)


def register_pricing_source(session_factory: Callable[[], Any] | None) -> None:
    """Register the DB session factory used to resolve per-model pricing overrides.

    Called once at app startup (``create_app``); resets the cache so the next
    cost computation reflects the live table.
    """
    global _pricing_session_factory, _pricing_cache  # noqa: PLW0603 — process-wide pricing source
    with _pricing_lock:
        _pricing_session_factory = session_factory
        _pricing_cache = None


def invalidate_pricing_cache() -> None:
    """Drop the cached merged pricing table (call after a pricing row changes)."""
    global _pricing_cache  # noqa: PLW0603 — process-wide pricing cache
    with _pricing_lock:
        _pricing_cache = None


def resolve_model_pricing(session_factory: Callable[[], Any]) -> dict[str, ModelPricingRates]:
    """Merge active ``caliber_llm_model_pricing`` rows over DEFAULT_MODEL_PRICING (DB wins)."""
    from sqlalchemy import select  # noqa: PLC0415

    from caliber.db.models import CaliberLlmModelPricing  # noqa: PLC0415

    table: dict[str, ModelPricingRates] = dict(DEFAULT_MODEL_PRICING)
    with session_factory() as session:
        rows = (
            session.execute(
                select(CaliberLlmModelPricing).where(CaliberLlmModelPricing.status == "active")
            )
            .scalars()
            .all()
        )
    for row in rows:
        key = (row.model_id or "").strip().lower()
        if not key:
            continue
        if row.cached_prompt_price is not None:
            table[key] = (row.prompt_price, row.completion_price, row.cached_prompt_price)
        else:
            table[key] = (row.prompt_price, row.completion_price)
    return table


def _effective_pricing() -> Mapping[str, ModelPricingRates]:
    """Pricing table used by :func:`model_cost_usd` when no explicit table is passed.

    Returns the cached DB-merged table when a pricing source is registered, else
    the built-in defaults. Never raises — a DB error degrades to the defaults.
    """
    global _pricing_cache  # noqa: PLW0603 — process-wide pricing cache
    with _pricing_lock:
        factory = _pricing_session_factory
        cache = _pricing_cache
    if factory is None:
        return DEFAULT_MODEL_PRICING
    if cache is not None and cache[0] > time.monotonic():
        return cache[1]
    try:
        table = resolve_model_pricing(factory)
    except Exception:  # DB unavailable — never fail a cost calc over pricing
        logger.debug("pricing override resolve failed; using defaults", exc_info=True)
        return DEFAULT_MODEL_PRICING
    with _pricing_lock:
        _pricing_cache = (time.monotonic() + _PRICING_TTL_SECONDS, table)
    return table


def _truncate_string(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    suffix = "...[truncated]"
    keep = max(0, max_bytes - len(suffix.encode("utf-8")))
    truncated = raw[:keep].decode("utf-8", errors="ignore")
    return f"{truncated}{suffix}"


def sanitize_trace_value(value: Any, *, max_bytes: int = DEFAULT_MAX_ATTRIBUTE_BYTES) -> Any:
    """Redact and byte-cap a value before sending it to trace attributes."""
    redacted = get_redactor().redact_value(value)
    if redacted is None or isinstance(redacted, bool | int | float):
        return redacted
    if isinstance(redacted, str):
        return _truncate_string(redacted, max_bytes)
    try:
        encoded = json.dumps(redacted, sort_keys=True, default=str)
    except TypeError:
        encoded = str(redacted)
    return _truncate_string(encoded, max_bytes)


def sanitize_trace_attributes(
    attributes: Mapping[str, Any] | None,
    *,
    max_bytes: int = DEFAULT_MAX_ATTRIBUTE_BYTES,
) -> dict[str, Any]:
    """Return MLflow-safe, redacted, byte-capped span attributes / run tags."""
    return {
        str(key): sanitize_trace_value(value, max_bytes=max_bytes)
        for key, value in dict(attributes or {}).items()
    }


def _match_model(model: str, table: Mapping[str, ModelPricingRates]) -> str | None:
    if not model:
        return None
    needle = model.strip().lower()
    if needle in table:
        return needle
    best: str | None = None
    for key in table:
        if needle.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return best


def model_cost_usd(
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    pricing: Mapping[str, ModelPricingRates] | None = None,
) -> float:
    """USD cost for a single LLM call. Unknown model → 0.0 (never guessed).

    When no explicit ``pricing`` table is passed, the operator-configured rates
    (DB overrides merged over the defaults) are used via :func:`_effective_pricing`.
    """
    table = pricing if pricing is not None else _effective_pricing()
    key = _match_model(model or "", table)
    if key is None:
        return 0.0
    rates = table[key]
    if len(rates) == _CACHED_PRICING_RATE_COUNT:
        cached_rates = cast(tuple[float, float, float], rates)
        price_in = cached_rates[0]
        price_out = cached_rates[1]
        cached_price_in = cached_rates[2]
    else:
        uncached_rates = cast(tuple[float, float], rates)
        price_in = uncached_rates[0]
        price_out = uncached_rates[1]
        cached_price_in = price_in
    prompt = int(prompt_tokens or 0)
    cached = max(0, min(prompt, int(cached_prompt_tokens or 0)))
    uncached = max(0, prompt - cached)
    cost = (uncached / 1000.0) * price_in
    cost += (cached / 1000.0) * cached_price_in
    cost += (int(completion_tokens or 0) / 1000.0) * price_out
    return round(cost, 6)


@dataclass
class TraceSpan:
    """Handle for a (possibly no-op) MLflow span. ``set_attribute`` redacts."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    mlflow_trace_id: str | None = None
    _span: Any | None = field(default=None, repr=False)
    _max_attribute_bytes: int = field(default=DEFAULT_MAX_ATTRIBUTE_BYTES, repr=False)
    _pricing: Mapping[str, ModelPricingRates] | None = field(default=None, repr=False)
    # Accumulated multimodal attachments, re-applied as span inputs on each
    # ``attach`` so multiple files on one span don't overwrite each other.
    _attachments: dict[str, Any] = field(default_factory=dict, repr=False)

    def set_attribute(self, key: str, value: Any) -> None:
        clean = sanitize_trace_value(value, max_bytes=self._max_attribute_bytes)
        self.attributes[str(key)] = clean
        setter = getattr(self._span, "set_attribute", None)
        if not callable(setter):
            return
        try:
            setter(str(key), clean)
        except Exception:
            logger.debug("failed setting span attribute %s", key, exc_info=True)

    def attach(self, name: str, content_bytes: bytes, content_type: str) -> bool:
        """Attach a binary artifact (image / PDF / audio) to this span.

        Uses MLflow multimodal tracing (``mlflow.tracing.attachments.Attachment``,
        MLflow 3.12+): the bytes are uploaded alongside the trace and the span
        input is replaced with a reference URI, so the trace UI renders the actual
        source document. Accumulates across calls so multiple attachments on one
        span don't clobber each other.

        Returns ``True`` when attached; ``False`` when tracing is inert/unavailable
        or the attachment can't be built (never raises — observability is
        best-effort).
        """
        span = self._span
        if span is None:
            return False
        try:
            from mlflow.tracing.attachments import Attachment  # noqa: PLC0415
        except Exception:
            return False
        try:
            attachment = Attachment(
                content_type=str(content_type), content_bytes=bytes(content_bytes)
            )
        except Exception:
            logger.debug("failed building attachment %s", name, exc_info=True)
            return False
        self._attachments[str(name)] = attachment
        setter = getattr(span, "set_inputs", None)
        if not callable(setter):
            return False
        try:
            setter(dict(self._attachments))
        except Exception:
            logger.debug("failed attaching %s to span", name, exc_info=True)
            return False
        # A plain-text marker so CALIBER's redacted trace viewer (which reads
        # ``caliber.*`` attributes, not the binary) still surfaces the attachment.
        self.set_attribute(f"caliber.attachment.{name}", content_type)
        return True

    def record_usage(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_prompt_tokens: int = 0,
        total_tokens: int | None = None,
        model: str | None = None,
    ) -> float:
        """Record token usage + (when the split is known) USD cost on the span.

        Returns the computed cost (0.0 when the prompt/completion split or model
        is unknown — we record tokens but never fabricate a cost).
        """
        prompt = int(prompt_tokens or 0)
        completion = int(completion_tokens or 0)
        total = int(total_tokens) if total_tokens is not None else prompt + completion
        if total:
            self.set_attribute("caliber.tokens", total)
        if prompt:
            self.set_attribute("caliber.prompt_tokens", prompt)
        if completion:
            self.set_attribute("caliber.completion_tokens", completion)
        cached_prompt = max(0, min(prompt, int(cached_prompt_tokens or 0)))
        if cached_prompt:
            self.set_attribute("caliber.cached_prompt_tokens", cached_prompt)
        if model:
            # Record the model so usage can be rolled up by-model (gateway usage tab).
            self.set_attribute("caliber.model", model)
        cost = model_cost_usd(
            model or "",
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_prompt_tokens=cached_prompt,
            pricing=self._pricing,
        )
        if cost:
            self.set_attribute("caliber.cost_usd", cost)
        return cost


@dataclass
class RunHandle:
    """Handle for a (possibly no-op) MLflow run."""

    name: str
    run_id: str | None = None


class Tracer:
    """Small guarded adapter around the optional MLflow tracing/tracking APIs."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        mlflow_module: Any = _MISSING,
        max_attribute_bytes: int = DEFAULT_MAX_ATTRIBUTE_BYTES,
        experiment: str | None = None,
        pricing: Mapping[str, ModelPricingRates] | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._mlflow_module = mlflow_module
        self.max_attribute_bytes = max(256, int(max_attribute_bytes))
        self._experiment = experiment or None
        self._pricing = pricing

    @property
    def enabled(self) -> bool:
        return self._enabled

    def mlflow_module(self) -> Any | None:
        if not self._enabled:
            return None
        if self._mlflow_module is not _MISSING:
            return self._mlflow_module
        try:
            self._mlflow_module = importlib.import_module("mlflow")
        except ImportError:
            self._mlflow_module = None
        except Exception:
            logger.debug("unable to import MLflow for tracing", exc_info=True)
            self._mlflow_module = None
        return self._mlflow_module

    def active_run_id(self, mlflow_mod: Any | None = None) -> str | None:
        module = mlflow_mod if mlflow_mod is not None else self.mlflow_module()
        if module is None:
            return None
        active_run = getattr(module, "active_run", None)
        if not callable(active_run):
            return None
        try:
            run = active_run()
        except Exception:
            logger.debug("failed reading active MLflow run", exc_info=True)
            return None
        info = getattr(run, "info", None)
        run_id = getattr(info, "run_id", None)
        return run_id if isinstance(run_id, str) and run_id else None

    def annotate_trace(self, *, session_id: str | None = None, user: str | None = None) -> None:
        """Stamp the active trace with MLflow-native session/user metadata.

        Sets ``mlflow.trace.session`` / ``mlflow.trace.user`` so multi-turn runs
        group into MLflow sessions and the Observability Session/User columns
        populate. No-op when nothing to set or MLflow/tracing is unavailable;
        never raises (annotation must not break a traced run).
        """
        metadata: dict[str, str] = {}
        if session_id:
            metadata["mlflow.trace.session"] = str(session_id)
        if user:
            metadata["mlflow.trace.user"] = str(user)
        if not metadata:
            return
        module = self.mlflow_module()
        update = getattr(module, "update_current_trace", None) if module else None
        if not callable(update):
            return
        try:
            update(metadata=metadata)
        except Exception:
            logger.debug("failed annotating trace session/user", exc_info=True)

    def extract_trace_id(self, span: Any | None) -> str | None:
        if span is None:
            return None
        for attr in ("trace_id", "request_id"):
            value = getattr(span, attr, None)
            if isinstance(value, str) and value:
                return value
        return None

    def _set_experiment(self, module: Any, experiment: str) -> None:
        setter = getattr(module, "set_experiment", None)
        if not callable(setter):
            return
        try:
            setter(experiment)
        except Exception:
            logger.debug("failed selecting MLflow experiment %s", experiment, exc_info=True)

    @contextmanager
    def trace_run(
        self,
        name: str,
        *,
        tags: Mapping[str, Any] | None = None,
        experiment: str | None = None,
    ) -> Iterator[RunHandle]:
        """Open an MLflow run for the duration of the block.

        Reuses an already-active run (does not nest). Body exceptions propagate;
        the run is closed with the exception so MLflow marks it FAILED.
        """
        handle = RunHandle(name=name)
        module = self.mlflow_module()
        cm: Any | None = None
        if module is not None:
            active = self.active_run_id(module)
            if active is not None:
                handle.run_id = active
            else:
                start_run = getattr(module, "start_run", None)
                if callable(start_run):
                    try:
                        exp = experiment or self._experiment
                        if exp:
                            self._set_experiment(module, exp)
                        kwargs: dict[str, Any] = {}
                        if name:
                            kwargs["run_name"] = name
                        clean_tags = sanitize_trace_attributes(
                            tags, max_bytes=self.max_attribute_bytes
                        )
                        if clean_tags:
                            kwargs["tags"] = clean_tags
                        cm = start_run(**kwargs)
                        run = cm.__enter__()
                        info = getattr(run, "info", None)
                        handle.run_id = getattr(info, "run_id", None)
                    except Exception:
                        logger.debug("unable to open MLflow run %s", name, exc_info=True)
                        cm = None
        err: BaseException | None = None
        try:
            yield handle
        except BaseException as exc:  # re-raised below; tracing never swallows body errors
            err = exc
            raise
        finally:
            if cm is not None:
                tb = getattr(err, "__traceback__", None)
                try:
                    cm.__exit__(type(err) if err else None, err, tb)
                except Exception:
                    logger.debug("failed closing MLflow run %s", name, exc_info=True)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        span_type: str = "CHAIN",
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[TraceSpan]:
        """Open an MLflow span. Body exceptions propagate and are recorded."""
        handle = TraceSpan(
            name=name,
            _max_attribute_bytes=self.max_attribute_bytes,
            _pricing=self._pricing,
        )
        base = sanitize_trace_attributes(attributes, max_bytes=self.max_attribute_bytes)
        handle.attributes = dict(base)
        module = self.mlflow_module()
        cm: Any | None = None
        if module is not None:
            start_span = getattr(module, "start_span", None)
            if callable(start_span):
                try:
                    cm = start_span(name=name, span_type=span_type, attributes=base or None)
                    span_obj = cm.__enter__()
                    handle._span = span_obj
                    handle.mlflow_trace_id = self.extract_trace_id(span_obj)
                except Exception:
                    logger.debug("unable to open MLflow span %s", name, exc_info=True)
                    cm = None
        err: BaseException | None = None
        try:
            yield handle
        except BaseException as exc:  # re-raised below; tracing never swallows body errors
            err = exc
            handle.set_attribute("caliber.status", "failed")
            handle.set_attribute("caliber.error_type", type(exc).__name__)
            raise
        finally:
            if err is None:
                handle.set_attribute("caliber.status", "completed")
            if cm is not None:
                tb = getattr(err, "__traceback__", None)
                try:
                    cm.__exit__(type(err) if err else None, err, tb)
                except Exception:
                    logger.debug("failed closing MLflow span %s", name, exc_info=True)


_TRACER: Tracer | None = None


def _enable_autolog(module: Any | None) -> list[str]:
    """Best-effort enable mlflow autolog for installed provider SDKs.

    Returns the list of providers autolog was enabled for (useful for tests /
    logging). Only patches a provider when its SDK is importable, so this is a
    no-op on a server without ``openai`` / ``anthropic`` installed.
    """
    if module is None:
        return []
    enabled: list[str] = []
    for submodule, package in (("openai", "openai"), ("anthropic", "anthropic")):
        try:
            importlib.import_module(package)
        except ImportError:
            continue
        except Exception:
            logger.debug("provider SDK %s import failed", package, exc_info=True)
            continue
        try:
            mod = importlib.import_module(f"mlflow.{submodule}")
            autolog = getattr(mod, "autolog", None)
            if callable(autolog):
                autolog()
                enabled.append(submodule)
        except Exception:
            logger.debug("autolog unavailable for mlflow.%s", submodule, exc_info=True)
    return enabled


def configure_tracing(config: Any) -> Tracer:
    """Build the process-wide tracer from config and enable autolog.

    Called once from :func:`caliber.server.create_app`. Subsequent
    :func:`get_tracer` calls return this instance.
    """
    global _TRACER  # noqa: PLW0603 — process-wide tracer singleton; see module docstring
    enabled = bool(getattr(config, "tracing_enabled", True))
    tracer = Tracer(
        enabled=enabled,
        max_attribute_bytes=int(
            getattr(config, "tracing_max_attribute_bytes", DEFAULT_MAX_ATTRIBUTE_BYTES)
        ),
        experiment=getattr(config, "tracing_experiment", "") or None,
    )
    _TRACER = tracer
    if enabled and bool(getattr(config, "tracing_autolog_enabled", True)):
        providers = _enable_autolog(tracer.mlflow_module())
        if providers:
            logger.info("CALIBER tracing: autolog enabled for %s", ", ".join(providers))
    return tracer


def get_tracer() -> Tracer:
    """Return the process-wide tracer.

    Until :func:`configure_tracing` runs (which ``create_app`` calls at startup),
    the default is an *inert* tracer — so code paths instrumented with the tracer
    are no-ops in unit tests, scripts, and any context that has not opted in.
    Tests that exercise tracing inject an enabled tracer via :func:`set_tracer`.
    """
    global _TRACER  # noqa: PLW0603 — process-wide tracer singleton; see module docstring
    if _TRACER is None:
        _TRACER = Tracer(enabled=False)
    return _TRACER


def set_tracer(tracer: Tracer | None) -> None:
    """Override the process-wide tracer (test seam)."""
    global _TRACER  # noqa: PLW0603 — process-wide tracer singleton; see module docstring
    _TRACER = tracer
