"""B09.1 (#779): the Harborlight predicates judge what the real downstream recorded, not what it answered.

The observations here are read from the real downstream process (the EGI scenarios' disposable system of record), the way
a caller would read them after a dispatch, and handed to the pure predicates:

* a request committed against the version the proposal expected is accepted (and the downstream stamps it one version
  higher than the ``If-Match`` it was sent: the predicate's ``expected_version + 1`` is the measured behaviour, not a guess);
* a receipt names the committed request, so of two requests of one type each is judged against its own proposal's version;
* a request refused as stale (412) commits nothing, so nothing is attached at the version it expected;
* a retry that replays the stored answer (200) is still exactly one accepted request;
* the same acceptance of an HTTP 2xx whose recorded state is wrong (a downstream that stamped the pre-mutation version) fails
  the version criterion: success of the transport is not acceptance;
* judging makes no downstream call and changes no downstream state.
"""

from __future__ import annotations

from typing import Any

import httpx

from caliber.acceptance import HARBORLIGHT_V1, Observations, evaluate_all
from tests.egi_scenarios.conftest import Downstream

CASE = "MORT-2026-000184"
DOCUMENT_TYPE = "pay_statement"


def _post(downstream: Downstream, key: str, if_match: int | None) -> httpx.Response:
    headers = {"Idempotency-Key": key}
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    return downstream.client.post(
        f"/cases/{CASE}/document-requests", json={"document_type": DOCUMENT_TYPE}, headers=headers
    )


def _observe(
    downstream: Downstream, expected_version: int, request_id: str | None = None
) -> Observations:
    """What a caller reads back after a dispatch proposed against ``expected_version`` (and, once committed, its receipt)."""
    record = downstream.client.get(f"/cases/{CASE}")
    record.raise_for_status()
    observations: dict[str, Any] = {
        "proposal": {
            "case_id": CASE,
            "document_type": DOCUMENT_TYPE,
            "expected_version": expected_version,
        },
        "case_record": record.json(),
    }
    if request_id is not None:
        observations["receipt"] = {"downstream_request_id": request_id}
    return Observations(observations)


def _passed(observations: Observations) -> dict[str, bool]:
    return {r.predicate_id: r.passed for r in evaluate_all(HARBORLIGHT_V1, observations)}


def test_a_committed_request_is_accepted(downstream: Downstream) -> None:
    response = _post(downstream, "b09-accepted", if_match=1)
    assert response.status_code == 201
    assert response.json()["case_version"] == 2  # the version after the mutation
    request_id = response.json()["request_id"]
    observations = _observe(downstream, expected_version=1, request_id=request_id)
    assert _passed(observations) == {
        "document_request_exists": True,
        "attached_to_expected_version": True,
    }
    # the pointers locate the deciding request in the very record that was read
    results = evaluate_all(HARBORLIGHT_V1, observations)
    assert [r.evidence_ref for r in results] == [
        "/case_record/document_requests/0",
        "/case_record/document_requests/0/case_version",
    ]
    assert observations["case_record"]["document_requests"][0]["request_id"] == "DR-0001"


def test_a_receipt_pins_the_request_among_several_of_the_same_type(downstream: Downstream) -> None:
    first = _post(downstream, "b09-first", if_match=1)
    second = _post(downstream, "b09-second", if_match=2)
    assert (first.status_code, second.status_code) == (201, 201)
    assert (first.json()["case_version"], second.json()["case_version"]) == (2, 3)
    first_id, second_id = first.json()["request_id"], second.json()["request_id"]
    assert all(_passed(_observe(downstream, 1, first_id)).values())
    assert all(_passed(_observe(downstream, 2, second_id)).values())
    # each request judged against the other's proposal: it exists, but is attached to the wrong version
    expected = {"document_request_exists": True, "attached_to_expected_version": False}
    assert _passed(_observe(downstream, 1, second_id)) == expected
    assert _passed(_observe(downstream, 2, first_id)) == expected


def test_a_stale_precondition_commits_nothing_and_so_nothing_is_accepted(
    downstream: Downstream,
) -> None:
    first = _post(downstream, "b09-first", if_match=1)
    assert first.status_code == 201
    stale = _post(downstream, "b09-second", if_match=1)  # the case is already at version 2
    assert stale.status_code == 412
    assert downstream.mutations() == 1
    # the refused proposal has no receipt: the first proposal's request (version 2 = 1 + 1) must not accept it
    assert _passed(_observe(downstream, expected_version=1)) == {
        "document_request_exists": False,
        "attached_to_expected_version": False,
    }
    # a proposal that expected the current version (2) finds the first request, attached to the wrong version
    first_id = first.json()["request_id"]
    assert _passed(_observe(downstream, 2, first_id)) == {
        "document_request_exists": True,
        "attached_to_expected_version": False,
    }


def test_no_request_at_all_fails_both(downstream: Downstream) -> None:
    assert _post(downstream, "b09-none", if_match=7).status_code == 412
    assert _passed(_observe(downstream, expected_version=7)) == {
        "document_request_exists": False,
        "attached_to_expected_version": False,
    }


def test_a_replayed_answer_is_one_accepted_request(downstream: Downstream) -> None:
    created = _post(downstream, "b09-replay", if_match=1)
    assert created.status_code == 201
    replay = _post(downstream, "b09-replay", if_match=1)
    assert replay.status_code == 200
    assert replay.json() == created.json()
    observations = _observe(downstream, 1, replay.json()["request_id"])
    assert len(observations["case_record"]["document_requests"]) == 1
    assert all(_passed(observations).values())


def test_a_2xx_that_left_the_case_where_it_was_is_not_accepted(downstream: Downstream) -> None:
    """The transport said success; the case record says the version never advanced (the V11 false success)."""
    response = _post(downstream, "b09-false-success", if_match=1)
    assert response.status_code == 201
    observations = _observe(downstream, 1, response.json()["request_id"]).as_dict()
    observations["case_record"]["document_requests"][0]["case_version"] = 1
    verdicts = _passed(Observations(observations))
    assert verdicts == {"document_request_exists": True, "attached_to_expected_version": False}


def test_judging_calls_nothing_and_changes_nothing(downstream: Downstream) -> None:
    response = _post(downstream, "b09-readonly", if_match=1)
    assert response.status_code == 201
    observations = _observe(downstream, 1, response.json()["request_id"])
    before: dict[str, Any] = downstream.client.get(f"/cases/{CASE}").json()
    mutations = downstream.mutations()
    calls: list[httpx.Request] = []
    original = httpx.Client.send

    def spy(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        calls.append(request)
        return original(self, request, **kwargs)

    httpx.Client.send = spy  # type: ignore[method-assign]
    try:
        first = evaluate_all(HARBORLIGHT_V1, observations)
        second = evaluate_all(HARBORLIGHT_V1, observations)
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]
    assert first == second
    assert calls == []
    assert downstream.mutations() == mutations
    assert downstream.client.get(f"/cases/{CASE}").json() == before
