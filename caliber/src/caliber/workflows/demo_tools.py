"""Built-in demo tools for support-style workflow fixtures (plan §22).

These are safe, side-effect-free Python callables registered in the tool
registry so the support workflow can resolve real tools during preview/runtime
without any external dependency. Real deployments register their own tools
pointing at their own modules; these exist so the demo and the test-suite have
something concrete to bind.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def lookup_policy(query: str = "") -> dict[str, Any]:
    """Return the (canned) policy text relevant to a query. Read-only."""
    return {"policy": "Purchases within 30 days are eligible for a full refund.", "query": query}


def get_order(order_id: str = "") -> dict[str, Any]:
    """Return (canned) order status for an order id. Read-only."""
    return {"order_id": order_id or "unknown", "status": "delivered"}


def escalate(reason: str = "") -> dict[str, Any]:
    """Escalate to a human agent (no-op stub). Marked external_action in the registry."""
    return {"escalated": True, "reason": reason}


# --- Support / travel / order scenario tool stubs ---------------------------
#
# Canned, dependency-free callables retained as TEST SCAFFOLDING only. The demo
# seed scenarios were removed and these tools are no longer seeded into the live
# registry; the workflow test fixtures (tests/workflow_helpers.py:
# register_demo_tools / make_support_manifest) still register them on demand so
# the workflow-runtime tests have realistic tools to exercise.


def lookup_order(order_id: str = "") -> dict[str, Any]:
    """Look up an order's line items + status (canned). Read-only."""
    return {"order_id": order_id or "12345", "items": ["ACME Laptop"], "status": "delivered"}


def initiate_refund(order_id: str = "") -> dict[str, Any]:
    """Begin a refund for an order (stub). Marked write in the registry."""
    return {"order_id": order_id or "12345", "refund_status": "initiated", "amount_usd": 1299.0}


def track_shipment(tracking_id: str = "") -> dict[str, Any]:
    """Return (canned) shipment tracking. Read-only."""
    return {"tracking_id": tracking_id or "1Z999", "status": "in_transit", "eta_days": 2}


def search_knowledge_base(query: str = "") -> dict[str, Any]:
    """Search the (canned) product/policy knowledge base. Read-only."""
    return {"query": query, "articles": ["Returns & refunds", "Shipping & delivery"]}


def parse_travel_request(query: str = "") -> dict[str, Any]:
    """Extract structured travel intent from a request (canned). Read-only."""
    return {"origin": "SFO", "destination": "TYO", "intent": "flight", "query": query}


def search_flights(query: str = "") -> dict[str, Any]:
    """Return (canned) flight options. Read-only."""
    return {"options": [{"flight": "UA837", "price_usd": 980.0, "stops": 0}], "query": query}


def book_flight(flight: str = "") -> dict[str, Any]:
    """Book a flight (stub). Marked external_action in the registry."""
    return {"flight": flight or "UA837", "confirmation": "ABC123", "amount_usd": 980.0}


def search_hotels(query: str = "") -> dict[str, Any]:
    """Return (canned) hotel options. Read-only."""
    return {
        "options": [{"hotel": "Shibuya Grand", "price_usd": 220.0, "nights": 7}],
        "query": query,
    }


def book_hotel(hotel: str = "") -> dict[str, Any]:
    """Book a hotel (stub). Marked external_action in the registry."""
    return {"hotel": hotel or "Shibuya Grand", "confirmation": "HOT456", "amount_usd": 1540.0}


def lookup_product(sku: str = "") -> dict[str, Any]:
    """Look up a product by SKU (canned). Read-only."""
    return {"sku": sku or "APW-500", "name": "ACME Pro Widget", "price_usd": 49.0}


def check_inventory(sku: str = "") -> dict[str, Any]:
    """Check inventory for a SKU (canned). Read-only."""
    return {"sku": sku or "APW-500", "in_stock": True, "quantity_available": 128}


def create_order(_payload: str = "") -> dict[str, Any]:
    """Create an order (stub). Marked write in the registry."""
    return {"product_id": "APW-500", "quantity": 3, "total": 147.0, "order_id": "ORD-9001"}


def send_confirmation_email(_payload: str = "") -> dict[str, Any]:
    """Send an order-confirmation email (stub). Marked external_action in the registry."""
    return {"sent": True, "channel": "email"}


