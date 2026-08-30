"""Datasets, judges, and evaluations — the evidence and scoring surfaces."""

from __future__ import annotations

from typing import Any

from ..models._decode import decode, decode_list
from ..models.quality import EvalDataset, EvalExample, Evaluation, Judge, JudgeAlignment
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
        self, dataset_id: str, *, inputs: Any, expected: Any = None, **options: Any
    ) -> EvalExample:
        body: dict[str, Any] = {"inputs": inputs, **options}
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


__all__ = ["EvalDatasetsAPI", "EvaluationsAPI", "JudgesAPI"]
