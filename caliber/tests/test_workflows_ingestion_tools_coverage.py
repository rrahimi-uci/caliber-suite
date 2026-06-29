"""Coverage tests for caliber.workflows.ingestion_tools error/edge branches.

These exercise the document-ingestion paths that the sibling
``test_ingestion_tools.py`` leaves uncovered without installing the optional
``[ingest]`` / ``[ocr]`` extras: the OCR worker body and CLI, the JSON-decode
failure branch, the pptx/xlsx fake-backed extractors, the generic
extraction-failure wrap, and the attach-source size guard.

Every heavy parser / IO boundary (``fitz`` / ``pytesseract`` / ``PIL`` /
``pptx`` / ``openpyxl`` / ``subprocess`` / ``os._exit``) is faked or
monkeypatched -- no optional dependency, no subprocess, and no real process
exit.
"""

from __future__ import annotations

import io
import json
import types
from pathlib import Path

import pytest

from caliber.workflows import ingestion_tools
from caliber.workflows.ingestion_tools import IngestionError


# --------------------------------------------------------------------------- #
# _ensure_ocr_dependency (line 92)
# --------------------------------------------------------------------------- #
def test_ensure_ocr_dependency_missing_module_raises() -> None:
    """A module that has no spec yields an actionable [ocr]-extra error."""
    with pytest.raises(IngestionError, match=r"scanned-PDF OCR needs the optional dependency"):
        ingestion_tools._ensure_ocr_dependency("caliber_no_such_ocr_module_xyz")


def test_ensure_ocr_dependency_present_module_is_noop() -> None:
    """A module that IS importable (e.g. ``io``) passes silently."""
    assert ingestion_tools._ensure_ocr_dependency("io") is None


# --------------------------------------------------------------------------- #
# _ocr_pdf_worker_impl (lines 105-120)
# --------------------------------------------------------------------------- #
class _FakePixmap:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def tobytes(self, fmt: str) -> bytes:
        assert fmt == "png"
        return self._payload


class _FakePage:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.seen_dpi: int | None = None

    def get_pixmap(self, *, dpi: int) -> _FakePixmap:
        self.seen_dpi = dpi
        return _FakePixmap(self._payload)


class _FakeDoc:
    def __init__(self, pages: list[_FakePage]) -> None:
        self._pages = pages
        self.closed = False

    def __iter__(self) -> object:  # mirrors PyMuPDF document iteration
        return iter(self._pages)

    def close(self) -> None:
        self.closed = True


def _install_fake_ocr_stack(
    monkeypatch: pytest.MonkeyPatch,
    *,
    doc: _FakeDoc,
    page_text: dict[bytes, str],
) -> dict[str, object]:
    """Fake fitz / pytesseract / PIL.Image so the worker body runs in-process."""
    captured: dict[str, object] = {}

    fake_fitz = types.SimpleNamespace(open=lambda path: doc)

    def fake_image_to_string(image: object) -> str:
        return page_text[image]

    fake_pytesseract = types.SimpleNamespace(image_to_string=fake_image_to_string)

    class _FakePilImage:
        @staticmethod
        def open(buffer: io.BytesIO) -> bytes:
            # Map the rasterized PNG bytes back to its page text via identity.
            return buffer.getvalue()

    fake_pil = types.SimpleNamespace(Image=_FakePilImage)

    def fake_require(module: str, fmt: str, *, extra: str = "ingest") -> object:
        captured.setdefault("modules", []).append(module)  # type: ignore[union-attr]
        return {
            "fitz": fake_fitz,
            "pytesseract": fake_pytesseract,
            "PIL.Image": fake_pil,
        }[module]

    monkeypatch.setattr(ingestion_tools, "_require", fake_require)
    return captured


def test_ocr_pdf_worker_impl_joins_page_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each page is rasterized + OCR'd; non-empty page text is joined, blanks dropped."""
    page_a = _FakePage(b"PNG-A")
    page_blank = _FakePage(b"PNG-BLANK")
    page_b = _FakePage(b"PNG-B")
    doc = _FakeDoc([page_a, page_blank, page_b])
    # _FakePilImage.open returns the raw PNG bytes, keyed here.
    _install_fake_ocr_stack(
        monkeypatch,
        doc=doc,
        page_text={b"PNG-A": "  page one  ", b"PNG-BLANK": "   ", b"PNG-B": "page two"},
    )

    out = ingestion_tools._ocr_pdf_worker_impl(Path("/tmp/scan.pdf"), dpi=150)

    assert out == "page one\n\npage two"
    assert doc.closed is True
    assert page_a.seen_dpi == 150  # dpi threaded through to get_pixmap


