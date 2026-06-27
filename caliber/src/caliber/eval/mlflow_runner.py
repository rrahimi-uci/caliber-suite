"""Production EvalProvider backed by ``mlflow.genai.evaluate``.

The hard part of running the eval stage against a real backend is that
CALIBER doesn't *know* how to run an agent — that's the operator's
business. So this provider exposes a small registration surface:

* :meth:`register_predict_fn` — operator supplies a per-agent factory
  that takes a prompt string and returns a ``predict_fn`` (the callable
  ``mlflow.genai.evaluate`` invokes per example).
* :attr:`load_dataset` — callable that turns an ``eval_dataset_id`` into
  whatever ``mlflow.genai.evaluate`` accepts as its ``data`` argument
  (a list of dicts, a pandas DataFrame, or an MLflow
  ``EvaluationDataset``). Defaults to MLflow's own dataset registry.
* :attr:`scorers` — list of scorer objects. Defaults to the standard
  CALIBER suite (Correctness, Guidelines, RelevanceToQuery, Safety) from
  ``mlflow.genai.scorers``.

On :meth:`evaluate` we:

1. Look up the agent's factory; raise :class:`EvalProviderError` if none
   has been registered.
2. Build a candidate ``predict_fn`` from ``request.candidate_content``,
   call ``mlflow.genai.evaluate``, fold its metrics into a
   :class:`ScoreSet`.
3. If ``request.baseline_content`` is provided, do the same for the
   baseline.
4. Return the :class:`EvalComparison` (deltas computed by the helper
   shared with :class:`FakeEvalProvider`).

Every backend exception is wrapped in :class:`EvalProviderError` so the
orchestrator's error handling stays simple and the operator gets a
human-readable reason on a failure.
"""

from __future__ import annotations

import inspect
import logging
import math
from collections.abc import Callable
from typing import Any

from caliber.eval.judge_scorer import JUDGE_VALUE_TYPE_MAP, JudgeError, build_judge
from caliber.eval.provider import (
    EvalComparison,
    EvalProviderError,
    EvalRequest,
    ScoreSet,
    apply_scorer_weights,
)

logger = logging.getLogger("caliber.eval.mlflow_runner")

# A predict_fn takes one example (dict) and returns the model's output —
# typically a string but anything the scorers accept is fine. The factory
# wraps the prompt content so the predict_fn closes over it.
PredictFn = Callable[..., Any]
PredictFnFactory = Callable[[str], PredictFn]

# Re-exported for back-compat; the canonical map lives in ``eval.judge_scorer``.
_JUDGE_VALUE_TYPE_MAP: dict[Any, type] = JUDGE_VALUE_TYPE_MAP


