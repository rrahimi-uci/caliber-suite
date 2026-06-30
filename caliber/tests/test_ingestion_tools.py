"""Tests for multi-format document ingestion (extract_document)."""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from caliber.workflows import ingestion_tools
from caliber.workflows.ingestion_tools import IngestionError, extract_document

SAMPLE = "Minimum credit score is 620. The maximum DTI ratio is 50 percent."
OCR_TEXT = "MORTGAGE CREDIT SCORE 620"


def _make_scanned_pdf(path: Path, text: str) -> None:
    """Render text onto a white image and save it as an image-only PDF — i.e. a
    PDF with NO text layer, so PyPDF extracts nothing and the OCR path engages."""
    image = pytest.importorskip("PIL.Image")
    image_draw = pytest.importorskip("PIL.ImageDraw")
    image_font = pytest.importorskip("PIL.ImageFont")
    img = image.new("RGB", (1600, 480), "white")
    image_draw.Draw(img).text((40, 60), text, fill="black", font=image_font.load_default(size=64))
    img.save(str(path), "PDF")


def _skip_without_ocr_stack() -> None:
    for module in ("fitz", "pytesseract", "PIL.Image"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"{module} not installed")


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict = {}
        self.inputs: dict | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_inputs(self, value: dict) -> None:
        self.inputs = value


class _SpanCM:
    def __init__(self, span: _RecordingSpan) -> None:
        self._span = span

    def __enter__(self) -> _RecordingSpan:
        return self._span

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeMlflow:
    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    def start_span(self, *, name: str, span_type: str, attributes: dict | None = None) -> _SpanCM:
        span = _RecordingSpan()
        self.spans.append(span)
        return _SpanCM(span)