def test_ocr_pdf_worker_impl_respects_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """``max_pages`` halts the loop once that many non-empty pages are collected."""
    pages = [_FakePage(f"PNG-{i}".encode()) for i in range(5)]
    doc = _FakeDoc(pages)
    _install_fake_ocr_stack(
        monkeypatch,
        doc=doc,
        page_text={f"PNG-{i}".encode(): f"text {i}" for i in range(5)},
    )

    out = ingestion_tools._ocr_pdf_worker_impl(Path("/tmp/scan.pdf"), max_pages=2)

    assert out == "text 0\n\ntext 1"
    assert doc.closed is True


# --------------------------------------------------------------------------- #
# _ocr_pdf JSON-decode failure (lines 157-158)
# --------------------------------------------------------------------------- #
def test_ocr_pdf_invalid_json_output_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0-exit worker whose stdout is not JSON yields an actionable error."""
    import subprocess

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(ingestion_tools, "_ensure_ocr_dependency", lambda module: None)

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="not-json{", stderr="")

    monkeypatch.setattr(ingestion_tools.subprocess, "run", fake_run)

    with pytest.raises(IngestionError, match="invalid JSON output"):
        ingestion_tools._ocr_pdf(pdf)


# --------------------------------------------------------------------------- #
# _extract_pdf_with_ocr auto-fallback returns OCR text (line 174)
# --------------------------------------------------------------------------- #
def test_extract_pdf_with_ocr_auto_falls_back_to_ocr_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto mode: PyPDF text below the threshold triggers OCR; non-empty OCR wins."""
    monkeypatch.setattr(ingestion_tools, "_extract_pdf", lambda path: "")  # scanned: no text
    monkeypatch.setattr(ingestion_tools, "_ocr_pdf", lambda path: "RECOVERED VIA OCR")

    text, ocr_used = ingestion_tools._extract_pdf_with_ocr(Path("/tmp/x.pdf"), ocr="auto")

    assert text == "RECOVERED VIA OCR"
    assert ocr_used is True


def test_extract_pdf_with_ocr_auto_blank_ocr_keeps_pypdf_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto mode: when OCR also yields nothing, fall back to PyPDF's (empty) text."""
    monkeypatch.setattr(ingestion_tools, "_extract_pdf", lambda path: "")
    monkeypatch.setattr(ingestion_tools, "_ocr_pdf", lambda path: "   ")

    text, ocr_used = ingestion_tools._extract_pdf_with_ocr(Path("/tmp/x.pdf"), ocr="auto")

    assert text == ""
    assert ocr_used is False


# --------------------------------------------------------------------------- #
# _extract_pptx (lines 189, 195) -- fake python-pptx
# --------------------------------------------------------------------------- #
class _FakeShape:
    def __init__(self, text: str, *, has_text_frame: bool = True) -> None:
        self.text = text
        self.has_text_frame = has_text_frame


class _FakeSlide:
    def __init__(self, shapes: list[_FakeShape]) -> None:
        self.shapes = shapes


def test_extract_pptx_collects_slide_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slides with text become ``# Slide N`` blocks; blank/non-text shapes drop out."""
    slide_one = _FakeSlide(
        [
            _FakeShape("Eligibility"),
            _FakeShape("   "),  # blank -> filtered
            _FakeShape("not a text box", has_text_frame=False),  # no text frame -> filtered
            _FakeShape("Minimum score 620"),
        ]
    )
    slide_blank = _FakeSlide([_FakeShape("   ")])  # whole slide drops (line 195 false branch)
    slides = [slide_one, slide_blank]

    fake_presentation = types.SimpleNamespace(slides=slides)
    fake_pptx = types.SimpleNamespace(Presentation=lambda path: fake_presentation)
    monkeypatch.setattr(ingestion_tools, "_require", lambda module, fmt, **kw: fake_pptx)

    out = ingestion_tools._extract_pptx(Path("/tmp/deck.pptx"))

    assert out == "# Slide 1\nEligibility\nMinimum score 620"


