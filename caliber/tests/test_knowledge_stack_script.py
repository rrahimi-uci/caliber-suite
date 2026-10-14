"""The knowledge-base stack journey is wired into CI and cannot run by accident (alpha gap P1-23).

The journey itself needs a real MinIO and PostgreSQL, so CI is where it runs. These offline tests
keep the pieces around it honest: the script parses and runs as its own compose project, the
workflow has a job that runs it, the journey skips without the stack, and the markers that say
its embeddings are simulated are still there.

They read repository files, so they skip when the tests run from an sdist without them.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify-knowledge-stack.sh"
WORKFLOW = REPO / ".github" / "workflows" / "stack-smoke.yml"
JOURNEY = REPO / "caliber" / "tests" / "test_knowledge_stack_journey.py"

pytestmark = pytest.mark.skipif(
    not (SCRIPT.exists() and WORKFLOW.exists() and JOURNEY.exists()),
    reason="repository scripts and workflows not present",
)


def test_the_script_is_executable_and_parses() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash not available")
    result = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_the_script_runs_as_its_own_project_and_refuses_to_share_a_stack() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'PROJECT="caliber-kb-smoke"' in text
    assert 'docker compose -p "$PROJECT"' in text
    assert "another CALIBER stack owns these containers" in text
    assert text.index("another CALIBER stack owns") < text.index("up -d --build postgres minio")
    assert "COMPOSE_DISABLE_ENV_FILE=1" in text  # never reads a developer's own .env
    assert "CALIBER_KB_STACK_S3_ENDPOINT" in text
    assert "CALIBER_KB_STACK_POSTGRES_URL" in text


def test_the_workflow_runs_the_script_in_its_own_job_and_on_the_files_that_matter() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("\n  knowledge:\n", 1)[1]
    assert "scripts/verify-knowledge-stack.sh" in job
    assert "knowledge" in job.split("run:", 1)[0] or ".[dev,s3,postgres,knowledge]" in job
    for path in (
        "scripts/verify-knowledge-stack.sh",
        "caliber/tests/test_knowledge_stack_journey.py",
        "caliber/src/caliber/knowledge/**",
    ):
        assert f'- "{path}"' in workflow, path


def test_the_journey_skips_without_the_stack_and_says_its_embeddings_are_simulated() -> None:
    text = JOURNEY.read_text(encoding="utf-8")
    assert "CALIBER_KB_STACK_S3_ENDPOINT" in text
    assert "CALIBER_KB_STACK_POSTGRES_URL" in text
    assert "pytest.mark.skipif" in text
    assert "The embeddings are simulated" in text
    assert "_SimulatedEmbedder" in text
    assert "simulated_embeddings" in text  # the test's own name carries the caveat


def test_the_knowledge_page_states_what_is_and_is_not_verified() -> None:
    page = " ".join(
        (REPO / "docs" / "use" / "knowledge-bases.md").read_text(encoding="utf-8").split()
    )
    assert "Before the first step: what a knowledge base needs" in page
    assert "knowledge-local" in page
    assert "Its embeddings are simulated" in page or "embeddings are simulated" in page
    assert "scripts/verify-knowledge-stack.sh" in page