def read_text_file(
    path: str = "", max_bytes: int = 200_000, encoding: str = "utf-8"
) -> dict[str, Any]:
    """Read one local text file with a bounded byte limit."""
    file_path = Path(path).expanduser()
    text, metadata = _read_text(file_path, max_bytes=max_bytes, encoding=encoding)
    return {"text": text, "metadata": metadata}


def list_folder_files(
    path: str = ".",
    pattern: str = "**/*",
    recursive: bool = True,
    max_files: int = 100,
) -> dict[str, Any]:
    """List files in a folder using a glob pattern."""
    files = _iter_files(path, pattern=pattern, recursive=recursive, max_files=max_files)
    return {
        "files": [
            {
                "path": str(file_path),
                "relative_path": _relative_to(file_path, Path(path).expanduser()),
                "bytes": file_path.stat().st_size,
            }
            for file_path in files
        ],
        "count": len(files),
        "max_files": max_files,
    }


def grep_files(
    query: str = "",
    path: str = ".",
    pattern: str = "**/*",
    case_sensitive: bool = False,
    max_matches: int = 50,
) -> dict[str, Any]:
    """Find literal text matches across files."""
    if not query:
        return {"query": query, "matches": []}
    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []
    for file_path in _iter_files(path, pattern=pattern, recursive=True, max_files=500):
        try:
            text, _metadata = _read_text(file_path, max_bytes=500_000, encoding="utf-8")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                matches.append(
                    {
                        "path": str(file_path),
                        "line": line_number,
                        "text": line[:500],
                    }
                )
                if len(matches) >= max_matches:
                    return {"query": query, "matches": matches, "truncated": True}
    return {"query": query, "matches": matches, "truncated": False}


def regex_search(
    pattern: str = "",
    text: str = "",
    path: str = "",
    flags: str = "",
    max_matches: int = 50,
) -> dict[str, Any]:
    """Search text or one file with a Python regular expression."""
    if not pattern:
        return {"pattern": pattern, "matches": []}
    haystack = text
    source = "text"
    if path:
        file_path = Path(path).expanduser()
        haystack, _metadata = _read_text(file_path, max_bytes=500_000, encoding="utf-8")
        source = str(file_path)
    compiled = re.compile(pattern, _regex_flags(flags))
    matches = [
        {
            "source": source,
            "start": match.start(),
            "end": match.end(),
            "text": match.group(0)[:500],
            "groups": match.groupdict(),
        }
        for match in list(compiled.finditer(haystack))[:max_matches]
    ]
    return {"pattern": pattern, "matches": matches, "truncated": len(matches) == max_matches}


def grok_parse(pattern: str = "", text: str = "") -> dict[str, Any]:
    """Parse text with a small built-in Grok-compatible pattern subset."""
    if not pattern:
        return {"pattern": pattern, "matches": []}
    compiled = re.compile(_grok_to_regex(pattern))
    return {
        "pattern": pattern,
        "matches": [
            {"text": match.group(0), "fields": match.groupdict()}
            for match in compiled.finditer(text)
        ],
    }


def sandbox_python(code: str = "", timeout_seconds: float = 2.0) -> dict[str, Any]:
    """Run a short Python snippet in an isolated temp working directory."""
    timeout = min(max(float(timeout_seconds or 2.0), 0.1), 10.0)
    with tempfile.TemporaryDirectory(prefix="caliber-sandbox-") as tmp:
        try:
            completed = subprocess.run(  # noqa: S603
                [sys.executable, "-I", "-c", code],
                cwd=tmp,
                env={},
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "timed_out": True,
                "returncode": None,
                "stdout": _clip_output(exc.stdout),
                "stderr": _clip_output(exc.stderr),
            }
    return {
        "timed_out": False,
        "returncode": completed.returncode,
        "stdout": _clip_output(completed.stdout),
        "stderr": _clip_output(completed.stderr),
    }


