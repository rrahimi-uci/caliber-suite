"""Datasets, judges, evaluations, and verification — the evidence and scoring
surfaces."""

from __future__ import annotations

from typing import Any

from ..models._decode import decode, decode_list
from ..models.quality import (
    EvalDataset,
    EvalExample,
    Evaluation,
    Judge,
    JudgeAlignment,
    VerificationBatchResult,
    VerificationItem,
)
from ..waiters import wait_for
from ._base import Resource

_List = list


class EvalDatasetsAPI(Resource):
    """Versioned evaluation datasets and their examples."""

    def list(self, *, status: str | None = None) -> _List[EvalDataset]:
        params = {"status": status} if status else None
        return decode_list(EvalDataset, self._get("/eval-datasets", params=params))

    def get(self, dataset_id: str) -> EvalDataset:
        return decode(EvalDataset, self._get(f"/eval-datasets/{dataset_id}"))

    def create(
        self, name: str, *, owner: str, description: str | None = None, **options: Any
    ) -> EvalDataset:
        """Create a dataset.

        ``owner`` is required by the server and kept keyword-required here for
        the same reason as skills: ownership is a governance field, not
        something to infer from whichever credential ran the script.
        """
        body: dict[str, Any] = {"name": name, "owner": owner, **options}
        if description is not None:
            body["description"] = description
        return decode(EvalDataset, self._post("/eval-datasets", json=body))

    def add_example(
        self, dataset_id: str, *, input: Any, expected: Any = None, **options: Any
    ) -> EvalExample:
        """Append one labeled example row.

        The parameter is ``input`` (singular) because that is the server's
        field name (``EvalExampleCreateRequest.input``); the request schema
        forbids extra fields, so an ``inputs=`` (plural) call used to 422
        against a real server despite matching every mocked test.
        """
        body: dict[str, Any] = {"input": input, **options}
        if expected is not None:
            body["expected"] = expected
        return decode(EvalExample, self._post(f"/eval-datasets/{dataset_id}/examples", json=body))

    def examples(self, dataset_id: str) -> _List[EvalExample]:
        return decode_list(EvalExample, self._get(f"/eval-datasets/{dataset_id}/examples"))

    def add_from_trace(self, dataset_id: str, trace_id: str, **options: Any) -> EvalExample:
        """Capture a production trace as a dataset row.

        The path that turns an observed failure into evidence, which is where
        the refinement loop starts.
        """
        return decode(
            EvalExample,
            self._post(
                f"/eval-datasets/{dataset_id}/examples/from-trace",
                json={"trace_id": trace_id, **options},
            ),
        )

    def update(self, dataset_id: str, **changes: Any) -> EvalDataset:
        return decode(EvalDataset, self._patch(f"/eval-datasets/{dataset_id}", json=changes))

    def revise_example(
        self,
        dataset_id: str,
        example_id: str,
        *,
        input: dict[str, Any],
        expected: dict[str, Any],
        **params: Any,
    ) -> EvalExample:
        """Supersede the old row and append a replacement atomically --
        append-only, so history stays reproducible. ``params`` may carry
        ``weight``, ``tags``. Returns the new (replacement) example."""
        return decode(
            EvalExample,
            self._post(
                f"/eval-datasets/{dataset_id}/examples/{example_id}/revise",
                json={"input": input, "expected": expected, **params},
            ),
        )

    def supersede_example(self, dataset_id: str, example_id: str) -> EvalExample:
        """Retire an example without replacing it. Idempotent -- superseding
        an already-superseded row just returns it unchanged."""
        return decode(
            EvalExample,
            self._post(f"/eval-datasets/{dataset_id}/examples/{example_id}/supersede"),
        )

    def restore(self, dataset_id: str, *, version: int) -> Any:
        """Restore a prior version's example set as a new head version
        (forward-only; history is preserved, not rewritten)."""
        return self._post(f"/eval-datasets/{dataset_id}/restore", json={"version": version})

    def sync(self, dataset_id: str, **options: Any) -> Any:
        """Push the dataset's current example set to MLflow's GenAI dataset
        registry. CALIBER stays the source of truth; this is a one-way
        push, not a bidirectional sync."""
        return self._post(f"/eval-datasets/{dataset_id}/sync", json=options)


