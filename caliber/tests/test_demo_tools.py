"""Tests for built-in workflow/demo tools."""

from __future__ import annotations

import subprocess

import pytest

from caliber.workflows import demo_tools


def test_file_folder_and_grep_tools(tmp_path) -> None:
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.log"
    file_a.write_text("alpha\nrefund policy\n", encoding="utf-8")
    file_b.write_text("beta\n", encoding="utf-8")

    read = demo_tools.read_text_file(str(file_a))
    listed = demo_tools.list_folder_files(str(tmp_path), pattern="*.txt", recursive=False)
    grep = demo_tools.grep_files("refund", str(tmp_path), pattern="*.txt")

    assert read["text"].startswith("alpha")
    assert listed["count"] == 1
    assert listed["files"][0]["relative_path"] == "a.txt"
    assert grep["matches"][0]["line"] == 2


def test_regex_and_grok_tools() -> None:
    regex = demo_tools.regex_search(
        pattern=r"order-(?P<id>\d+)",
        text="created order-42",
    )
    grok = demo_tools.grok_parse(
        pattern="%{WORD:level} %{NUMBER:code} %{GREEDYDATA:message}",
        text="ERROR 500 backend unavailable",
    )

    assert regex["matches"][0]["groups"] == {"id": "42"}
    assert grok["matches"][0]["fields"] == {
        "level": "ERROR",
        "code": "500",
        "message": "backend unavailable",
    }


def test_sandbox_python_runs_bounded_snippet() -> None:
    # Generous timeout so a cold subprocess start doesn't flake under
    # full-suite load (see test_tool_sandbox_service for the same fix). The
    # dedicated timeout test below forces the timeout path deterministically.
    result = demo_tools.sandbox_python("print('sandbox ok')", timeout_seconds=30)

    assert result["timed_out"] is False
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "sandbox ok"


def test_search_tools_cover_limits_and_empty_inputs(tmp_path) -> None:
    file_a = tmp_path / "a.txt"
    file_a.write_text("Alpha\nalpha again\n", encoding="utf-8")

    assert demo_tools.grep_files("") == {"query": "", "matches": []}
    truncated_grep = demo_tools.grep_files(
        "alpha", str(file_a), case_sensitive=False, max_matches=1
    )
    assert truncated_grep["truncated"] is True

    assert demo_tools.regex_search(pattern="") == {"pattern": "", "matches": []}
    regex = demo_tools.regex_search(
        pattern=r"^alpha.*$",
        path=str(file_a),
        flags="ims",
        max_matches=1,
    )
    assert regex["truncated"] is True
    assert regex["matches"][0]["source"] == str(file_a)

    assert demo_tools.grok_parse("") == {"pattern": "", "matches": []}
    with pytest.raises(ValueError, match="unknown grok pattern"):
        demo_tools.grok_parse("%{NOPE:field}", "x")


def test_file_tool_error_and_truncation_paths(tmp_path) -> None:
    missing = tmp_path / "missing.txt"
    folder = tmp_path / "folder"
    folder.mkdir()
    text = folder / "long.txt"
    text.write_text("abcdef", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        demo_tools.read_text_file(str(missing))
    with pytest.raises(IsADirectoryError):
        demo_tools.read_text_file(str(folder))
    with pytest.raises(FileNotFoundError):
        demo_tools.list_folder_files(str(missing))

    truncated = demo_tools.read_text_file(str(text), max_bytes=3)
    assert truncated["text"] == "abc"
    assert truncated["metadata"]["truncated"] is True

    listed = demo_tools.list_folder_files(str(folder), pattern="**/*.txt", recursive=False)
    assert listed["count"] == 1
    assert listed["files"][0]["relative_path"] == "long.txt"


def test_sandbox_timeout_and_output_clipping(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("python", timeout=0.1, output=b"out", stderr=b"err")

    monkeypatch.setattr(demo_tools.subprocess, "run", fake_run)
    result = demo_tools.sandbox_python("while True: pass", timeout_seconds=0.1)

    assert result == {
        "timed_out": True,
        "returncode": None,
        "stdout": "out",
        "stderr": "err",
    }


def test_sandbox_python_does_not_silently_clamp_requested_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["timeout"] = kwargs["timeout"]

        class _Completed:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return _Completed()

    monkeypatch.setattr(demo_tools.subprocess, "run", fake_run)
    result = demo_tools.sandbox_python("print('ok')", timeout_seconds=30)

    assert result["timed_out"] is False
    assert seen["timeout"] == 30
    assert seen["args"] == [demo_tools.sys.executable, "-I", "-B", "-c", "print('ok')"]
