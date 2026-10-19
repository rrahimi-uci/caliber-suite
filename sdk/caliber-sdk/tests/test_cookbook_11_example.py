"""Cookbook 11's runnable example, replayed offline against ``cookbook_11_fake_server``.

The fake copies the server's scoring, waiver and signoff rules, so these tests prove the script's
logic and the dataset's expectations agree with them; the live run in the lab README proves the
real server.
"""

from __future__ import annotations

from typing import Any

import pytest
from cookbook_11_fake_server import FakeReleaseServer

from examples.cookbooks import cookbook_11_release_signoff_factory as lab


def run_lab(server: FakeReleaseServer, **kwargs: Any) -> dict[str, Any]:
    admin = server.client("calpat_admin")
    operator = server.client("calpat_operator")
    viewer = server.client("calpat_viewer")
    with admin, operator, viewer:
        return lab.run(admin, operator=operator, viewer=viewer, suffix="-t", **kwargs)


def test_the_dataset_has_20_candidates_with_14_ready() -> None:
    rows = lab.load_candidates()
    assert len(rows) == 20
    assert sum(1 for row in rows if row["expect"]["status"] == "ready") == 14


def test_candidate_body_is_what_the_new_candidate_form_sends() -> None:
    row = lab.load_candidates()[0]
    body = lab.candidate_body(row, " x")
    assert body["name"] == f"{row['name']} x"
    assert body["planned_action"] == {"action": "promote_prompt", "target": "prod"}
    assert body["rollback_target"] == {"version_ref": row["rollback_version"], "target": "prod"}


def test_the_lab_runs_end_to_end_and_a_signed_go_deploys_nothing() -> None:
    server = FakeReleaseServer()
    summary = run_lab(server)
    assert summary["candidates"] == 20 and len(summary["ids"]) == 20
    candidates = server.candidates
    assert candidates[summary["ids"]["RC20"]]["status"] == "signed_go"
    assert candidates[summary["ids"]["RC19"]]["status"] == "signed_no_go"
    assert candidates[summary["ids"]["RC17"]]["status"] == "blocked"
    # the only promote on the timeline is the explicit one after the signoffs
    assert [e["action"] for e in server.timeline] == ["promote_prompt"]
    calls = server.calls
    first_signoff = next(i for i, c in enumerate(calls) if c.endswith("/signoffs"))
    promote = next(i for i, c in enumerate(calls) if "/aliases/" in c)
    assert first_signoff < promote
    assert len([c for c in calls if c == "POST /releases/candidates"]) >= 20


def test_the_script_fails_loudly_when_the_server_scores_differently() -> None:
    class Miscounting(FakeReleaseServer):
        def _score(self, candidate: dict[str, Any]) -> None:
            super()._score(candidate)
            candidate["weighted_score"] = round(candidate["weighted_score"] + 0.001, 6)

    with pytest.raises(AssertionError, match="RC01"):
        run_lab(Miscounting())


def test_the_script_fails_loudly_when_a_signed_candidate_can_still_be_waived() -> None:
    class Loose(FakeReleaseServer):
        def _signed(self, candidate: dict[str, Any]) -> bool:
            return False

    with pytest.raises(AssertionError):
        run_lab(Loose())


def test_get_candidate_unwraps_the_server_envelope() -> None:
    server = FakeReleaseServer()
    admin = server.client("calpat_admin")
    row = lab.load_candidates()[0]
    made = admin.releases.create_candidate(**lab.candidate_body(row, ""))
    got = admin.releases.get_candidate(made.candidate_id)
    assert got.candidate_id == made.candidate_id and got.name == row["name"]
    assert got.status == "ready" and got.weighted_score == 0.961
    assert got.extra["signoffs"] == [] and got.extra["report_jobs"] == []


def test_cleanup_deletes_the_prompt_and_says_candidates_stay() -> None:
    server = FakeReleaseServer()
    admin = server.client("calpat_admin")
    admin.prompts.create("payments-assistant-t", "x")
    lab.cleanup(admin, "-t")
    assert "payments-assistant-t" not in server.prompts