# --------------------------------------------------------------------------- #
# _extract_xlsx (lines 204, 210) -- fake openpyxl
# --------------------------------------------------------------------------- #
class _FakeWorksheet:
    def __init__(self, title: str, rows: list[tuple[object, ...]]) -> None:
        self.title = title
        self._rows = rows

    def iter_rows(self, *, values_only: bool) -> list[tuple[object, ...]]:
        assert values_only is True
        return self._rows


class _FakeWorkbook:
    def __init__(self, worksheets: list[_FakeWorksheet]) -> None:
        self.worksheets = worksheets
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_extract_xlsx_renders_sheets_and_skips_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-empty rows render tab-separated under a sheet header; empty sheets drop."""
    sheet = _FakeWorksheet(
        "Rules",
        [("Rule", "Requirement"), ("Credit", 620), (None, None), ("", "  ")],
    )
    empty_sheet = _FakeWorksheet("Blank", [(None, None), ("", "")])  # line 210 false branch
    wb = _FakeWorkbook([sheet, empty_sheet])

    fake_openpyxl = types.SimpleNamespace(load_workbook=lambda path, **kw: wb)
    monkeypatch.setattr(ingestion_tools, "_require", lambda module, fmt, **kw: fake_openpyxl)

    out = ingestion_tools._extract_xlsx(Path("/tmp/book.xlsx"))

    assert out == "## Sheet: Rules\nRule\tRequirement\nCredit\t620"
    assert wb.closed is True


# --------------------------------------------------------------------------- #
# extract_document generic-exception wrap (lines 268-269)
# --------------------------------------------------------------------------- #
def test_extract_document_wraps_unexpected_extractor_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-IngestionError from an extractor is wrapped as IngestionError."""
    doc = tmp_path / "broken.txt"
    doc.write_text("anything")

    def boom(path: Path) -> str:
        raise ValueError("disk gremlin")

    monkeypatch.setitem(ingestion_tools._EXTRACTORS, "text", boom)

    with pytest.raises(IngestionError, match=r"failed to extract text 'broken.txt': disk gremlin"):
        ingestion_tools.extract_document(str(doc))


def test_extract_document_propagates_ingestion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IngestionError raised inside an extractor is NOT re-wrapped (line 267)."""
    doc = tmp_path / "doc.txt"
    doc.write_text("anything")

    def boom(path: Path) -> str:
        raise IngestionError("original message")

    monkeypatch.setitem(ingestion_tools._EXTRACTORS, "text", boom)

    with pytest.raises(IngestionError, match="^original message$"):
        ingestion_tools.extract_document(str(doc))


# --------------------------------------------------------------------------- #
# _attach_source size guard (line 293)
# --------------------------------------------------------------------------- #
class _RecordingSpan:
    def __init__(self) -> None:
        self.attached: list[tuple[str, bytes, str]] = []

    def attach(self, name: str, content_bytes: bytes, content_type: str) -> bool:
        self.attached.append((name, content_bytes, content_type))
        return True


def test_attach_source_skips_empty_file(tmp_path: Path) -> None:
    """A zero-byte file is below the size floor and is never attached."""
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    span = _RecordingSpan()

    ingestion_tools._attach_source(span, empty, "text")

    assert span.attached == []


def test_attach_source_skips_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over the attach byte cap is skipped without reading its bytes."""
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(ingestion_tools, "_MAX_ATTACH_BYTES", 1)  # force oversize
    span = _RecordingSpan()

    ingestion_tools._attach_source(span, big, "pdf")

    assert span.attached == []


def test_attach_source_attaches_within_bounds(tmp_path: Path) -> None:
    """A normal-sized file IS attached with the format-derived content type."""
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4\n%hello")
    span = _RecordingSpan()

    ingestion_tools._attach_source(span, doc, "pdf")

    assert len(span.attached) == 1
    name, content, content_type = span.attached[0]
    assert name == "source.pdf"
    assert content == b"%PDF-1.4\n%hello"
    assert content_type == "application/pdf"


# --------------------------------------------------------------------------- #
# _worker_exit + _run_ocr_worker_cli (lines 301-325)
# --------------------------------------------------------------------------- #
class _ExitCalledError(Exception):
    """Sentinel raised by the faked ``os._exit`` to halt without exiting."""

    def __init__(self, code: int) -> None:
        super().__init__(str(code))
        self.code = code