class JudgesAPI(Resource):
    """Model-backed graders and their human alignment."""

    def list(self) -> _List[Judge]:
        return decode_list(Judge, self._get("/judges"))

    def get(self, judge_id: str) -> Judge:
        return decode(Judge, self._get(f"/judges/{judge_id}"))

    def create(
        self,
        name: str,
        *,
        instructions: str,
        feedback_value_type: str = "bool",
        model: str | None = None,
        **options: Any,
    ) -> Judge:
        """Create a model-backed grader.

        ``instructions`` must reference at least one evaluation variable —
        ``{{ inputs }}``, ``{{ outputs }}``, ``{{ expectations }}``,
        ``{{ conversation }}``, or ``{{ trace }}`` — or the server rejects it.
        The rule exists because a judge with no variable grades nothing: it
        would return the same verdict for every example.

        ``feedback_value_type`` defaults to ``bool``. A numeric judge is not
        interchangeable with a boolean one downstream, so scorecards read this
        field to know which they have.
        """
        body: dict[str, Any] = {
            "name": name,
            "instructions": instructions,
            "feedback_value_type": feedback_value_type,
            **options,
        }
        if model is not None:
            body["model"] = model
        return decode(Judge, self._post("/judges", json=body))

    def update(self, judge_id: str, **changes: Any) -> Judge:
        return decode(Judge, self._patch(f"/judges/{judge_id}", json=changes))

    def test(self, judge_id: str, **payload: Any) -> Any:
        """Run a judge against sample input (``inputs=``, ``outputs=``,
        ``expectations=``) without recording a scorecard. Returns the raw
        ``{"score", "value", "rationale"}`` — untyped because the judge's
        ``value`` is author-defined (bool, number, or string; see
        ``feedback_value_type``), so a fixed model here would either narrow
        that or duplicate the union for no benefit over reading the dict.
        """
        return self._post(f"/judges/{judge_id}/test-run", json=payload)

    def alignment(self, judge_id: str, **payload: Any) -> JudgeAlignment:
        """Agreement with human labels.

        Read ``kappa``, not ``agreement``: a judge that always answers the same
        way agrees with a skewed sample while measuring nothing.
        """
        return decode(JudgeAlignment, self._post(f"/judges/{judge_id}/alignment", json=payload))


class EvaluationsAPI(Resource):
    """Scored runs over datasets."""

    def list(self, *, dataset_id: str | None = None) -> _List[Evaluation]:
        params = {"dataset_id": dataset_id} if dataset_id else None
        return decode_list(Evaluation, self._get("/evaluations", params=params))

    def get(self, evaluation_id: str) -> Evaluation:
        return decode(Evaluation, self._get(f"/evaluations/{evaluation_id}"))

    def create(self, dataset_id: str, **options: Any) -> Evaluation:
        return decode(
            Evaluation, self._post("/evaluations", json={"dataset_id": dataset_id, **options})
        )

    def wait(self, evaluation_id: str, *, timeout: float = 900.0, **options: Any) -> Evaluation:
        """Poll until the evaluation stops.

        Returns the terminal evaluation rather than raising: a low score is the
        measurement, not an error in the call.
        """
        return wait_for(
            lambda: self.get(evaluation_id),
            is_done=lambda item: item.is_terminal,
            timeout=timeout,
            **options,
        )


class VerificationQueueAPI(Resource):
    """Stage ① Verify — manually-flagged concerns awaiting confirmation.

    Verifying or dismissing an item here does **not** create a refinement
    job. Building that requires generalizing three separate job-creation
    paths (prompt/skill/workflow) behind a shared interface, which is
    adapter-shaped work for a later phase, not this resource. See
    ``docs/workspace-plan.md`` section 2.2 and
    ``caliber/src/caliber/routes/verification.py``'s module docstring.
    """

    def list(
        self,
        *,
        status: str | None = "pending",
        severity: str | None = None,
        agent_id: str | None = None,
    ) -> _List[VerificationItem]:
        params = {
            key: value
            for key, value in {"status": status, "severity": severity, "agent_id": agent_id}.items()
            if value is not None
        }
        return decode_list(VerificationItem, self._get("/verification-queue", params=params))

    def get(self, item_id: str) -> VerificationItem:
        return decode(VerificationItem, self._get(f"/verification-queue/{item_id}"))

    def create(
        self, agent_id: str, *, category: str, free_text: str, **options: Any
    ) -> VerificationItem:
        """Manually flag a concern that isn't tied to an already-running job."""
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "category": category,
            "free_text": free_text,
            **options,
        }
        return decode(VerificationItem, self._post("/verification-queue", json=body))

    def verify(self, item_id: str, **options: Any) -> VerificationItem:
        """Confirm the flagged concern is real.

        Returns the updated item. The server's response also carries a
        ``job`` key, which is always ``None`` today — see the class
        docstring.
        """
        response = self._post(f"/verification-queue/{item_id}/verify", json=options)
        item = response.get("item", response) if isinstance(response, dict) else response
        return decode(VerificationItem, item)

    def dismiss(self, item_id: str, **options: Any) -> VerificationItem:
        """Mark the flagged concern as not real (or, with ``duplicate_of_id``,
        as a duplicate of another item)."""
        return decode(
            VerificationItem, self._post(f"/verification-queue/{item_id}/dismiss", json=options)
        )

    def mark_duplicate(
        self, item_id: str, duplicate_of_id: str, **options: Any
    ) -> VerificationItem:
        """Dedicated route for "this is a duplicate of X" — same mutation as
        :meth:`dismiss` with ``duplicate_of_id`` set, but a distinct call
        makes the intent unambiguous in audit logs."""
        body = {"duplicate_of_id": duplicate_of_id, **options}
        return decode(
            VerificationItem, self._post(f"/verification-queue/{item_id}/duplicate", json=body)
        )

    def batch(self, action: str, item_ids: _List[str], **options: Any) -> VerificationBatchResult:
        """Verify or dismiss several items in one round-trip.

        Per-item failures don't fail the whole batch — inspect
        ``result.results`` for which items succeeded.
        """
        body = {"action": action, "item_ids": item_ids, **options}
        return decode(VerificationBatchResult, self._post("/verification-queue/batch", json=body))


__all__ = ["EvalDatasetsAPI", "EvaluationsAPI", "JudgesAPI", "VerificationQueueAPI"]
