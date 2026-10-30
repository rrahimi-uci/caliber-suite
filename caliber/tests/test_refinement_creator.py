"""Creator provenance for every real refinement-job producer (B06.2, #739).

These tests go through the public route or assistant service that owns each
constructor and then inspect the persisted row.  Constructor inventory alone
would not prove that the authenticated identity survived the caller/storage
boundary.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import IntentExecuteRequest, IntentPlanRequest, SessionCreateRequest
from caliber.assistant.service import AssistantService
from caliber.auth import SCOPE_OPERATOR, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberVerificationItem,
)
from tests.workflow_helpers import (
    PREFIX,
    make_support_manifest,
    register_demo_tools,
    seed_eval_dataset,
)

PROMPT_PREFIX = f"{PREFIX}/prompts"
SKILLS_PREFIX = f"{PREFIX}/skills"
ACTOR = "@b062-requester"
FORGED_CREATOR = "@client-forged-creator"


def _operator_headers(client: TestClient, actor: str = ACTOR) -> dict[str, str]:
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": f"{actor},@test"}
    )
    return {"X-CALIBER-User": actor}


def test_prompt_creator(client: TestClient, db_session: Session) -> None:
    """The prompts route stores the authenticated requester on its job."""
    db_session.add(
        # The prompt route only needs an active dataset row to pin the launch.
        CaliberEvalDataset(
            dataset_id="creator-prompt-dataset",
            name="creator-prompt-dataset",
            description="creator provenance",
            owner=ACTOR,
            tags=[],
            status="active",
            version=1,
            visibility="user",
        )
    )
    db_session.commit()

    response = client.post(
        f"{PROMPT_PREFIX}/optimization/runs",
        headers=_operator_headers(client),
        json={
            "agent_id": "creator-prompt-agent",
            "eval_dataset_id": "creator-prompt-dataset",
            "optimizer_type": "GEPA",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
    )

    assert response.status_code == 201, response.text
    job_id = response.json()["data"]["job"]["job_id"]
    job = db_session.get(CaliberRefinementJob, job_id)
    assert job is not None
    assert job.triggered_by == ACTOR


def test_skill_creator(client: TestClient, db_session: Session) -> None:
    """The skill calibration route stores the authenticated requester."""
    headers = _operator_headers(client)
    created = client.post(
        SKILLS_PREFIX,
        headers=headers,
        json={
            "name": "creator-skill",
            "description": "creator provenance",
            "content": "Use the creator provenance test skill.",
            "owner": FORGED_CREATOR,
        },
    )
    assert created.status_code == 201, created.text
    skill_id = created.json()["data"]["skill_id"]

    response = client.post(f"{SKILLS_PREFIX}/{skill_id}/calibrate", headers=headers, json={})

    assert response.status_code == 201, response.text
    job_id = response.json()["data"]["job"]["job_id"]
    job = db_session.get(CaliberRefinementJob, job_id)
    assert job is not None
    assert job.triggered_by == ACTOR


def test_workflow_creator(client: TestClient, db_session: Session) -> None:
    """The workflow calibration route stores the authenticated requester."""
    headers = _operator_headers(client)
    db_session.add(
        CaliberAgentConfig(
            agent_id="creator-workflow-agent",
            experiment_id="creator-workflow-experiment",
            name="Creator workflow agent",
            owner=ACTOR,
            visibility="user",
            enabled=True,
        )
    )
    seed_eval_dataset(db_session)
    db_session.commit()
    register_demo_tools(client)

    workflow = client.post(
        f"{PREFIX}/workflows",
        headers=headers,
        json={"name": "Creator provenance workflow", "owner": FORGED_CREATOR},
    )
    assert workflow.status_code == 201, workflow.text
    workflow_id = workflow.json()["data"]["workflow_id"]
    draft = client.post(
        f"{PREFIX}/workflows/{workflow_id}/versions",
        headers=headers,
        json={
            "manifest": make_support_manifest(
                workflow_id,
                deploy_gates={
                    "support_eval_gate": {
                        "type": "deploy_gate",
                        "dataset_ref": "support_eval",
                        "required_for_aliases": ["prod"],
                        "thresholds": {"min_completion_rate": 1.0},
                    }
                },
            )
        },
    )
    assert draft.status_code == 201, draft.text
    version_id = draft.json()["data"]["version_id"]
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish", headers=headers)
    assert published.status_code == 200, published.text

    response = client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        headers=headers,
        json={"agent_id": "creator-workflow-agent"},
    )

    assert response.status_code == 201, response.text
    job_id = response.json()["data"]["job"]["job_id"]
    job = db_session.get(CaliberRefinementJob, job_id)
    assert job is not None
    assert job.triggered_by == ACTOR


def test_assistant_creator(session_factory: sessionmaker[Session]) -> None:
    """The assistant promotion service stores its resolved identity."""
    service = AssistantService(engine=FakeAssistantEngine())
    with session_factory() as db:
        db.add(
            CaliberAgentConfig(
                agent_id="creator-assistant-agent",
                experiment_id="creator-assistant-experiment",
                name="Creator assistant agent",
                owner=ACTOR,
                visibility="user",
                artifact_types=["prompt"],
                eval_thresholds={},
                optimizer_config={},
                approval_policy={},
            )
        )
        db.commit()

    identity = CaliberIdentity(
        user_id=ACTOR,
        scopes=frozenset({SCOPE_OPERATOR, SCOPE_VIEWER}),
    )
    session_id = service.create_session(
        SessionCreateRequest(title="creator provenance"),
        session_factory=session_factory,
        user=ACTOR,
        identity=identity,
    ).session_id
    plan = service.create_intent_plan(
        session_id,
        IntentPlanRequest(
            intent_name="propose_promotion",
            slot_overrides={
                "prompt_name": "creator-assistant-agent",
                "source_version": 7,
                "target_alias": "prod",
            },
        ),
        session_factory=session_factory,
        user=ACTOR,
    )
    executed = service.execute_intent_plan(
        session_id,
        IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
        session_factory=session_factory,
        user=ACTOR,
        identity=identity,
    )

    assert executed.result["result_type"] == "promotion_proposal"
    job_id = executed.result["ids"]["job_id"]
    with session_factory() as db:
        job = db.get(CaliberRefinementJob, job_id)
        assert job is not None
        assert job.triggered_by == ACTOR


def test_forged_body_creator_ignored(client: TestClient, db_session: Session) -> None:
    """A client creator/owner field cannot change the persisted requester."""
    headers = _operator_headers(client)
    response = client.post(
        SKILLS_PREFIX,
        headers=headers,
        json={
            "name": "forged-creator-skill",
            "description": "forged body regression",
            "content": "The server owns creator provenance.",
            "owner": FORGED_CREATOR,
        },
    )
    assert response.status_code == 201, response.text
    skill_id = response.json()["data"]["skill_id"]

    skill = db_session.get(CaliberSkill, skill_id)
    assert skill is not None
    assert skill.owner == ACTOR

    calibrated = client.post(f"{SKILLS_PREFIX}/{skill_id}/calibrate", headers=headers, json={})
    assert calibrated.status_code == 201, calibrated.text
    job_id = calibrated.json()["data"]["job"]["job_id"]
    job = db_session.get(CaliberRefinementJob, job_id)
    assert job is not None
    assert job.triggered_by == ACTOR
    assert job.triggered_by != FORGED_CREATOR


def test_legacy_unknown_preserved(db_session: Session) -> None:
    """Rows created before B06.2 retain explicit unknown provenance."""
    db_session.add(
        CaliberAgentConfig(
            agent_id="legacy-creator-agent",
            experiment_id="legacy-creator-experiment",
            name="Legacy creator agent",
            owner="@legacy",
        )
    )
    db_session.add(
        CaliberVerificationItem(
            item_id="legacy-creator-item",
            agent_id="legacy-creator-agent",
            category="legacy",
            free_text="legacy row",
            severity="standard",
            status="verified",
        )
    )
    db_session.add(
        CaliberRefinementJob(
            job_id="legacy-creator-job",
            agent_id="legacy-creator-agent",
            primary_item_id="legacy-creator-item",
            artifact_type="prompt",
            bundle_targets=[],
        )
    )
    db_session.commit()
    db_session.expire_all()

    job = db_session.get(CaliberRefinementJob, "legacy-creator-job")
    assert job is not None
    assert job.triggered_by is None