def _read_text(path: Path, *, max_bytes: int, encoding: str) -> tuple[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"path does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"path is not a file: {path}")
    limit = min(max(int(max_bytes), 1), 5_000_000)
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    truncated = len(raw) > limit
    raw = raw[:limit]
    text = raw.decode(encoding or "utf-8", errors="replace")
    return text, {
        "path": str(path),
        "bytes": len(raw),
        "truncated": truncated,
        "encoding": encoding or "utf-8",
    }


def _iter_files(
    path: str,
    *,
    pattern: str,
    recursive: bool,
    max_files: int,
) -> list[Path]:
    base = Path(path or ".").expanduser()
    if base.is_file():
        return [base]
    if not base.exists():
        raise FileNotFoundError(f"path does not exist: {base}")
    if not base.is_dir():
        raise NotADirectoryError(f"path is not a folder: {base}")
    glob_pattern = pattern or ("**/*" if recursive else "*")
    if not recursive and "**" in glob_pattern:
        glob_pattern = glob_pattern.replace("**/", "").replace("**", "*")
    return sorted((p for p in base.glob(glob_pattern) if p.is_file()), key=str)[:max_files]


def _relative_to(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return path.name


def _regex_flags(flags: str) -> int:
    out = 0
    if "i" in flags:
        out |= re.IGNORECASE
    if "m" in flags:
        out |= re.MULTILINE
    if "s" in flags:
        out |= re.DOTALL
    return out


_GROK_PATTERNS = {
    "WORD": r"\w+",
    "NUMBER": r"[-+]?\d+(?:\.\d+)?",
    "INT": r"[-+]?\d+",
    "DATA": r".*?",
    "GREEDYDATA": r".*",
    "SPACE": r"\s+",
    "IP": r"(?:\d{1,3}\.){3}\d{1,3}",
}
_GROK_TOKEN_RE = re.compile(r"%\{(?P<kind>[A-Z0-9_]+)(?::(?P<name>[A-Za-z_][A-Za-z0-9_]*))?\}")


def _grok_to_regex(pattern: str) -> str:
    parts: list[str] = []
    pos = 0
    for match in _GROK_TOKEN_RE.finditer(pattern):
        parts.append(re.escape(pattern[pos : match.start()]))
        kind = match.group("kind")
        expr = _GROK_PATTERNS.get(kind)
        if expr is None:
            raise ValueError(f"unknown grok pattern {kind!r}")
        name = match.group("name")
        parts.append(f"(?P<{name}>{expr})" if name else f"(?:{expr})")
        pos = match.end()
    parts.append(re.escape(pattern[pos:]))
    return "".join(parts)


def _clip_output(value: str | bytes | None, limit: int = 4_000) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return text[:limit]


# --- run working-directory file tools (storage doc §4.4) --------------------
#
# These registry stubs let a workflow manifest *bind* the run file tools so
# validation passes and they appear in the tools UI. At execution time the run
# route injects the real, run-scoped implementations (bound to the run's
# WorkingDirectoryService + session) via ``execute(..., extra_tools=...)``,
# which override these stubs. Outside a real run (preview, ungrounded test) the
# stubs return a benign placeholder so nothing crashes.

_FILE_TOOL_NOTE = "Bound to the run working directory at execution time."


def list_workdir_files(arg: str = "") -> dict[str, Any]:  # noqa: ARG001
    """List files in the run working directory (run-bound at execution time)."""
    return {"files": [], "note": _FILE_TOOL_NOTE}


def read_workdir_file(arg: str = "") -> dict[str, Any]:  # noqa: ARG001
    """Read a run-scoped file (run-bound at execution time)."""
    return {"content": "", "note": _FILE_TOOL_NOTE}


def get_file_metadata(arg: str = "") -> dict[str, Any]:  # noqa: ARG001
    """Return run-scoped file metadata (run-bound at execution time)."""
    return {"note": _FILE_TOOL_NOTE}


def write_workdir_file(arg: str = "") -> dict[str, Any]:  # noqa: ARG001
    """Write a run-scoped work file (run-bound at execution time)."""
    return {"written": False, "note": _FILE_TOOL_NOTE}


def create_artifact(arg: str = "") -> dict[str, Any]:  # noqa: ARG001
    """Write + register a run artifact (run-bound at execution time)."""
    return {"registered": False, "note": _FILE_TOOL_NOTE}


__all__ = [
    "book_flight",
    "book_hotel",
    "check_inventory",
    "create_artifact",
    "create_order",
    "escalate",
    "get_file_metadata",
    "get_order",
    "grep_files",
    "grok_parse",
    "initiate_refund",
    "list_folder_files",
    "list_workdir_files",
    "lookup_order",
    "lookup_policy",
    "lookup_product",
    "parse_travel_request",
    "read_text_file",
    "read_workdir_file",
    "regex_search",
    "sandbox_python",
    "search_flights",
    "search_hotels",
    "search_knowledge_base",
    "send_confirmation_email",
    "track_shipment",
    "write_workdir_file",
]
