"""The paper's source identifier must survive a paper-only commit."""

from paper.scripts.gen_stats import _source_fingerprint


def test_source_fingerprint_tracks_authored_inputs_only(tmp_path):
    pkg = tmp_path / "package"
    tests = tmp_path / "tests"
    ui = tmp_path / "ui"
    pkg.mkdir()
    tests.mkdir()
    ui.mkdir()
    (pkg / "service.py").write_text("VALUE = 1\n")
    (tests / "test_service.py").write_text("assert True\n")
    (ui / "App.tsx").write_text("export const value = 1;\n")

    first = _source_fingerprint(pkg, tests, ui)
    assert len(first) == 16

    # Generated paper changes and build outputs cannot make make repro drift.
    (tmp_path / "paper.tex").write_text("paper-only edit\n")
    (ui / "dist").mkdir()
    (ui / "dist" / "bundle.ts").write_text("generated\n")
    assert _source_fingerprint(pkg, tests, ui) == first

    # A working-tree edit is identified even before it is committed.
    (ui / "App.tsx").write_text("export const value = 2;\n")
    assert _source_fingerprint(pkg, tests, ui) != first