@pytest.fixture
def fake_exit(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace os._exit with a sentinel-raising stub and capture stdout/stderr."""
    captured: dict[str, object] = {"out": [], "err": []}

    def fake_os_exit(code: int) -> None:
        raise _ExitCalledError(code)

    monkeypatch.setattr(ingestion_tools.os, "_exit", fake_os_exit)
    monkeypatch.setattr(
        ingestion_tools.sys.stdout,
        "write",
        lambda s: captured["out"].append(s),  # type: ignore[union-attr]
    )
    monkeypatch.setattr(ingestion_tools.sys.stdout, "flush", lambda: None)
    monkeypatch.setattr(
        ingestion_tools.sys.stderr,
        "write",
        lambda s: captured["err"].append(s),  # type: ignore[union-attr]
    )
    monkeypatch.setattr(ingestion_tools.sys.stderr, "flush", lambda: None)
    return captured


def test_worker_exit_writes_stdout_and_exits(fake_exit: dict[str, object]) -> None:
    """``_worker_exit`` flushes stdout then exits with the given code."""
    with pytest.raises(_ExitCalledError) as caught:
        ingestion_tools._worker_exit(0, stdout='{"text": "ok"}')

    assert caught.value.code == 0
    assert fake_exit["out"] == ['{"text": "ok"}']


def test_worker_exit_appends_newline_to_stderr(fake_exit: dict[str, object]) -> None:
    """stderr without a trailing newline gets one appended before exit."""
    with pytest.raises(_ExitCalledError) as caught:
        ingestion_tools._worker_exit(1, stderr="boom")

    assert caught.value.code == 1
    assert fake_exit["err"] == ["boom", "\n"]


def test_worker_exit_keeps_existing_stderr_newline(fake_exit: dict[str, object]) -> None:
    """stderr already ending in a newline is not double-terminated."""
    with pytest.raises(_ExitCalledError):
        ingestion_tools._worker_exit(1, stderr="boom\n")

    assert fake_exit["err"] == ["boom\n"]


def test_run_ocr_worker_cli_success(
    fake_exit: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI runs the worker impl and exits 0 with a JSON ``{"text": ...}`` payload."""
    monkeypatch.setattr(
        ingestion_tools,
        "_ocr_pdf_worker_impl",
        lambda path, *, dpi, max_pages: f"OCR:{path.name}:{dpi}:{max_pages}",
    )

    argv = [
        ingestion_tools._OCR_WORKER_FLAG,
        "/tmp/scan.pdf",
        "--dpi",
        "300",
        "--max-pages",
        "7",
    ]
    with pytest.raises(_ExitCalledError) as caught:
        ingestion_tools._run_ocr_worker_cli(argv)

    assert caught.value.code == 0
    assert json.loads("".join(fake_exit["out"])) == {"text": "OCR:scan.pdf:300:7"}  # type: ignore[arg-type]


def test_run_ocr_worker_cli_missing_flag(
    fake_exit: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the worker flag the CLI exits 2 with a 'missing required flag' error."""
    # argparse stores the flag as False when absent; the impl must NOT be reached.
    called: dict[str, bool] = {"ran": False}

    def should_not_run(path: Path, *, dpi: int, max_pages: int) -> str:
        called["ran"] = True
        return ""

    monkeypatch.setattr(ingestion_tools, "_ocr_pdf_worker_impl", should_not_run)

    with pytest.raises(_ExitCalledError) as caught:
        ingestion_tools._run_ocr_worker_cli(["/tmp/scan.pdf"])

    assert caught.value.code == 2
    assert any("missing required flag" in s for s in fake_exit["err"])  # type: ignore[union-attr]
    assert called["ran"] is False


def test_run_ocr_worker_cli_worker_error_exits_1(
    fake_exit: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception in the worker impl is reported on stderr with exit 1."""

    def boom(path: Path, *, dpi: int, max_pages: int) -> str:
        raise RuntimeError("tesseract missing")

    monkeypatch.setattr(ingestion_tools, "_ocr_pdf_worker_impl", boom)

    with pytest.raises(_ExitCalledError) as caught:
        ingestion_tools._run_ocr_worker_cli([ingestion_tools._OCR_WORKER_FLAG, "/tmp/scan.pdf"])

    assert caught.value.code == 1
    assert any("RuntimeError: tesseract missing" in s for s in fake_exit["err"])  # type: ignore[union-attr]
