"""Contracts for process-local MLflow isolation in the pytest harness."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CALIBER_INTEGRATION_TESTS") == "1",
    reason="real-integration runs manage their own MLflow store",
)


def _file_uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    assert parsed.scheme == "file"
    return Path(unquote(parsed.path))


def test_mlflow_store_and_artifact_root_share_a_unique_temp_root() -> None:
    tracking_uri = os.environ["MLFLOW_TRACKING_URI"]
    artifact_uri = os.environ["_MLFLOW_SERVER_ARTIFACT_ROOT"]
    assert tracking_uri.startswith("sqlite:///")

    db_path = Path(unquote(tracking_uri.removeprefix("sqlite:///")))
    artifact_root = _file_uri_path(artifact_uri)
    assert db_path.name == "mlflow.db"
    assert artifact_root.name == "artifacts"
    assert db_path.parent == artifact_root.parent
    assert db_path.parent.name.startswith("caliber-test-mlflow-")
    assert os.environ["MLFLOW_ENABLE_ASYNC_TRACE_LOGGING"] == "false"

    from mlflow.tracking import MlflowClient

    client = MlflowClient()
    experiment_id = client.create_experiment(f"harness-isolation-{uuid4().hex}")
    experiment = client.get_experiment(experiment_id)
    assert experiment is not None
    experiment_artifact_root = _file_uri_path(experiment.artifact_location)
    assert experiment_artifact_root.is_relative_to(artifact_root)
