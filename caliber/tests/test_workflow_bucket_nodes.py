"""Tests for the object-storage / folder I/O workflow nodes.

Covers the manifest+IR wiring and the runtime executors for ``input_bucket``,
``output_bucket`` and ``output_folder``. Bucket I/O is exercised against a
``LocalStorageBackend`` injected via the ``_bucket_io`` seam, so these run
without a live MinIO/S3 endpoint.
"""

from __future__ import annotations

import pytest

from caliber.config import WorkflowStorageConfig
from caliber.storage.base import StorageUnavailableError
from caliber.storage.service import build_backend
from caliber.workflows import runtime as rt
from caliber.workflows.compiler import build_ir
from caliber.workflows.ir import IRInputBucket, IROutputBucket, IROutputFolder, NodeType
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimePlan,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver
from tests.workflow_helpers import fake_resolver, make_manifest


def _local_bucket_io(tmp_path):
    """Return a ``_bucket_io`` replacement backed by a local temp directory."""
    backend = build_backend(WorkflowStorageConfig(backend="local", base_uri=f"file://{tmp_path}"))

    def _io(bucket: str, prefix: str):
        key_prefix = "/".join(p for p in [bucket.strip("/"), (prefix or "").strip("/")] if p)
        return backend, key_prefix

    return _io


# ---------------------------------------------------------------------------
# Manifest + IR wiring
# ---------------------------------------------------------------------------


def test_bucket_and_folder_nodes_compile_to_ir() -> None:
    data = make_manifest()
    data["nodes"]["ib"] = {
        "id": "ib",
        "type": "input_bucket",
        "bucket": "docs",
        "prefix": "in/",
        "recursive": True,
        "max_files": 10,
        "inputs": {"prefix": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["nodes"]["ob"] = {
        "id": "ob",
        "type": "output_bucket",
        "bucket": "out",
        "prefix": "r/",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"keys": {"type": "structured"}, "metadata": {"type": "structured"}},
    }
    data["nodes"]["of"] = {
        "id": "of",
        "type": "output_folder",
        "path": "/tmp/exports",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"files": {"type": "structured"}, "metadata": {"type": "structured"}},
    }
    manifest = parse_manifest(data)
    ir = build_ir(manifest, fake_resolver(), version="v")

    assert isinstance(ir.nodes["ib"], IRInputBucket)
    assert ir.nodes["ib"].bucket == "docs"
    assert ir.nodes["ib"].node_type == NodeType.INPUT_BUCKET
    assert isinstance(ir.nodes["ob"], IROutputBucket)
    assert ir.nodes["ob"].prefix == "r/"
    assert isinstance(ir.nodes["of"], IROutputFolder)
    assert ir.nodes["of"].path == "/tmp/exports"


# ---------------------------------------------------------------------------
# Runtime: output_folder
# ---------------------------------------------------------------------------


