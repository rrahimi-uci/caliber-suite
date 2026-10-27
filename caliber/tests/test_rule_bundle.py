"""RuleBundle v1: the raw bytes are verified before anything is parsed, and parsing is strict (#726).

Pure: no database, no provider, no network, no run. The fixture is scripted test content, not domain acceptance.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import pytest

from caliber.context import rule_bundle
from caliber.context.rule_bundle import (
    BAD_EXPECTED_DIGEST,
    CODES,
    DIGEST_MISMATCH,
    DUPLICATE_RULE_ID,
    INVALID_YAML,
    MALFORMED,
    MAX_BUNDLE_BYTES,
    MISSING_BYTES,
    TOO_LARGE,
    UNKNOWN_FIELD,
    UNKNOWN_RULE,
    UNSUPPORTED_VERSION,
    Rule,
    RuleBundleError,
    load_rule_bundle,
    sha256_of,
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


def _refused(code: str) -> Any:
    return pytest.raises(RuleBundleError, check=lambda e: e.code == code)


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


def test_an_oversize_bundle_is_refused_before_it_is_read(tmp_path: Path) -> None:
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (MAX_BUNDLE_BYTES + 1))
    with _refused(TOO_LARGE):
        load_rule_bundle(big, "0" * 64)


def test_a_file_that_grows_between_the_size_check_and_the_read_is_still_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = _write(tmp_path, GOOD)
    real_read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: real_read(self) + b"#" * (MAX_BUNDLE_BYTES + 1)
    )
    with _refused(TOO_LARGE):
        load_rule_bundle(path, "0" * 64)


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


def test_multiline_text_keeps_its_newlines_and_unicode_text_is_allowed(tmp_path: Path) -> None:
    text = GOOD.replace('"Say hello."', '"Line one.\\nLine two. Café — ok."')
    assert _load_text(tmp_path, text).rules[0].text == "Line one.\nLine two. Café — ok."


def test_error_messages_never_quote_the_file(tmp_path: Path) -> None:
    secret = "TOP-SECRET-RULE-TEXT"
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
        "re",
        "unicodedata",
        "dataclasses",
        "pathlib",
        "typing",
        "yaml",
    }
