"""RuleBundle v1: the raw bytes are verified before anything is parsed, and parsing is strict (#726).

Pure: no database, no provider, no network, no run. The fixture is scripted test content, not domain acceptance.
"""

from __future__ import annotations

import ast
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from caliber.context import rule_bundle
from caliber.context import rules as context_rules
from caliber.context.manifest import ContextManifestError, ContextSegment, build_context_manifest
from caliber.context.rule_bundle import (
    BAD_EXPECTED_DIGEST,
    CODES,
    DIGEST_MISMATCH,
    DUPLICATE_RULE_ID,
    INVALID_YAML,
    MALFORMED,
    MAX_BUNDLE_BYTES,
    MAX_CITATION_BYTES,
    MISSING_BYTES,
    TOO_LARGE,
    UNKNOWN_FIELD,
    UNKNOWN_RULE,
    UNSUPPORTED_VERSION,
    Rule,
    RuleBundleError,
    load_rule_bundle,
    parse_rule_bundle,
    sha256_of,
)
from caliber.context.rules import (
    CACHE_SIZE,
    RULE_BUNDLE_CHANGED,
    RULE_BUNDLE_MISMATCH,
    RULE_BUNDLE_UNAVAILABLE,
    RULE_BUNDLE_UNPINNED,
    clear_rule_bundle_cache,
    rule_bundle_record,
    rule_bundle_reference,
)
from caliber.policy.principal import Subject, Workload
from caliber.run_manifest import (
    DeploymentScope,
    ManifestError,
    ResolvedPins,
    build_run_manifest,
    canonical_bytes,
    pin_rule_bundle,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rule_bundles" / "support_rules.v1.yaml"
#: Pinned: the fixture's bytes must hash to this, so the hash is deterministic and a changed fixture is noticed.
FIXTURE_SHA256 = "c63babbe0b046624d41b284ae159629d787b2dea721a68b4237e17934c31d533"

GOOD = """\
schema_version: v1
bundle_id: b1
version: "1"
rules:
  - id: r1
    text: "Say hello."
    source_ref: "src#1"
    applies_to: [greeting]
"""


def _write(tmp_path: Path, text: str | bytes, name: str = "bundle.yaml") -> tuple[Path, str]:
    path = tmp_path / name
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


@contextmanager
def _refused(code: str) -> Iterator[Any]:
    """Expect a ``RuleBundleError`` with exactly this ``code`` (and nothing else escaping)."""
    with pytest.raises(RuleBundleError) as raised:
        yield raised
    assert raised.value.code == code, raised.value


# ---------------------------------------------------------------- the valid fixture


def test_the_valid_fixture_loads_with_its_exact_contents() -> None:
    bundle = load_rule_bundle(FIXTURE, FIXTURE_SHA256)
    assert (bundle.bundle_id, bundle.version) == ("scripted-support-rules", "2026-10-01")
    assert [rule.id for rule in bundle.rules] == ["refund-window", "escalate-fraud", "tone"]
    assert bundle.rules[0] == Rule(
        id="refund-window",
        text="Refunds are allowed within 30 days of delivery.",
        source_ref="scripted/policy#refunds",
        applies_to=("refund", "order-status"),
    )
    assert bundle.sha256 == FIXTURE_SHA256
    assert bundle.size_bytes == len(FIXTURE.read_bytes())


def test_the_hash_is_deterministic_and_is_the_digest_of_the_raw_bytes() -> None:
    data = FIXTURE.read_bytes()
    assert sha256_of(data) == FIXTURE_SHA256 == hashlib.sha256(data).hexdigest()
    assert sha256_of(data) == sha256_of(bytes(data))
    first, second = (
        load_rule_bundle(FIXTURE, FIXTURE_SHA256),
        load_rule_bundle(str(FIXTURE), FIXTURE_SHA256),
    )
    assert first == second


def test_lookup_by_id_and_by_topic() -> None:
    bundle = load_rule_bundle(FIXTURE, FIXTURE_SHA256)
    assert bundle.rule("tone").source_ref == "scripted/style#tone"
    assert [r.id for r in bundle.rules_for("refund")] == ["refund-window", "tone"]
    assert bundle.rules_for("nothing") == ()
    assert bundle.rules_for("Refund") == (), "topics match exactly"


def test_an_unknown_rule_id_is_a_typed_refusal() -> None:
    bundle = load_rule_bundle(FIXTURE, FIXTURE_SHA256)
    with _refused(UNKNOWN_RULE):
        bundle.rule("not-in-the-bundle")
    with _refused(UNKNOWN_RULE):
        bundle.rule("")


# ---------------------------------------------------------------- bytes first


def test_an_altered_byte_is_refused_even_when_the_yaml_values_read_the_same(tmp_path: Path) -> None:
    original = GOOD.encode()
    path, digest = _write(tmp_path, original)
    assert load_rule_bundle(path, digest).bundle_id == "b1"
    equivalents = [
        original + b"\n",  # a trailing newline
        original.replace(b"b1", b"b1 "),  # trailing space inside the scalar's line
        b"# a comment\n" + original,
        original.replace(b"text:", b"text:  "),  # extra space
        original.replace(b"\n", b"\r\n"),  # line endings
        original.replace(b'"1"', b"'1'"),  # the same string, quoted differently
    ]
    for altered in equivalents:
        path.write_bytes(altered)
        with _refused(DIGEST_MISMATCH):
            load_rule_bundle(path, digest)


def test_the_digest_is_checked_before_the_yaml_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatch never reaches the parser, so bytes the operator did not pin are never interpreted."""
    path, digest = _write(tmp_path, GOOD)
    path.write_bytes(b"not: [valid")  # would be invalid_yaml if it were parsed
    parsed: list[bool] = []
    monkeypatch.setattr(rule_bundle.yaml, "load", lambda *a, **k: parsed.append(True))
    with _refused(DIGEST_MISMATCH):
        load_rule_bundle(path, digest)
    assert parsed == []


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "A" * 64,
        "g" * 64,
        "0" * 63,
        "0" * 65,
        " " + "0" * 63,
        "sha256:" + "0" * 64,
        None,
        12,
        b"0" * 64,
    ],
)
def test_the_expected_digest_must_be_a_full_lowercase_sha256(bad: Any, tmp_path: Path) -> None:
    path, _ = _write(tmp_path, GOOD)
    with _refused(BAD_EXPECTED_DIGEST):
        load_rule_bundle(path, bad)


def test_a_missing_unreadable_or_empty_file_is_missing_bytes(tmp_path: Path) -> None:
    digest = "0" * 64
    with _refused(MISSING_BYTES):
        load_rule_bundle(tmp_path / "absent.yaml", digest)
    with _refused(MISSING_BYTES):
        load_rule_bundle(tmp_path, digest)  # a directory
    empty, _ = _write(tmp_path, b"", "empty.yaml")
    with _refused(MISSING_BYTES):
        load_rule_bundle(empty, hashlib.sha256(b"").hexdigest())


def test_the_documented_size_cap_is_one_mebibyte_and_the_boundary_is_exact(tmp_path: Path) -> None:
    assert MAX_BUNDLE_BYTES == 1_048_576
    exactly = tmp_path / "exact.yaml"
    exactly.write_bytes(b"#" * MAX_BUNDLE_BYTES)
    with _refused(DIGEST_MISMATCH):  # within the cap, so it reaches the digest check
        load_rule_bundle(exactly, "0" * 64)
    over = tmp_path / "big.yaml"
    over.write_bytes(b"#" * (MAX_BUNDLE_BYTES + 1))
    with _refused(TOO_LARGE):
        load_rule_bundle(over, "0" * 64)


def test_only_a_regular_file_is_read_and_a_special_file_is_never_read_unbounded(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with _refused(
        MISSING_BYTES
    ):  # would block forever if it were opened for a read of unknown size
        load_rule_bundle(fifo, "0" * 64)
    with _refused(MISSING_BYTES):
        load_rule_bundle(tmp_path, "0" * 64)  # a directory
    if Path("/dev/zero").exists():
        with _refused(MISSING_BYTES):  # a character device would read forever
            load_rule_bundle("/dev/zero", "0" * 64)
        link = tmp_path / "zero.yaml"
        link.symlink_to("/dev/zero")
        assert not stat.S_ISREG(link.stat().st_mode)
        with _refused(MISSING_BYTES):
            load_rule_bundle(link, "0" * 64)
    real = tmp_path / "real.yaml"
    real.write_bytes(GOOD.encode())
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    assert load_rule_bundle(link, sha256_of(GOOD.encode())).bundle_id == "b1", (
        "a symlink to a regular file is fine"
    )


def test_a_file_that_is_larger_than_the_cap_is_never_read_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read itself is bounded to the cap plus one byte, so a growing file cannot exhaust memory."""
    path, _ = _write(tmp_path, GOOD)
    reads: list[int] = []

    class Handle:
        def __enter__(self) -> Handle:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            return b"#" * (MAX_BUNDLE_BYTES + 1)

    monkeypatch.setattr(rule_bundle.os, "fdopen", lambda *a, **k: Handle())
    with _refused(TOO_LARGE):
        load_rule_bundle(path, "0" * 64)
    assert reads == [MAX_BUNDLE_BYTES + 1]


def test_a_read_error_after_the_open_is_missing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = _write(tmp_path, GOOD)

    def failing(*_a: Any, **_k: Any) -> Any:
        raise OSError("input/output error")

    monkeypatch.setattr(rule_bundle.os, "fdopen", failing)
    with _refused(MISSING_BYTES):
        load_rule_bundle(path, "0" * 64)


def test_bytes_can_be_verified_and_parsed_without_a_file() -> None:
    data = GOOD.encode()
    bundle = parse_rule_bundle(data, sha256_of(data))
    assert bundle.bundle_id == "b1" and bundle.sha256 == sha256_of(data)
    with _refused(DIGEST_MISMATCH):
        parse_rule_bundle(data + b" ", sha256_of(data))
    for bad in (b"", "text", None, bytearray(data)):
        with _refused(MISSING_BYTES):
            parse_rule_bundle(bad, sha256_of(data))  # type: ignore[arg-type]
    with _refused(TOO_LARGE):
        parse_rule_bundle(b"#" * (MAX_BUNDLE_BYTES + 1), "0" * 64)
    with _refused(BAD_EXPECTED_DIGEST):
        parse_rule_bundle(data, "nope")


# ---------------------------------------------------------------- strict parsing (bytes verified, content bad)


def _load_text(tmp_path: Path, text: str | bytes) -> Any:
    path, digest = _write(tmp_path, text)
    return load_rule_bundle(path, digest)


@pytest.mark.parametrize(
    "text",
    [
        "rules: [unclosed",
        "a: b: c: d",
        "\t- tabs: [",
        b"\xff\xfe not utf-8",
        "&a x: 1\n*b : 2",
        "? [a, b]\n: c",
    ],
)
def test_invalid_yaml_or_encoding_is_refused(tmp_path: Path, text: str | bytes) -> None:
    with _refused(INVALID_YAML):
        _load_text(tmp_path, text)


@pytest.mark.parametrize(
    "document",
    [
        "x: " + "[" * 500 + "]" * 500,  # RecursionError
        GOOD.replace('version: "1"', "version: 2026-02-30"),  # an impossible date
        GOOD.replace('version: "1"', "version: 2026-01-01T25:61:61Z"),
        GOOD.replace('version: "1"', "version: " + "9" * 5000),  # an integer over the digit limit
        GOOD.replace('version: "1"', "version: !!int foo"),
        GOOD.replace('version: "1"', "version: !!float foo"),
        GOOD.replace('version: "1"', "version: !!timestamp foo"),
        GOOD.replace('version: "1"', "version: !!map foo"),
        GOOD + "? !!bool maybe\n: 1\n",
        GOOD + "? !!set {a}\n: 1\n",
        GOOD.replace("[greeting]", "[" + "9" * 5000 + "]"),
        GOOD.replace("[greeting]", "[!!timestamp foo]"),
        GOOD.replace('version: "1"', "version: !!python/object:os.system x"),
        GOOD.replace("rules:", "rules: &a\n<<: *a\nx:"),
    ],
)
def test_hostile_or_odd_yaml_is_always_invalid_yaml_never_another_exception(
    tmp_path: Path, document: str
) -> None:
    """PyYAML raises ValueError, TypeError, KeyError, AttributeError and RecursionError for some input; the loader's
    contract is that only ``RuleBundleError`` ever leaves it."""
    with pytest.raises(RuleBundleError) as raised:
        _load_text(tmp_path, document)
    assert raised.value.code in {INVALID_YAML, MALFORMED, UNKNOWN_FIELD}
    assert raised.value.code == INVALID_YAML or "x" in document or "<<" in document


def test_a_duplicate_mapping_key_is_refused_not_last_wins(tmp_path: Path) -> None:
    with _refused(INVALID_YAML):
        _load_text(tmp_path, GOOD.replace("bundle_id: b1", "bundle_id: b1\nbundle_id: b2"))
    with _refused(INVALID_YAML):
        _load_text(
            tmp_path,
            GOOD.replace(
                '    text: "Say hello."', '    text: "Say hello."\n    text: "Say goodbye."'
            ),
        )


@pytest.mark.parametrize("document", ["- a\n- b\n", "just a string", "42", "null", "[]"])
def test_the_document_must_be_a_mapping(tmp_path: Path, document: str) -> None:
    with _refused(MALFORMED):
        _load_text(tmp_path, document)


def test_the_schema_version_must_be_v1(tmp_path: Path) -> None:
    for version in ("v2", "1", "V1", "", "null"):
        with _refused(UNSUPPORTED_VERSION):
            _load_text(tmp_path, GOOD.replace("schema_version: v1", f"schema_version: {version}"))
    with _refused(UNSUPPORTED_VERSION):
        _load_text(tmp_path, GOOD.replace("schema_version: v1\n", ""))
    with _refused(UNSUPPORTED_VERSION):
        _load_text(tmp_path, GOOD.replace("schema_version: v1", "schema_version: 1"))


def test_unknown_fields_are_refused_at_both_levels_not_ignored(tmp_path: Path) -> None:
    with _refused(UNKNOWN_FIELD):
        _load_text(tmp_path, GOOD + "extra: 1\n")
    with _refused(UNKNOWN_FIELD):
        _load_text(
            tmp_path,
            GOOD.replace(
                "    applies_to: [greeting]", "    applies_to: [greeting]\n    priority: 3"
            ),
        )
    with _refused(MALFORMED):
        _load_text(tmp_path, GOOD + "1: 2\n")  # a non-string key


@pytest.mark.parametrize(
    "mutation",
    [
        ("bundle_id: b1", "bundle_id: B1"),
        ("bundle_id: b1", "bundle_id: -b1"),
        ("bundle_id: b1", "bundle_id: ''"),
        ("bundle_id: b1", "bundle_id: 5"),
        ("bundle_id: b1", "bundle_id: " + "a" * 129),
        ('version: "1"', 'version: "' + "v" * 513 + '"'),
        ('version: "1"', 'version: "a\\nb"'),
        ('    source_ref: "src#1"', '    source_ref: "a\\tb"'),
        ("    applies_to: [greeting]", '    applies_to: ["a\\nb"]'),
        ('    text: "Say hello."', '    text: "lone \\ud800 surrogate"'),
        ('    text: "Say hello."', '    text: "unassigned \\U0010ffff"'),
        ('    text: "Say hello."', '    text: "line\\u2028separator"'),
        ('version: "1"', "version: 1.0"),
        ('version: "1"', 'version: ""'),
        ('version: "1"', 'version: " 1"'),
        ("  - id: r1", "  - id: R1"),
        ("  - id: r1", "  - id: ''"),
        ('    text: "Say hello."', '    text: ""'),
        ('    text: "Say hello."', "    text: 5"),
        ('    text: "Say hello."', '    text: "bad\\x01control"'),
        ('    text: "Say hello."', '    text: "zero\\u200bwidth"'),
        ('    text: "Say hello."', '    text: "' + "x" * 8001 + '"'),
        ('    source_ref: "src#1"', '    source_ref: ""'),
        ('    source_ref: "src#1"', "    source_ref: [a]"),
        ("    applies_to: [greeting]", "    applies_to: []"),
        ("    applies_to: [greeting]", "    applies_to: greeting"),
        ("    applies_to: [greeting]", "    applies_to: [greeting, greeting]"),
        ("    applies_to: [greeting]", "    applies_to: [5]"),
        ("    applies_to: [greeting]", "    applies_to: [' padded']"),
    ],
)
def test_malformed_fields_are_refused_with_a_typed_code(
    tmp_path: Path, mutation: tuple[str, str]
) -> None:
    old, new = mutation
    assert old in GOOD
    with _refused(MALFORMED):
        _load_text(tmp_path, GOOD.replace(old, new))


def test_missing_fields_and_empty_or_wrong_rule_lists_are_refused(tmp_path: Path) -> None:
    for field in ("bundle_id: b1\n", 'version: "1"\n'):
        with _refused(MALFORMED):
            _load_text(tmp_path, GOOD.replace(field, ""))
    with _refused(MALFORMED):
        _load_text(tmp_path, GOOD.split("rules:", maxsplit=1)[0] + "rules: []\n")
    with _refused(MALFORMED):
        _load_text(tmp_path, GOOD.split("rules:", maxsplit=1)[0])
    for missing in ("    text:", "    source_ref:", "    applies_to:", "  - id:"):
        lines = [line for line in GOOD.splitlines() if not line.startswith(missing)]
        text = "\n".join(lines) + "\n"
        if missing == "  - id:":
            text = text.replace("    text:", "  - text:", 1)
        with pytest.raises(RuleBundleError) as raised:
            _load_text(tmp_path, text)
        assert raised.value.code in {MALFORMED}, missing
    with _refused(MALFORMED):
        _load_text(tmp_path, GOOD.split("rules:", maxsplit=1)[0] + "rules:\n  - just a string\n")


def test_a_duplicate_rule_id_is_refused_and_named_by_position_not_content(tmp_path: Path) -> None:
    second = GOOD.split("rules:\n")[1]
    with pytest.raises(RuleBundleError) as raised:
        _load_text(tmp_path, GOOD + second)
    assert raised.value.code == DUPLICATE_RULE_ID
    assert "rule 2" in str(raised.value) and "r1" not in str(raised.value)


def test_a_bundle_may_hold_at_most_the_documented_number_of_rules(tmp_path: Path) -> None:
    head = GOOD.split("rules:\n", maxsplit=1)[0] + "rules:\n"
    rule = '  - id: r{n}\n    text: "t"\n    source_ref: "s"\n    applies_to: [x]\n'
    ok = head + "".join(rule.format(n=n) for n in range(rule_bundle.MAX_RULES))
    assert len(_load_text(tmp_path, ok).rules) == rule_bundle.MAX_RULES
    with _refused(MALFORMED):
        _load_text(tmp_path, ok + rule.format(n="extra"))


def test_the_documented_length_boundaries_are_exact(tmp_path: Path) -> None:
    longest_id = "a" * 128
    longest_text = "t" * 8000
    longest_field = "f" * 512
    text = (
        "schema_version: v1\n"
        f'bundle_id: {longest_id}\nversion: "{longest_field}"\n'
        f'rules:\n  - id: {longest_id}\n    text: "{longest_text}"\n'
        f'    source_ref: "{longest_field}"\n    applies_to: ["{longest_field}"]\n'
    )
    bundle = _load_text(tmp_path, text)
    assert len(bundle.bundle_id) == 128 and len(bundle.rules[0].text) == 8000


def test_multiline_text_keeps_its_newlines_and_unicode_text_is_allowed(tmp_path: Path) -> None:
    text = GOOD.replace('"Say hello."', '"Line one.\\nLine two. Café — ok."')
    assert _load_text(tmp_path, text).rules[0].text == "Line one.\nLine two. Café — ok."


def test_an_unknown_field_message_never_quotes_the_key_names_from_the_file(tmp_path: Path) -> None:
    secret = "unlisted-marker-alpha"
    with pytest.raises(RuleBundleError) as raised:
        _load_text(tmp_path, GOOD + f"{secret}: x\n")
    assert raised.value.code == UNKNOWN_FIELD and secret not in str(raised.value)
    assert "1 unknown field(s)" in str(raised.value)
    with pytest.raises(RuleBundleError) as nested:
        _load_text(
            tmp_path,
            GOOD.replace(
                "    applies_to: [greeting]", f"    applies_to: [greeting]\n    {secret}: 1"
            ),
        )
    assert secret not in str(nested.value)


def test_error_messages_never_quote_the_file(tmp_path: Path) -> None:
    secret = "unlisted-marker-beta"
    for text in (GOOD.replace("Say hello.", secret).replace("bundle_id: b1", "bundle_id: B!"),):
        with pytest.raises(RuleBundleError) as raised:
            _load_text(tmp_path, text)
        assert secret not in str(raised.value)
    path, _ = _write(tmp_path, GOOD.replace("Say hello.", secret))
    with pytest.raises(RuleBundleError) as mismatch:
        load_rule_bundle(path, "0" * 64)
    assert secret not in str(mismatch.value) and str(path) not in str(mismatch.value)


def test_every_code_is_documented() -> None:
    assert {
        "bad_expected_digest",
        "missing_bytes",
        "too_large",
        "digest_mismatch",
        "invalid_yaml",
        "unsupported_version",
        "unknown_field",
        "malformed",
        "duplicate_rule_id",
        "unknown_rule",
    } == CODES
    assert RuleBundleError(MALFORMED, "x").code == "malformed"


# ---------------------------------------------------------------- purity


def test_the_loader_imports_no_database_provider_run_or_context_manifest_code() -> None:
    tree = ast.parse(Path(rule_bundle.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {
        "__future__",
        "hashlib",
        "hmac",
        "os",
        "re",
        "stat",
        "unicodedata",
        "dataclasses",
        "pathlib",
        "typing",
        "yaml",
    }


# ---------------------------------------------------------------- B07.5b (#736): the pinned bundle at context use


def _pinned_run(tmp_path: Path, text: str = GOOD) -> tuple[Path, str, dict[str, Any]]:
    """A bundle pinned the way admission pins it: the run manifest's own ``rule_bundle_path`` / ``rule_bundle_sha256``."""
    path, digest = _write(tmp_path, text)
    pinned = pin_rule_bundle(path, digest)
    built = build_run_manifest(
        manifest_id="mf-rules",
        workflow_run_id="run-rules",
        pins=ResolvedPins(
            workflow_version="wfv-1",
            bundle_digest=hashlib.sha256(b"compiled").hexdigest(),
            prompt_versions=[],
            tool_versions=[],
            skill_versions=[],
            knowledge_base_versions=[],
            model_routes=["scripted"],
            policy_version="pol-1",
        ),
        subject=Subject("alice", "session", "proj-1"),
        workload=Workload.for_workflow("rules", hashlib.sha256(b"wf").hexdigest()),
        purpose="answer",
        scope=DeploymentScope.fixed("proj-1"),
        created_at=datetime(2026, 10, 28, 9, 0, 0, tzinfo=timezone.utc),
        rule_bundle=pinned,
        rule_bundles_enabled=True,
    )
    return path, digest, built.record


def test_context_references_pinned_hash(tmp_path: Path) -> None:
    path, digest, record = _pinned_run(tmp_path)
    verified = rule_bundle_reference(record)
    assert verified is not None
    reference = verified.record
    assert reference == {
        "bundle_id": "b1",
        "version": "1",
        "sha256": digest,
        "rules": [
            {
                "rule_id": "r1",
                "source_ref": "src#1",
                "text_digest": hashlib.sha256(b"Say hello.").hexdigest(),
            }
        ],
    }
    assert record["rule_bundle_sha256"] == digest
    assert record["rule_bundle_path"] == str(path.resolve())
    assert "Say hello." not in repr(reference), "the rule text is not recorded, only its digest"
    again = rule_bundle_reference(record)
    assert again is not None and again.record == reference, "the same pin gives the same reference"
    assert again.record is not verified.record, (
        "a fresh copy each time: a caller cannot alter the next one"
    )
    assert again.bundle is verified.bundle and again.bundle.rules[0].text == "Say hello."


def test_the_whole_fixture_bundle_is_selected_in_bundle_order_with_provenance() -> None:
    record = {"rule_bundle_path": str(FIXTURE), "rule_bundle_sha256": FIXTURE_SHA256}
    verified = rule_bundle_reference(record)
    assert verified is not None
    reference = verified.record
    assert [(r["rule_id"], r["source_ref"]) for r in reference["rules"]] == [
        ("refund-window", "scripted/policy#refunds"),
        ("escalate-fraud", "scripted/policy#fraud"),
        ("tone", "scripted/style#tone"),
    ]
    assert reference["bundle_id"] == "scripted-support-rules"


def test_a_run_admitted_without_a_bundle_gets_no_reference_even_if_one_is_enabled_later() -> None:
    assert rule_bundle_reference({"rule_bundle_path": None, "rule_bundle_sha256": None}) is None
    assert rule_bundle_reference({}) is None


def test_admitted_hash_unchanged(tmp_path: Path) -> None:
    path, digest, record = _pinned_run(tmp_path)
    before = dict(record)
    rule_bundle_reference(record)
    path.write_text(GOOD.replace("Say hello.", "Say goodbye."), encoding="utf-8")
    with pytest.raises(ContextManifestError):
        rule_bundle_reference(record)
    assert record == before, "the manifest the pin was read from is never rewritten or backfilled"
    assert record["rule_bundle_sha256"] == digest


def test_changed_bytes_refused_at_use(tmp_path: Path) -> None:
    path, digest, record = _pinned_run(tmp_path)
    # the same values, one more comment: the YAML reads the same, the bytes are not the pinned bytes
    path.write_text(GOOD + "# edited after admission\n", encoding="utf-8")
    with pytest.raises(ContextManifestError) as comment:
        rule_bundle_reference(record)
    assert comment.value.code == RULE_BUNDLE_CHANGED == "rule_bundle_changed"
    assert "edited after admission" not in str(comment.value), "the message never quotes the file"
    path.write_bytes(b"x" * (MAX_BUNDLE_BYTES + 1))
    with pytest.raises(ContextManifestError) as large:
        rule_bundle_reference(record)
    assert large.value.code == RULE_BUNDLE_CHANGED
    # the original bytes restored: the pin is honoured again, with nothing re-admitted
    path.write_text(GOOD, encoding="utf-8")
    reference = rule_bundle_reference(record)
    assert reference is not None and reference.record["sha256"] == digest


def test_an_unreadable_pinned_bundle_is_unavailable_not_changed(tmp_path: Path) -> None:
    path, _, record = _pinned_run(tmp_path)
    path.unlink()
    for pin in (
        record,  # the file is gone
        {**record, "rule_bundle_path": str(tmp_path)},  # a directory
        {**record, "rule_bundle_path": "relative-and-absent.yaml"},
        {**record, "rule_bundle_path": "bad\x00path"},  # an embedded NUL
        {**record, "rule_bundle_sha256": "ABC"},  # not a lowercase 64-hex digest
    ):
        with pytest.raises(ContextManifestError) as refused:
            rule_bundle_reference(pin)
        assert refused.value.code == RULE_BUNDLE_UNAVAILABLE == "rule_bundle_unavailable"
    path.write_bytes(b"")
    with pytest.raises(ContextManifestError) as empty:
        rule_bundle_reference(record)
    assert empty.value.code == RULE_BUNDLE_UNAVAILABLE


def test_a_half_pinned_or_malformed_pin_fails_closed(tmp_path: Path) -> None:
    _, digest, _ = _pinned_run(tmp_path)
    where = str(tmp_path / "bundle.yaml")
    for pin in (
        {"rule_bundle_path": where, "rule_bundle_sha256": None},
        {"rule_bundle_path": None, "rule_bundle_sha256": digest},
        {"rule_bundle_path": "", "rule_bundle_sha256": digest},
        {"rule_bundle_path": 5, "rule_bundle_sha256": digest},
        {"rule_bundle_path": where, "rule_bundle_sha256": 5},
    ):
        with pytest.raises(ContextManifestError) as refused:
            rule_bundle_reference(pin)
        assert refused.value.code == RULE_BUNDLE_UNAVAILABLE, pin


def test_hostile_bytes_at_the_pinned_path_are_refused_without_leaking_them(tmp_path: Path) -> None:
    path, _, record = _pinned_run(tmp_path)
    secret = "TOP-SECRET-PAYLOAD"
    for hostile in (
        f"!!python/object/apply:os.system ['{secret}']\n".encode(),
        f"{secret}: *loop\n".encode(),
        b"\xff\xfe" + secret.encode(),
        secret.encode() * 4096,
    ):
        path.write_bytes(hostile)
        with pytest.raises(ContextManifestError) as refused:
            rule_bundle_reference(record)
        assert refused.value.code == RULE_BUNDLE_CHANGED
        assert secret not in str(refused.value)


def test_a_repointed_path_after_admission_cannot_change_the_run_pin(tmp_path: Path) -> None:
    original, digest = _write(tmp_path, GOOD, "v1.yaml")
    other, other_digest = _write(tmp_path, GOOD.replace("b1", "b2"), "v2.yaml")
    alias = tmp_path / "current.yaml"
    alias.symlink_to(original)
    pinned = pin_rule_bundle(alias, digest)
    record = {"rule_bundle_path": pinned.path, "rule_bundle_sha256": digest}
    assert pinned.path == str(original.resolve()), (
        "admission records the resolved file, not the alias"
    )
    alias.unlink()
    alias.symlink_to(other)  # the alias now means a different, perfectly valid bundle
    verified = rule_bundle_reference(record)
    assert verified is not None
    assert (verified.record["bundle_id"], verified.record["sha256"]) == ("b1", digest)
    assert verified.record["sha256"] != other_digest


def test_the_reference_builder_is_pure_over_a_verified_bundle() -> None:
    bundle = parse_rule_bundle(GOOD.encode(), sha256_of(GOOD.encode()))
    assert rule_bundle_record(bundle)["rules"][0]["rule_id"] == "r1"
    assert RULE_BUNDLE_MISMATCH == "rule_bundle_mismatch"
    assert RULE_BUNDLE_UNPINNED == "rule_bundle_unpinned"


def test_the_context_reference_module_reads_only_the_pinned_file() -> None:
    tree = ast.parse(Path(context_rules.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
    assert imported <= {
        "__future__",
        "copy",
        "threading",
        "collections",
        "collections.abc",
        "dataclasses",
        "typing",
        "caliber.context.manifest",
        "caliber.context.rule_bundle",
    }, imported


# ---------------------------------------------------------------- the citation cap at admission (B07.5b, #736)


def _bundle_text(rules: int, source_ref: str, *, bundle_id: str = "sized") -> str:
    """A valid bundle of ``rules`` rules, each citing ``source_ref`` (a value may hold up to 512 characters)."""
    lines = [f'schema_version: v1\nbundle_id: {bundle_id}\nversion: "1"\nrules:\n']
    for number in range(rules):
        lines.append(
            f'  - id: r{number:04d}\n    text: "Say hello."\n    source_ref: "{source_ref}"\n'
            "    applies_to: [greeting]\n"
        )
    return "".join(lines)


def _citation_size(path: Path, digest: str) -> int:
    bundle = load_rule_bundle(path, digest)
    return len(canonical_bytes(rule_bundle_record(bundle)))


CJK = "漢" * 512  # 512 characters, 3 bytes each


def test_a_bundle_whose_citation_cannot_fit_a_context_manifest_is_refused_at_admission(
    tmp_path: Path,
) -> None:
    # The file is far below the loader's 1 MiB cap, but every step's context manifest would have to cite 340 KB of it.
    path, digest = _write(tmp_path, _bundle_text(200, CJK))
    assert path.stat().st_size < MAX_BUNDLE_BYTES, "the loader itself accepts this file"
    assert _citation_size(path, digest) > MAX_CITATION_BYTES == 262_144
    with pytest.raises(ManifestError) as refused:
        pin_rule_bundle(path, digest)
    assert refused.value.code == "rule_bundle_refused"
    message = str(refused.value)
    assert "262144" in message and "context citation" in message, "the cap and the size are named"
    assert CJK[:8] not in message and "r0001" not in message, "no payload in the message"


def test_a_bundle_just_under_the_cap_is_admitted_and_every_steps_manifest_builds(
    tmp_path: Path,
) -> None:
    path, digest = _write(tmp_path, _bundle_text(150, CJK))
    size = _citation_size(path, digest)
    assert 200_000 < size <= MAX_CITATION_BYTES, size
    pinned = pin_rule_bundle(path, digest)  # admitted
    # ...and a context manifest with the most segments a step may have, each with a long prompt reference, still builds
    segments = [
        ContextSegment(
            "instruction",
            "workflow_instruction",
            {"kind": "prompt", "reference": f"p{number}-" + "x" * 240},
            "text",
            1,
        )
        for number in range(512)
    ]
    built = build_context_manifest(
        context_manifest_id="cm-cap",
        workflow_run_id="WR-cap",
        step_request_id="sr-cap",
        revision=1,
        scope=DeploymentScope.fixed("P-cap"),
        segments=segments,
        token_budget=10_000,
        created_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        rule_bundle=rule_bundle_record(pinned.bundle),
    )
    assert len(built.manifest_bytes) < (1 << 20)
    over, over_digest = _write(tmp_path, _bundle_text(165, CJK), "over.yaml")
    with pytest.raises(ManifestError):
        pin_rule_bundle(over, over_digest)


def test_the_small_fixture_is_far_below_the_cap() -> None:
    assert _citation_size(FIXTURE, FIXTURE_SHA256) < 2_000


# ---------------------------------------------------------------- verification on every use, and the cache (B07.5b)


def test_the_bytes_are_re_read_and_re_hashed_on_every_call_while_the_parse_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_rule_bundle_cache()
    text = GOOD.replace("b1", "cached-one")
    path, digest, record = _pinned_run(tmp_path, text)
    parses: list[int] = []
    real_parse = context_rules.parse_rule_bundle
    monkeypatch.setattr(
        context_rules,
        "parse_rule_bundle",
        lambda data, expected: parses.append(1) or real_parse(data, expected),
    )
    reads: list[int] = []
    real_read = context_rules.read_rule_bundle_bytes
    monkeypatch.setattr(
        context_rules,
        "read_rule_bundle_bytes",
        lambda target: reads.append(1) or real_read(target),
    )
    for _ in range(5):
        verified = rule_bundle_reference(record)
        assert verified is not None and verified.record["sha256"] == digest
    assert (len(parses), len(reads)) == (1, 5), "parsed once, read from the file on every step"

    # changed bytes: the cached parse of the old digest is not served; the step is refused
    path.write_text(text + "# edited\n", encoding="utf-8")
    with pytest.raises(ContextManifestError) as refused:
        rule_bundle_reference(record)
    assert refused.value.code == RULE_BUNDLE_CHANGED
    assert len(reads) == 6 and len(parses) == 2, "a miss goes to the parser, which refuses it"
    # the original bytes back: a hit again, without another parse
    path.write_text(text, encoding="utf-8")
    assert rule_bundle_reference(record) is not None
    assert len(parses) == 2


def test_a_cached_digest_never_stands_in_for_bytes_that_were_not_read(tmp_path: Path) -> None:
    clear_rule_bundle_cache()
    path, _, record = _pinned_run(tmp_path)
    assert rule_bundle_reference(record) is not None  # now cached under the pinned digest
    path.write_bytes(b"not the pinned bytes")
    with pytest.raises(ContextManifestError) as refused:
        rule_bundle_reference(record)
    assert refused.value.code == RULE_BUNDLE_CHANGED
    path.unlink()
    with pytest.raises(ContextManifestError) as gone:
        rule_bundle_reference(record)
    assert gone.value.code == RULE_BUNDLE_UNAVAILABLE


def test_the_cache_is_bounded_and_evicts_the_least_recently_used(tmp_path: Path) -> None:
    clear_rule_bundle_cache()
    records = []
    for number in range(CACHE_SIZE + 3):
        path = tmp_path / f"b{number}.yaml"
        path.write_text(GOOD.replace("b1", f"bundle-{number}"), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        record = {"rule_bundle_path": str(path), "rule_bundle_sha256": digest}
        assert rule_bundle_reference(record) is not None
        records.append((digest, record))
    assert len(context_rules._cache) == CACHE_SIZE
    kept = list(context_rules._cache)
    assert records[0][0] not in kept and records[-1][0] in kept
    assert rule_bundle_reference(records[3][1]) is not None  # a hit makes it the most recent
    assert list(context_rules._cache)[-1] == records[3][0]


def test_a_malformed_digest_is_never_cached_or_served(tmp_path: Path) -> None:
    clear_rule_bundle_cache()
    path, _, _ = _pinned_run(tmp_path)
    for digest in ("A" * 64, "abc", "0" * 64):
        with pytest.raises(ContextManifestError):
            rule_bundle_reference({"rule_bundle_path": str(path), "rule_bundle_sha256": digest})
    assert len(context_rules._cache) == 0


def test_the_file_is_opened_without_a_controlling_terminal_or_inheritance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest = _write(tmp_path, GOOD)
    flags: list[int] = []
    real_open = os.open

    def spy(target: Any, mode: int, *args: Any) -> int:
        flags.append(mode)
        return real_open(target, mode, *args)

    monkeypatch.setattr(rule_bundle.os, "open", spy)
    load_rule_bundle(path, digest)
    assert len(flags) == 1
    for name in ("O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC"):
        assert flags[0] & getattr(os, name), name


# ---------------------------------------------------------------- the recorded path is the symlink-resolved one


def test_a_rotating_mount_leaves_the_recorded_path_missing_on_the_next_use(tmp_path: Path) -> None:
    """The ConfigMap/Secret pattern: ``rules.yaml -> ..data/rules.yaml``, ``..data -> ..data_<timestamp>``.

    Admission records the resolved ``..data_<timestamp>`` path, which the kubelet removes on the next rotation. The
    pin is not rewritten, so a run admitted before the rotation cannot use its bundle: ``rule_bundle_unavailable``.
    """
    mount = tmp_path / "mount"
    first = mount / "..data_1"
    first.mkdir(parents=True)
    (first / "rules.yaml").write_text(GOOD, encoding="utf-8")
    (mount / "..data").symlink_to("..data_1")
    (mount / "rules.yaml").symlink_to("..data/rules.yaml")
    digest = hashlib.sha256(GOOD.encode()).hexdigest()
    pinned = pin_rule_bundle(mount / "rules.yaml", digest)
    assert pinned.path == str((first / "rules.yaml").resolve()), (
        "the resolved path, not the stable name"
    )
    record = {"rule_bundle_path": pinned.path, "rule_bundle_sha256": digest}
    assert rule_bundle_reference(record) is not None

    second = mount / "..data_2"  # the same bytes, republished under a new directory
    second.mkdir()
    (second / "rules.yaml").write_text(GOOD, encoding="utf-8")
    (mount / "..data_tmp").symlink_to("..data_2")
    (mount / "..data_tmp").replace(mount / "..data")
    (first / "rules.yaml").unlink()
    first.rmdir()
    assert pin_rule_bundle(mount / "rules.yaml", digest) is not None, "new admissions still work"
    # ...but the run admitted before the rotation records the old directory
    with pytest.raises(ContextManifestError) as refused:
        rule_bundle_reference(record)
    assert refused.value.code == RULE_BUNDLE_UNAVAILABLE