def test_output_folder_writes_artifacts_to_disk(tmp_path) -> None:
    out_dir = tmp_path / "exports"
    data = make_manifest()
    data["nodes"]["of"] = {
        "id": "of",
        "type": "output_folder",
        "path": str(out_dir),
        "inputs": {"input": {"type": "string"}},
        "outputs": {"files": {"type": "structured"}, "metadata": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
        {"id": "e3", "from": "agent", "to": "of", "map": {"final_output": "input"}},
    ]
    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    written = list(out_dir.glob("*"))
    assert [p.name for p in written] == ["output.txt"]
    assert (out_dir / "output.txt").read_text(encoding="utf-8")


def test_output_folder_node_reports_missing_path_in_runtime() -> None:
    data = make_manifest()
    data["nodes"]["of"] = {
        "id": "of",
        "type": "output_folder",
        "path": "",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"files": {"type": "structured"}, "metadata": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
        {"id": "e3", "from": "agent", "to": "of", "map": {"final_output": "input"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ValueError: output_folder node requires a folder path" in result.error


def test_input_bucket_node_executes_in_runtime_workflow(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rt, "_bucket_io", _local_bucket_io(tmp_path))
    rt._write_output_bucket_node(
        bucket="docs",
        prefix="run1/",
        overwrite=True,
        port_values={("seed", "result"): {"artifacts": {"a.txt": "hello", "docs/b.md": "# B"}}},
        direct_input=None,
    )

    data = make_manifest()
    data["nodes"]["input_bucket"] = {
        "id": "input_bucket",
        "type": "input_bucket",
        "bucket": "docs",
        "prefix": "",
        "recursive": True,
        "max_files": 10,
        "inputs": {"prefix": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_bucket", "from": "start", "to": "input_bucket", "map": {"msg": "prefix"}},
        {"id": "e_bucket_agent", "from": "input_bucket", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    result = execute(_plan(data), "run1/", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    bucket_step = next(step for step in result.steps if step.node_id == "input_bucket")
    assert bucket_step.output_by_port is not None
    assert bucket_step.output_by_port["metadata"]["object_count"] == 2
    assert "hello" in bucket_step.output
    assert "# B" in bucket_step.output
    assert "read 2 object(s) from docs/run1/" in bucket_step.detail
    assert result.output


def test_output_folder_helper_sanitizes_traversal(tmp_path) -> None:
    written, metadata = rt._write_output_folder_node(
        path=str(tmp_path),
        overwrite=True,
        port_values={("n", "result"): {"artifacts": {"../escape.txt": "x", "ok.txt": "y"}}},
        direct_input=None,
    )
    # The traversal segment is stripped, keeping the file inside the target dir.
    assert metadata["file_count"] == 2
    assert (tmp_path / "escape.txt").exists()
    assert (tmp_path / "ok.txt").exists()
    assert not (tmp_path.parent / "escape.txt").exists()
    assert all(str(tmp_path) in w for w in written)


# ---------------------------------------------------------------------------
# Runtime: input_bucket / output_bucket (local backend via _bucket_io seam)
# ---------------------------------------------------------------------------


def test_output_then_input_bucket_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rt, "_bucket_io", _local_bucket_io(tmp_path))

    keys, meta = rt._write_output_bucket_node(
        bucket="mybkt",
        prefix="run1/",
        overwrite=True,
        port_values={("n", "result"): {"artifacts": {"a.txt": "hello", "docs/b.md": "# B"}}},
        direct_input=None,
    )
    assert meta["object_count"] == 2
    assert keys == ["mybkt/run1/a.txt", "mybkt/run1/docs/b.md"]

    text, files, rmeta = rt._read_input_bucket_node(
        bucket="mybkt",
        prefix="run1/",
        recursive=True,
        max_files=50,
        max_bytes_per_file=1000,
        encoding="utf-8",
    )
    assert rmeta["object_count"] == 2
    assert sorted(f["relative_path"] for f in files) == ["a.txt", "docs/b.md"]
    assert "hello" in text and "# B" in text


def test_output_bucket_node_executes_in_runtime_workflow(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rt, "_bucket_io", _local_bucket_io(tmp_path))

    data = make_manifest()
    data["nodes"]["ob"] = {
        "id": "ob",
        "type": "output_bucket",
        "bucket": "results",
        "prefix": "run1/",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"keys": {"type": "structured"}, "metadata": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
        {"id": "e3", "from": "agent", "to": "ob", "map": {"final_output": "input"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    output_step = next(step for step in result.steps if step.node_id == "ob")
    assert output_step.output_by_port is not None
    assert output_step.output_by_port["metadata"]["object_count"] == 1
    assert output_step.output == '["results/run1/output.txt"]'
    assert "wrote 1 object(s) to results/run1/" in output_step.detail

    text, files, metadata = rt._read_input_bucket_node(
        bucket="results",
        prefix="run1/",
        recursive=True,
        max_files=10,
        max_bytes_per_file=10_000,
        encoding="utf-8",
    )
    assert metadata["object_count"] == 1
    assert [f["relative_path"] for f in files] == ["output.txt"]
    assert result.output in text


def test_output_bucket_node_surfaces_storage_write_failure_in_runtime(
    monkeypatch,
) -> None:
    class _FailingBackend:
        def write_bytes(self, key, data, *, media_type=None, overwrite=True):
            del data, media_type, overwrite
            raise StorageUnavailableError(f"backend unavailable for {key}")

    monkeypatch.setattr(
        rt,
        "_bucket_io",
        lambda bucket, prefix: (_FailingBackend(), f"{bucket}/{prefix}".strip("/")),
    )

    data = make_manifest()
    data["nodes"]["ob"] = {
        "id": "ob",
        "type": "output_bucket",
        "bucket": "results",
        "prefix": "run1/",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"keys": {"type": "structured"}, "metadata": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
        {"id": "e3", "from": "agent", "to": "ob", "map": {"final_output": "input"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: output_bucket write failed" in result.error
    assert "results/run1/output.txt" in result.error


def test_output_bucket_node_reports_partial_write_progress_in_runtime(
    tmp_path, monkeypatch
) -> None:
    bucket_io = _local_bucket_io(tmp_path)
    monkeypatch.setattr(rt, "_bucket_io", bucket_io)
    backend, _key_prefix = bucket_io("results", "")
    original_write_bytes = backend.write_bytes
    write_calls: list[str] = []

    def _write_bytes_then_fail(key: str, data: bytes, *, media_type=None, overwrite=True):
        write_calls.append(key)
        if len(write_calls) > 1:
            raise StorageUnavailableError(f"backend unavailable for {key}")
        return original_write_bytes(
            key,
            data,
            media_type=media_type,
            overwrite=overwrite,
        )

    monkeypatch.setattr(backend, "write_bytes", _write_bytes_then_fail)

    data = make_manifest()
    data["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "emit_artifacts": {
            "id": "emit_artifacts",
            "type": "python_code",
            "code": (
                'return {"text": "artifact-ready", '
                '"result": {"artifacts": {"a.txt": "hello", "b.txt": "world"}}}'
            ),
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "result": {"type": "structured"},
            },
        },
        "ob": {
            "id": "ob",
            "type": "output_bucket",
            "bucket": "results",
            "prefix": "run1/",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"keys": {"type": "structured"}, "metadata": {"type": "structured"}},
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    data["edges"] = [
        {"id": "e_start_emit", "from": "start", "to": "emit_artifacts", "map": {"msg": "input"}},
        {
            "id": "e_emit_final",
            "from": "emit_artifacts",
            "to": "final",
            "map": {"text": "response"},
        },
        {"id": "e_emit_bucket", "from": "emit_artifacts", "to": "ob", "map": {"text": "input"}},
    ]

    result = execute(_plan(data), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: output_bucket write failed" in result.error
    assert "results/run1/b.txt" in result.error
    assert "after writing 1 object(s)" in result.error
    assert "results/run1/a.txt" in result.error
    assert (tmp_path / "results" / "run1" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_input_bucket_missing_prefix_reads_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rt, "_bucket_io", _local_bucket_io(tmp_path))
    text, files, metadata = rt._read_input_bucket_node(
        bucket="empty",
        prefix="nope/",
        recursive=True,
        max_files=50,
        max_bytes_per_file=1000,
        encoding="utf-8",
    )
    assert text == ""
    assert files == []
    assert metadata["object_count"] == 0


def test_input_bucket_node_surfaces_storage_list_failure_in_runtime(
    monkeypatch,
) -> None:
    class _FailingBackend:
        def list(self, prefix, *, recursive=True, limit=1000, cursor=None):
            del recursive, limit, cursor
            raise StorageUnavailableError(f"backend unavailable for {prefix}")

    monkeypatch.setattr(
        rt,
        "_bucket_io",
        lambda bucket, prefix: (_FailingBackend(), f"{bucket}/{prefix}".strip("/")),
    )

    data = make_manifest()
    data["nodes"]["input_bucket"] = {
        "id": "input_bucket",
        "type": "input_bucket",
        "bucket": "docs",
        "prefix": "",
        "recursive": True,
        "max_files": 10,
        "inputs": {"prefix": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_bucket", "from": "start", "to": "input_bucket", "map": {"msg": "prefix"}},
        {"id": "e_bucket_agent", "from": "input_bucket", "to": "agent", "map": {"text": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    result = execute(_plan(data), "run1/", executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert result.error is not None
    assert "ToolExecutionError: input_bucket list failed" in result.error
    assert "docs/run1/" in result.error


def test_input_bucket_unreadable_objects_do_not_look_truncated(tmp_path, monkeypatch) -> None:
    bucket_io = _local_bucket_io(tmp_path)
    monkeypatch.setattr(rt, "_bucket_io", bucket_io)
    backend, _key_prefix = bucket_io("docs", "")
    backend.write_bytes(
        "docs/run1/a.txt",
        b"hello",
        media_type="text/plain",
        overwrite=True,
    )
    backend.write_bytes(
        "docs/run1/b.txt",
        b"blocked",
        media_type="text/plain",
        overwrite=True,
    )
    original_read_bytes = backend.read_bytes

    def _read_bytes_with_partial_failure(key: str) -> bytes:
        if key.endswith("/b.txt"):
            raise StorageUnavailableError(f"backend unavailable for {key}")
        return original_read_bytes(key)

    monkeypatch.setattr(backend, "read_bytes", _read_bytes_with_partial_failure)

    text, files, metadata = rt._read_input_bucket_node(
        bucket="docs",
        prefix="run1/",
        recursive=True,
        max_files=10,
        max_bytes_per_file=1000,
        encoding="utf-8",
    )

    assert text == "--- a.txt ---\nhello"
    assert [file["relative_path"] for file in files] == ["a.txt"]
    assert metadata["object_count"] == 1
    assert metadata["skipped_object_count"] == 1
    assert metadata["truncated_file_list"] is False


def test_input_bucket_marks_truncated_file_list_when_backend_has_more_objects(
    tmp_path, monkeypatch
) -> None:
    bucket_io = _local_bucket_io(tmp_path)
    monkeypatch.setattr(rt, "_bucket_io", bucket_io)
    backend, _key_prefix = bucket_io("docs", "")
    backend.write_bytes(
        "docs/run1/a.txt",
        b"hello",
        media_type="text/plain",
        overwrite=True,
    )
    backend.write_bytes(
        "docs/run1/b.txt",
        b"world",
        media_type="text/plain",
        overwrite=True,
    )

    text, files, metadata = rt._read_input_bucket_node(
        bucket="docs",
        prefix="run1/",
        recursive=True,
        max_files=1,
        max_bytes_per_file=1000,
        encoding="utf-8",
    )

    assert text == "--- a.txt ---\nhello"
    assert [file["relative_path"] for file in files] == ["a.txt"]
    assert metadata["object_count"] == 1
    assert metadata["skipped_object_count"] == 0
    assert metadata["truncated_file_list"] is True


def test_output_bucket_falls_back_to_direct_input(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rt, "_bucket_io", _local_bucket_io(tmp_path))
    keys, meta = rt._write_output_bucket_node(
        bucket="b",
        prefix="",
        overwrite=True,
        port_values={},
        direct_input="just text",
    )
    assert keys == ["b/output.txt"]
    assert meta["object_count"] == 1


def test_bucket_nodes_require_bucket_name() -> None:
    with pytest.raises(ValueError, match="input_bucket node requires a bucket name"):
        rt._read_input_bucket_node(
            bucket="",
            prefix="",
            recursive=True,
            max_files=1,
            max_bytes_per_file=1,
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="output_bucket node requires a bucket name"):
        rt._write_output_bucket_node(
            bucket="", prefix="", overwrite=True, port_values={}, direct_input="x"
        )


def _plan(manifest_dict, **plan_kwargs) -> RuntimePlan:
    manifest = parse_manifest(manifest_dict)
    resolver = InMemoryToolResolver([])
    ir = build_ir(manifest, resolver, version="test")
    return RuntimePlan(ir=ir, resolver=resolver, **plan_kwargs)
