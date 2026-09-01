"""Default predict_fn + dataset loading for the MLflow-backed eval gate.

The eval stage scores a candidate prompt by running it against an eval dataset
via ``mlflow.genai.evaluate``. Two pieces are needed that MLflow can't supply on
its own:

* a ``predict_fn`` that turns one example's inputs into a model output, and
* a ``load_dataset`` that turns a CALIBER ``eval_dataset_id`` into the
  list-of-``{"inputs", "expectations"}`` shape ``mlflow.genai.evaluate`` accepts
  (CALIBER eval datasets live in Postgres, not MLflow's dataset registry).

Operators can still register a bespoke per-agent predict_fn factory
(:meth:`MLflowEvalProvider.register_predict_fn`); this module provides the
*default* used when none is registered, so the gate runs out of the box for
ordinary prompt agents (candidate prompt = system instruction, example input =
the user turn) instead of raising. There is never a fake-score fallback: if no
real LLM provider/key is configured, the default is simply absent and eval
fails loudly.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from caliber.llm.models import (
    DEFAULT_OPENAI_REASONING_EFFORT,
    reasoning_effort_for_model,
    supports_temperature,
)
from caliber.secrets import resolve_secret

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from caliber.config import CaliberConfig

logger = logging.getLogger("caliber.eval.predict")

# (system, user) -> completion text.
CompletionFn = Callable[[str, str], str]
PredictFn = Callable[..., Any]
PredictFnFactory = Callable[[str], PredictFn]

_VAR_RE = re.compile(r"{{\s*(\w+)\s*}}")
_USER_FIELD_PREFERENCE = ("input", "question", "query", "prompt", "message", "text", "user")


def _render_template(prompt: str, inputs: dict[str, Any]) -> str:
    """Substitute ``{{var}}`` placeholders in ``prompt`` from ``inputs``.

    Unknown placeholders are left intact — the prompt may legitimately contain
    ``{{...}}`` that isn't an eval-input key.
    """
    return _VAR_RE.sub(lambda m: str(inputs.get(m.group(1), m.group(0))), prompt)


def _user_message(inputs: dict[str, Any]) -> str:
    """Pick the user turn from an example's inputs.

    Prefers a conventional text field (``input``/``question``/...), then the
    sole string value, then a JSON dump of the whole input dict so no
    information is silently dropped.
    """
    for key in _USER_FIELD_PREFERENCE:
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    string_values = [v for v in inputs.values() if isinstance(v, str) and v.strip()]
    if len(string_values) == 1:
        return string_values[0]
    return json.dumps(inputs, ensure_ascii=False, sort_keys=True)


def user_message(inputs: dict[str, Any]) -> str:
    """Public extraction of the user turn from an example's inputs.

    Used by the scorecard route (:mod:`caliber.routes.evaluations`) to build the
    user message for the ``llm`` predict target. Delegates to the same private
    field-preference logic the default predict_fn factory uses, so dataset eval
    and gate eval pick the user turn identically.
    """
    return _user_message(inputs)


def build_default_predict_fn_factory(complete: CompletionFn) -> PredictFnFactory:
    """A predict_fn factory for ordinary prompt agents.

    The candidate prompt becomes the system instruction (with ``{{var}}``
    placeholders filled from the example inputs); the example's input is the
    user turn. ``mlflow.genai.evaluate`` calls the returned predict_fn with the
    example's ``inputs`` dict as keyword arguments, so it accepts ``**inputs``.
    """

    def factory(prompt: str) -> PredictFn:
        def predict_fn(**inputs: Any) -> str:
            system = _render_template(prompt, inputs)
            user = _user_message(inputs)
            return complete(system, user)

        return predict_fn

    return factory


# ---------------------------------------------------------------------------
# Completion functions (real LLM calls) — mirrors caliber.workflows.judge.
# ---------------------------------------------------------------------------


def _supports_temperature(model: str) -> bool:
    """Whether ``model`` accepts a custom ``temperature`` (shared reasoning-model check)."""
    return supports_temperature(model)


def _openai_completion_fn(
    api_key: str,
    model: str,
    reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT,
) -> CompletionFn:
    from openai import OpenAI  # noqa: PLC0415

    client = OpenAI(api_key=api_key)

    def complete(system: str, user: str) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if _supports_temperature(model):
            kwargs["temperature"] = 0.0
        elif effort := reasoning_effort_for_model(model, reasoning_effort):
            kwargs["reasoning_effort"] = effort
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    return complete


def _anthropic_completion_fn(api_key: str, model: str) -> CompletionFn:
    from anthropic import Anthropic  # type: ignore[import-not-found]  # noqa: PLC0415

    client = Anthropic(api_key=api_key)

    def complete(system: str, user: str) -> str:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        blocks = getattr(response, "content", None) or []
        return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text")

    return complete


def build_completion_fn(config: CaliberConfig) -> CompletionFn | None:
    """Build a ``(system, user) -> text`` completion from config, or ``None``.

    Returns ``None`` when the LLM provider is fake/unset or no API key resolves
    — the caller then leaves the default predict_fn unset and eval requires an
    operator-registered factory (a loud failure, never fabricated scores).
    """
    provider = (getattr(config, "llm_provider", "fake") or "fake").strip().lower()
    if provider in ("", "fake", "deterministic"):
        return None
    api_key = resolve_secret(config.llm_api_key_env)
    if not api_key:
        logger.warning(
            "eval default predict_fn wanted a %s key at %r but none resolved; "
            "register a per-agent predict_fn or set the key.",
            provider,
            config.llm_api_key_env,
        )
        return None
    model = config.llm_diagnosis_model
    if provider == "openai":
        return _openai_completion_fn(
            api_key,
            model,
            getattr(config, "llm_reasoning_effort", DEFAULT_OPENAI_REASONING_EFFORT),
        )
    if provider == "anthropic":
        return _anthropic_completion_fn(api_key, model)
    return None


# ---------------------------------------------------------------------------
# Dataset loading — CALIBER eval datasets live in Postgres, not MLflow's registry.
# ---------------------------------------------------------------------------


def build_db_load_dataset(
    session_factory: sessionmaker[Session],
) -> Callable[..., list[dict[str, Any]]]:
    """Build ``load_dataset(eval_dataset_id, version=None) -> [{"inputs", ...}]``.

    Reads the examples of a CALIBER eval dataset and shapes them for
    ``mlflow.genai.evaluate``: each example's ``input`` dict becomes ``inputs``
    (passed to predict_fn as kwargs) and ``expected`` becomes ``expectations``
    (consumed by the scorers).

    When ``version`` is ``None`` the *current* active set is returned (examples
    that have not been superseded). When a pinned ``version`` is supplied the
    *historical* set "as of version N" is reconstructed for reproducibility:
    include examples that existed at version N (``dataset_version <= N``) and
    exclude those retired at or before N (``superseded_version <= N``). Examples
    superseded only after N — or never — stay in, exactly as the dataset looked
    when the run was launched.
    """

    def load(eval_dataset_id: str, version: int | None = None) -> list[dict[str, Any]]:
        from sqlalchemy import or_, select  # noqa: PLC0415

        from caliber.db.models import CaliberEvalDatasetExample  # noqa: PLC0415

        stmt = (
            select(CaliberEvalDatasetExample)
            .where(CaliberEvalDatasetExample.dataset_id == eval_dataset_id)
            .order_by(CaliberEvalDatasetExample.created_at)
        )
        if version is None:
            stmt = stmt.where(CaliberEvalDatasetExample.superseded_at.is_(None))
        else:
            stmt = stmt.where(CaliberEvalDatasetExample.dataset_version <= version).where(
                or_(
                    CaliberEvalDatasetExample.superseded_version.is_(None),
                    CaliberEvalDatasetExample.superseded_version > version,
                )
            )

        with session_factory() as session:
            rows = session.execute(stmt).scalars().all()
        data: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {"inputs": dict(row.input or {})}
            if row.expected:
                expectations = dict(row.expected)
                # MLflow's built-in Correctness scorer requires one of these
                # canonical fields. CALIBER historically stored UI-created
                # prompt cases as ``{"behavior": ...}`` (and accepted common
                # aliases such as ``answer``), which made every Correctness
                # invocation fail even though a usable reference was present.
                # Preserve the original shape for custom judges while adding
                # the canonical alias consumed by MLflow.
                if not any(
                    expectations.get(key) not in (None, "")
                    for key in ("expected_response", "expected_facts")
                ):
                    expected_response = _expected_response_text(expectations)
                    if expected_response:
                        expectations["expected_response"] = expected_response
                entry["expectations"] = expectations
            data.append(entry)
        if not data:
            suffix = "" if version is None else f" at version {version}"
            raise ValueError(f"eval dataset {eval_dataset_id!r} has no active examples{suffix}")
        return data

    return load


_EXPECTED_RESPONSE_FIELD_PREFERENCE = (
    "behavior",
    "expected",
    "answer",
    "output",
    "response",
    "expected_output",
    "label",
    "text",
    "value",
)


def _expected_response_text(expected: dict[str, Any]) -> str:
    """Return a conventional reference string for MLflow Correctness."""
    for key in _EXPECTED_RESPONSE_FIELD_PREFERENCE:
        value = expected.get(key)
        if isinstance(value, str) and value.strip():
            return value
    string_values = [
        value for value in expected.values() if isinstance(value, str) and value.strip()
    ]
    return string_values[0] if len(string_values) == 1 else ""