def test_extract_document_attaches_source_to_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multimodal tracing: the source document is attached to the span."""
    from caliber.observability.mlflow_tracing import Tracer, get_tracer, set_tracer

    fake = _FakeMlflow()
    previous = get_tracer()
    set_tracer(Tracer(mlflow_module=fake))
    try:
        doc = tmp_path / "report.txt"
        doc.write_text(SAMPLE)
        out = extract_document(str(doc))
    finally:
        set_tracer(previous)

    assert out["format"] == "text"
    span = fake.spans[0]
    # Source bytes attached as a span input + a text marker for the viewer.
    assert "source.txt" in (span.inputs or {})
    assert span.attributes.get("caliber.attachment.source.txt") == "text/plain"
    assert span.attributes.get("caliber.chars") == out["chars"]


def test_text_and_markdown(tmp_path: Path) -> None:
    txt = tmp_path / "d.txt"
    txt.write_text(SAMPLE)
    out = extract_document(str(txt))
    assert out["format"] == "text"
    assert "620" in out["text"] and out["chars"] > 10

    md = tmp_path / "d.md"
    md.write_text("# Title\n\n**" + SAMPLE + "**\n")
    out_md = extract_document(str(md))
    assert out_md["format"] == "markdown"
    assert "620" in out_md["text"]
    assert "#" not in out_md["text"] and "**" not in out_md["text"]  # markdown stripped


def test_missing_file_raises() -> None:
    with pytest.raises(IngestionError, match="not found"):
        extract_document("/no/such/file.pdf")


def test_unknown_extension_reads_as_text(tmp_path: Path) -> None:
    f = tmp_path / "d.rst"
    f.write_text(SAMPLE)
    assert extract_document(str(f))["format"] == "text"


def test_truncation(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("word " * 1000)
    out = extract_document(str(f), max_chars=100)
    assert out["truncated"] is True and len(out["text"]) == 100


def test_pdf(tmp_path: Path) -> None:
    fpdf = pytest.importorskip("fpdf")
    pytest.importorskip("pypdf")
    pdf = fpdf.FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(pdf.epw, 6, SAMPLE)
    path = tmp_path / "d.pdf"
    pdf.output(str(path))
    out = extract_document(str(path))
    assert out["format"] == "pdf" and "620" in out["text"]


def test_pptx(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Eligibility"
    slide.placeholders[1].text = SAMPLE
    path = tmp_path / "d.pptx"
    prs.save(str(path))
    out = extract_document(str(path))
    assert out["format"] == "pptx" and "620" in out["text"]


def test_xlsx(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Rule", "Requirement"])
    ws.append(["Credit", SAMPLE])
    path = tmp_path / "d.xlsx"
    wb.save(str(path))
    out = extract_document(str(path))
    assert out["format"] == "xlsx" and "620" in out["text"]


def test_docx(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph(SAMPLE)
    path = tmp_path / "d.docx"
    document.save(str(path))
    out = extract_document(str(path))
    assert out["format"] == "docx" and "620" in out["text"]


def test_text_pdf_does_not_ocr(tmp_path: Path) -> None:
    """A normal text PDF extracts via PyPDF and never triggers the OCR fallback."""
    fpdf = pytest.importorskip("fpdf")
    pytest.importorskip("pypdf")
    pdf = fpdf.FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(pdf.epw, 6, SAMPLE)
    path = tmp_path / "text.pdf"
    pdf.output(str(path))
    out = extract_document(str(path))
    assert out["format"] == "pdf" and "620" in out["text"]
    assert out["ocr_used"] is False


def test_ocr_worker_subprocess_returns_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(ingestion_tools, "_ensure_ocr_dependency", lambda module: None)

    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"text": OCR_TEXT}),
            stderr="",
        )

    monkeypatch.setattr(ingestion_tools.subprocess, "run", fake_run)

    text = ingestion_tools._ocr_pdf(pdf)

    assert text == OCR_TEXT
    command = seen["cmd"]
    assert isinstance(command, list)
    assert command[1] == str(Path(ingestion_tools.__file__).resolve())
    assert "--caliber-ocr-worker" in command


def test_ocr_worker_subprocess_failure_raises_actionable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(ingestion_tools, "_ensure_ocr_dependency", lambda module: None)

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="RuntimeError: boom")

    monkeypatch.setattr(ingestion_tools.subprocess, "run", fake_run)

    with pytest.raises(IngestionError, match="scanned-PDF OCR worker failed: RuntimeError: boom"):
        ingestion_tools._ocr_pdf(pdf)


def test_pdf_ocr_auto_on_scanned_pdf(tmp_path: Path) -> None:
    _skip_without_ocr_stack()
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract binary not installed")
    assert "fitz" not in sys.modules
    assert "pymupdf" not in sys.modules
    path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(path, OCR_TEXT)
    out = extract_document(str(path))
    assert out["format"] == "pdf"
    assert out["ocr_used"] is True
    upper = out["text"].upper()
    assert any(tok in upper for tok in ("MORTGAGE", "CREDIT", "SCORE", "620"))
    assert "fitz" not in sys.modules
    assert "pymupdf" not in sys.modules


def test_pdf_ocr_always(tmp_path: Path) -> None:
    _skip_without_ocr_stack()
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract binary not installed")
    assert "fitz" not in sys.modules
    assert "pymupdf" not in sys.modules
    path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(path, OCR_TEXT)
    out = extract_document(str(path), ocr="always")
    assert out["ocr_used"] is True
    assert any(tok in out["text"].upper() for tok in ("MORTGAGE", "CREDIT", "SCORE", "620"))
    assert "fitz" not in sys.modules
    assert "pymupdf" not in sys.modules


def test_pdf_ocr_never_returns_no_text(tmp_path: Path) -> None:
    """``ocr="never"`` returns PyPDF's (empty) text for a scanned PDF, no fallback."""
    pytest.importorskip("pypdf")
    path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(path, OCR_TEXT)
    out = extract_document(str(path), ocr="never")
    assert out["format"] == "pdf"
    assert out["ocr_used"] is False
    assert out["text"].strip() == ""


def test_invalid_ocr_mode_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(IngestionError, match="invalid ocr mode"):
        extract_document(str(path), ocr="bogus")


def test_repo_source_never_imports_pymupdf_directly() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source_root = repo_root / "src" / "caliber"
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in {"fitz", "pymupdf"}:
                        offenders.append(str(path.relative_to(repo_root)))
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".", 1)[0] in {
                "fitz",
                "pymupdf",
            }:
                offenders.append(str(path.relative_to(repo_root)))
    assert offenders == []