class MLflowEvalProvider:
    """Production EvalProvider against ``mlflow.genai.evaluate``.

    Operator wiring (typically in the agent's own bootstrap code) registers
    a per-agent factory:

    .. code-block:: python

        provider = MLflowEvalProvider()
        provider.register_predict_fn("support-agent", make_support_predict_fn)

    where ``make_support_predict_fn(prompt: str) -> Callable`` returns a
    function MLflow will call once per example. CALIBER never imports the
    agent's code directly — the factory keeps that dependency out of the
    plugin.
    """

    def __init__(
        self,
        predict_fn_factories: dict[str, PredictFnFactory] | None = None,
        load_dataset: Callable[..., Any] | None = None,
        scorers: list[Any] | None = None,
        default_factory: PredictFnFactory | None = None,
    ) -> None:
        self._factories: dict[str, PredictFnFactory] = dict(predict_fn_factories or {})
        # Used for any agent without a registered factory. ``build_provider``
        # wires a real LLM-backed default (caliber.eval.predict) so the gate
        # runs out of the box; ``None`` preserves the strict "register first"
        # behaviour (and never fabricates scores).
        self._default_factory = default_factory
        self._load_dataset = load_dataset
        # ``None`` means "load defaults at evaluate-time" so the scorer
        # imports stay lazy — the unit-test suite shouldn't have to pull
        # in ``mlflow.genai.scorers`` just to construct this class.
        self._scorers = scorers

    def register_predict_fn(self, agent_id: str, factory: PredictFnFactory) -> None:
        """Register the predict-fn factory for ``agent_id``.

        Re-registering overrides the prior factory — useful for hot-reload
        in development. Production typically registers once at startup.
        """
        self._factories[agent_id] = factory

    def evaluate(self, request: EvalRequest) -> EvalComparison:
        try:
            import mlflow  # noqa: PLC0415
        except ImportError as exc:
            raise EvalProviderError(
                "mlflow is not installed; install caliber with its "
                "default dependencies to use MLflowEvalProvider."
            ) from exc

        factory = self._factories.get(request.agent_id) or self._default_factory
        if factory is None:
            raise EvalProviderError(
                f"no predict_fn for agent_id={request.agent_id!r}: register one via "
                "MLflowEvalProvider.register_predict_fn, or configure a real LLM "
                "provider so the default predict_fn (caliber.eval.predict) is wired."
            )

        data = self._resolve_dataset(request.eval_dataset_id, request.eval_dataset_version)
        scorers = self._resolve_scorers(request.scorer_names, request.scorer_configs)

        candidate_scores = self._run_eval_pass(
            mlflow,
            factory(request.candidate_content),
            data,
            scorers,
            label="candidate",
            request=request,
        )
        candidate_scores = apply_scorer_weights(candidate_scores, request.scorer_weights)

        baseline_scores: ScoreSet | None
        if request.baseline_content is None:
            baseline_scores = None
        else:
            baseline_scores = self._run_eval_pass(
                mlflow,
                factory(request.baseline_content),
                data,
                scorers,
                label="baseline",
                request=request,
            )
            baseline_scores = apply_scorer_weights(baseline_scores, request.scorer_weights)

        deltas = _compute_deltas(candidate_scores, baseline_scores)
        n_examples = _count_examples(data)
        return EvalComparison(
            candidate=candidate_scores,
            baseline=baseline_scores,
            deltas=deltas,
            eval_dataset_id=request.eval_dataset_id,
            n_examples=n_examples,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _resolve_dataset(self, eval_dataset_id: str, version: int | None = None) -> Any:
        if self._load_dataset is not None:
            # Decide arity from the loader's *signature* rather than catching a
            # TypeError from the call: a blanket ``except TypeError`` also
            # swallows a TypeError raised inside a version-aware loader's body
            # and silently retries without the version, which resolves the
            # CURRENT active set instead of the pinned historical one — quietly
            # breaking reproducibility. With a signature check, an internal
            # TypeError propagates and is wrapped as EvalProviderError (loud).
            try:
                if _loader_accepts_version(self._load_dataset):
                    # Version-aware loader: always pass the pin (``None`` resolves
                    # the active set, matching the prior default behavior).
                    return self._load_dataset(eval_dataset_id, version)
                return self._load_dataset(eval_dataset_id)
            except Exception as exc:
                raise EvalProviderError(
                    f"failed to load eval dataset {eval_dataset_id!r}: {exc}"
                ) from exc
        # Default: look it up via the MLflow dataset registry. Operators
        # who use a custom registry / dataset format pass their own
        # ``load_dataset`` callable.
        try:
            import mlflow.genai  # noqa: PLC0415

            return mlflow.genai.datasets.get_dataset(eval_dataset_id)
        except Exception as exc:
            raise EvalProviderError(
                f"failed to load eval dataset {eval_dataset_id!r} via "
                f"mlflow.genai.datasets.get_dataset: {exc}. Pass a custom "
                "load_dataset callable to MLflowEvalProvider if you store "
                "eval datasets elsewhere."
            ) from exc

    def _resolve_scorers(
        self,
        scorer_names: list[str] | None = None,
        scorer_configs: dict[str, dict[str, object]] | None = None,
    ) -> list[Any]:
        explicit_names = [name for name in (scorer_names or []) if isinstance(name, str) and name]
        is_explicit = len(explicit_names) > 0

        if self._scorers is not None and not is_explicit:
            return self._scorers

        try:
            from mlflow.genai import scorers as mlflow_scorers  # noqa: PLC0415
        except ImportError as exc:
            raise EvalProviderError(
                "mlflow.genai.scorers is unavailable; cannot run default "
                "scorer suite. Pass an explicit ``scorers`` list to "
                "MLflowEvalProvider or upgrade mlflow."
            ) from exc

        scorer_configs = scorer_configs or {}

        # Explicit per-run scorer selection (the prompt optimization tab) wins.
        if is_explicit:
            resolved: list[Any] = []
            for name in explicit_names:
                raw_config = scorer_configs.get(name, {})
                if not isinstance(raw_config, dict):
                    raise EvalProviderError(f"scorer config for {name!r} must be an object/dict")

                # Custom CALIBER LLM judge: ``Judge.<name>`` is built via
                # ``mlflow.genai.make_judge`` from the definition the route
                # passes through ``scorer_configs`` (instructions/model/type).
                if name.startswith("Judge."):
                    resolved.append(self._build_judge_scorer(name, raw_config))
                    continue

                scorer_cls = self._resolve_scorer_class(mlflow_scorers, name)
                try:
                    scorer = scorer_cls(**raw_config) if raw_config else scorer_cls()
                except Exception as exc:
                    raise EvalProviderError(f"failed to initialize scorer {name!r}: {exc}") from exc
                resolved.append(scorer)

            if not resolved:
                raise EvalProviderError("no explicit scorers resolved")
            return resolved

        # The default CALIBER scorer suite — same names. Operators can override by passing
        # ``scorers=[...]`` at construction time.
        default_names = ("Correctness", "Guidelines", "RelevanceToQuery", "Safety")
        resolved = []
        for name in default_names:
            scorer_cls = getattr(mlflow_scorers, name, None)
            if scorer_cls is None:
                logger.warning("mlflow.genai.scorers.%s not found; skipping", name)
                continue
            try:
                resolved.append(scorer_cls())
            except Exception:
                # Some scorers require config (e.g. Guidelines wants a
                # ``guidelines=`` arg). Skip those silently; the operator
                # is expected to pass a configured list in that case.
                logger.warning(
                    "mlflow.genai.scorers.%s requires explicit configuration; skipping",
                    name,
                )
        if not resolved:
            raise EvalProviderError(
                "no scorers resolved from mlflow.genai.scorers; pass an "
                "explicit ``scorers`` list to MLflowEvalProvider."
            )
        return resolved

    def _build_judge_scorer(self, name: str, config: dict[str, Any]) -> Any:
        """Build a custom LLM judge scorer via the shared judge path.

        ``name`` is ``Judge.<judge-name>``; ``config`` carries the judge
        definition the route loads from CALIBER's ``caliber_judges`` table:
        ``instructions`` (required), and optional ``model`` /
        ``feedback_value_type``. Delegates to :func:`caliber.eval.judge_scorer.build_judge`
        so every judge — gate, scorecard, calibration — is built one way.
        """
        instructions = config.get("instructions")
        model = config.get("model")
        judge_name = name.partition(".")[2] or name
        try:
            return build_judge(
                judge_name,
                instructions if isinstance(instructions, str) else "",
                model=model if isinstance(model, str) else None,
                feedback_value_type=config.get("feedback_value_type"),
            )
        except JudgeError as exc:
            raise EvalProviderError(str(exc)) from exc

    def _resolve_scorer_class(self, mlflow_scorers: Any, scorer_name: str) -> Any:
        """Resolve scorer classes, including provider-prefixed names.

        Supported explicit format:

        * ``DeepEval.<MetricName>`` -> ``mlflow.genai.scorers.deepeval.<MetricName>``
        """
        provider, sep, metric_name = scorer_name.partition(".")
        if sep:
            if provider.lower() != "deepeval":
                raise EvalProviderError(
                    f"unknown scorer provider {provider!r} in {scorer_name!r}; "
                    "expected 'DeepEval.<MetricName>'"
                )

            deepeval_scorers = getattr(mlflow_scorers, "deepeval", None)
            if deepeval_scorers is None:
                try:
                    from mlflow.genai.scorers import deepeval as deepeval_scorers  # noqa: PLC0415
                except Exception as exc:
                    raise EvalProviderError(
                        f"scorer {scorer_name!r} requires deepeval support. "
                        "Install latest with 'pip install -U deepeval' and restart CALIBER."
                    ) from exc

            scorer_cls = getattr(deepeval_scorers, metric_name, None)
            if scorer_cls is None:
                raise EvalProviderError(
                    f"unknown scorer {scorer_name!r}; make sure "
                    f"mlflow.genai.scorers.deepeval.{metric_name} exists"
                )
            return scorer_cls

        scorer_cls = getattr(mlflow_scorers, scorer_name, None)
        if scorer_cls is None:
            raise EvalProviderError(
                f"unknown scorer {scorer_name!r}; make sure it exists under mlflow.genai.scorers"
            )
        return scorer_cls

    def _run_eval_pass(
        self,
        mlflow: Any,
        predict_fn: PredictFn,
        data: Any,
        scorers: list[Any],
        *,
        label: str,
        request: EvalRequest,
    ) -> ScoreSet:
        try:
            result = mlflow.genai.evaluate(
                data=data,
                predict_fn=predict_fn,
                scorers=scorers,
            )
        except Exception as exc:
            logger.exception(
                "mlflow.genai.evaluate (%s) failed for agent=%s job=%s",
                label,
                request.agent_id,
                request.job_id,
            )
            raise EvalProviderError(
                f"mlflow.genai.evaluate failed on {label} pass for "
                f"agent_id={request.agent_id!r}: {exc}"
            ) from exc
        return _metrics_to_score_set(getattr(result, "metrics", {}) or {})


# ---------------------------------------------------------------------------
# Helpers (module level so they're trivial to unit-test without the class)
# ---------------------------------------------------------------------------


def _metrics_to_score_set(metrics: dict[str, Any]) -> ScoreSet:
    """Fold an ``EvaluationResult.metrics`` dict into a :class:`ScoreSet`.

    MLflow's metrics dict typically uses ``{scorer_name}/mean`` and
    ``{scorer_name}/p90`` keys. We take per-scorer means as the dimension
    scores and average them for ``overall``. If the dict already has an
    ``overall`` key (some MLflow versions emit one), we honor it.
    """
    dimensions: dict[str, float] = {}
    explicit_overall: float | None = None

    for key, raw in metrics.items():
        score = _coerce_float(raw)
        if score is None:
            continue
        if key in {"overall", "overall/mean"}:
            explicit_overall = score
            continue
        # ``{scorer}/mean`` is the canonical per-scorer aggregate.
        if key.endswith("/mean"):
            dim = key[: -len("/mean")]
            dimensions[dim] = score
            continue
        # Fall back: if a key has no slash, treat it as a dimension name.
        if "/" not in key:
            dimensions[key] = score

    if explicit_overall is not None:
        overall = explicit_overall
    elif dimensions:
        overall = sum(dimensions.values()) / len(dimensions)
    else:
        # No usable metrics at all — surface as zero so the gate rejects
        # cleanly rather than crashing on a missing key.
        overall = 0.0

    return ScoreSet(overall=round(overall, 4), dimensions=dimensions)


def _loader_accepts_version(loader: Callable[..., Any]) -> bool:
    """True if ``loader`` can take a second positional ``version`` argument.

    Decides loader arity from its signature so we never have to distinguish an
    arity mismatch from an internal ``TypeError`` by catching the exception.
    Falls back to ``False`` (single-arg call) if the signature can't be read.
    """
    try:
        params = list(inspect.signature(loader).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [
        p
        for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    has_var_positional = any(p.kind is p.VAR_POSITIONAL for p in params)
    has_version_kw = any(p.name == "version" for p in params)
    return len(positional) > 1 or has_var_positional or has_version_kw


def _coerce_float(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` if it isn't numeric."""
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass; reject explicitly so a stray
        # passed/failed flag doesn't end up averaged into a dimension score.
        return None
    if isinstance(value, int | float):
        as_float = float(value)
        if not math.isfinite(as_float):
            # NaN/inf (e.g. a scorer that errored or emitted no numeric output
            # on every row) must not be averaged into a dimension score — drop
            # it so the empty→0.0 fail-closed branch applies and the regression
            # gate rejects the candidate cleanly instead of silently passing.
            return None
        return as_float
    return None


def _compute_deltas(candidate: ScoreSet, baseline: ScoreSet | None) -> dict[str, float]:
    if baseline is None:
        return {}
    deltas: dict[str, float] = {
        "overall": round(candidate.overall - baseline.overall, 4),
    }
    for dim, score in candidate.dimensions.items():
        baseline_score = baseline.dimensions.get(dim)
        if baseline_score is not None:
            deltas[dim] = round(score - baseline_score, 4)
    return deltas


def _count_examples(data: Any) -> int:
    """Best-effort row count of the eval dataset.

    Recorded on the comparison so the UI can render "N=120 examples" without
    a second backend call. We try ``len()`` first (works for lists, DataFrames,
    most MLflow ``EvaluationDataset`` builds); failing that, we count by
    iteration; failing *that*, we return 0. The downstream gate doesn't
    depend on this value being accurate, so silent fallback is correct.
    """
    try:
        return len(data)
    except TypeError:
        pass
    try:
        return sum(1 for _ in data)
    except TypeError:
        return 0
