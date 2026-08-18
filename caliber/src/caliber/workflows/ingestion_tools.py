"""Multi-format document ingestion for workflow pipelines.

Extracts plain text from PDF / DOCX / PPTX / XLSX / Markdown / plain-text files.
This is a *registered function tool* (real in-process callable) rather than a
``python_code`` node body, because the python_code sandbox blocks ``import`` and
file I/O — so binary parsing (PyPDF, python-pptx, openpyxl, …) cannot run there.

Parser libraries are imported lazily and are an optional extra
(``caliber-suite[ingest]``: ``pypdf``, ``python-pptx``, ``openpyxl``,
``python-docx``, ``markdown``). A missing library yields a clear error for that
format only; plain text / markdown always work.

Scanned / image-only PDFs (no extractable text layer) fall back to OCR when the
``caliber-suite[ocr]`` extra is installed (``pymupdf`` rasterizes each page,
``pytesseract`` runs the system ``tesseract`` binary). The fallback is automatic
(``ocr="auto"``): it only fires when PyPDF returns little/no text, so text-based
PDFs never pay the OCR cost.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A PDF whose PyPDF text is shorter than this (after strip) is treated as
# scanned/image-only and routed to the OCR fallback in ``ocr="auto"`` mode.
_MIN_PDF_TEXT_CHARS = 16

# Multimodal tracing (MLflow 3.12+): attach the source document to the
# extraction span so the trace UI renders the actual file the KG/OCR pipeline
# processed. Capped so a giant scan doesn't bloat every trace.
_MAX_ATTACH_BYTES = 16 * 1024 * 1024
_FORMAT_CONTENT_TYPE: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "markdown": "text/markdown",
    "text": "text/plain",
}
_OCR_MODES = ("auto", "always", "never")
_OCR_WORKER_FLAG = "--caliber-ocr-worker"

# Extension -> logical format.
_EXT_FORMAT: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".text": "text",
    ".csv": "text",
}


class IngestionError(RuntimeError):
    """Document could not be read or parsed."""


def _require(module: str, fmt: str, *, extra: str = "ingest") -> Any:
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise IngestionError(
            f"{fmt} ingestion needs the optional dependency {module!r}; "
            f"install caliber-suite[{extra}]"
        ) from exc


def _extract_pdf(path: Path) -> str:
    pypdf = _require("pypdf", "pdf")
    reader = pypdf.PdfReader(str(path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(p.strip() for p in pages if p.strip())


def _ensure_ocr_dependency(module: str) -> None:
    if importlib.util.find_spec(module) is None:
        raise IngestionError(
            f"scanned-PDF OCR needs the optional dependency {module!r}; install caliber-suite[ocr]"
        )


def _ocr_pdf_worker_impl(path: Path, *, dpi: int = 220, max_pages: int = 100) -> str:
    """Run OCR inline inside the dedicated worker process.

    Keep the PyMuPDF import scoped to this worker only. On Python 3.14/macOS we
    observed ``pymupdf._extra.so`` crash during interpreter finalization even
    after successful OCR work, so the worker exits via ``os._exit()`` after it
    flushes its JSON payload back to the parent.
    """
    # Import the ``pymupdf`` module name, not the deprecated ``fitz`` alias: newer
    # PyMuPDF releases print a deprecation notice to stdout on ``import fitz``,
    # which corrupts this worker's JSON-on-stdout contract with the parent.
    pymupdf = _require("pymupdf", "scanned-PDF OCR", extra="ocr")
    pytesseract = _require("pytesseract", "scanned-PDF OCR", extra="ocr")
    pil_image = _require("PIL.Image", "scanned-PDF OCR", extra="ocr").Image
    out: list[str] = []
    doc = pymupdf.open(str(path))
    try:
        for page in doc:
            if len(out) >= max_pages:
                break
            png = page.get_pixmap(dpi=dpi).tobytes("png")
            page_text = pytesseract.image_to_string(pil_image.open(io.BytesIO(png))).strip()
            if page_text:
                out.append(page_text)
    finally:
        doc.close()
    return "\n\n".join(out)


def _ocr_worker_command(path: Path, *, dpi: int, max_pages: int) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        _OCR_WORKER_FLAG,
        str(path),
        "--dpi",
        str(dpi),
        "--max-pages",
        str(max_pages),
    ]


def _ocr_pdf(path: Path, *, dpi: int = 220, max_pages: int = 100) -> str:
    """OCR a scanned/image PDF: rasterize each page (PyMuPDF) and read it
    (Tesseract). Needs the ``[ocr]`` extra + the system ``tesseract`` binary."""
    _ensure_ocr_dependency("pymupdf")
    _ensure_ocr_dependency("pytesseract")
    _ensure_ocr_dependency("PIL.Image")
    completed = subprocess.run(  # noqa: S603 - argv built internally (no shell), interpreter + worker path are trusted
        _ocr_worker_command(path, dpi=dpi, max_pages=max_pages),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"worker exited with status {completed.returncode}"
        )
        raise IngestionError(f"scanned-PDF OCR worker failed: {detail}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise IngestionError("scanned-PDF OCR worker returned invalid JSON output") from exc
    return str(payload.get("text") or "")


def _extract_pdf_with_ocr(path: Path, *, ocr: str) -> tuple[str, bool]:
    """Extract PDF text, falling back to OCR per the ``ocr`` mode.

    Returns ``(text, ocr_used)``. ``auto`` (default) OCRs only when PyPDF yields
    < ``_MIN_PDF_TEXT_CHARS`` (a scanned/image PDF); ``always`` forces OCR;
    ``never`` returns PyPDF's text as-is even when empty.
    """
    if ocr not in _OCR_MODES:
        raise IngestionError(f"invalid ocr mode {ocr!r}; expected one of {list(_OCR_MODES)}")
    text = "" if ocr == "always" else _extract_pdf(path)
    if ocr == "always" or (ocr == "auto" and len(text.strip()) < _MIN_PDF_TEXT_CHARS):
        ocr_text = _ocr_pdf(path)
        if ocr_text.strip():
            return ocr_text, True
    return text, False


def _extract_docx(path: Path) -> str:
    docx = _require("docx", "docx")
    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())


def _extract_pptx(path: Path) -> str:
    pptx = _require("pptx", "pptx")
    presentation = pptx.Presentation(str(path))
    out: list[str] = []
    for idx, slide in enumerate(presentation.slides, start=1):
        lines = [
            shape.text.strip()
            for shape in slide.shapes
            if getattr(shape, "has_text_frame", False) and shape.text.strip()
        ]
        if lines:
            out.append(f"# Slide {idx}\n" + "\n".join(lines))
    return "\n\n".join(out)


def _extract_xlsx(path: Path) -> str:
    openpyxl = _require("openpyxl", "xlsx")
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out: list[str] = []
    for ws in wb.worksheets:
        rows = [
            "\t".join("" if c is None else str(c) for c in row)
            for row in ws.iter_rows(values_only=True)
        ]
        rows = [r for r in rows if r.strip()]
        if rows:
            out.append(f"## Sheet: {ws.title}\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(out)


def _extract_markdown(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Strip the most common markdown syntax so the LLM sees clean prose, while
    # keeping headings/lists readable.
    text = re.sub(r"^#{1,6}\s*", "", raw, flags=re.MULTILINE)  # headings
    text = re.sub(
        r"\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`", lambda m: next(filter(None, m.groups())), text
    )
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links -> label


def _extract_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


_EXTRACTORS = {
    "pdf": _extract_pdf,
    "docx": _extract_docx,
    "pptx": _extract_pptx,
    "xlsx": _extract_xlsx,
    "markdown": _extract_markdown,
    "text": _extract_text,
}


def extract_document(ref: str, *, max_chars: int = 200_000, ocr: str = "auto") -> dict[str, Any]:
    """Extract plain text from a document by path, dispatching on its extension.

    Returns ``{"text", "format", "chars", "truncated", "source", "ocr_used"}``.
    Raises :class:`IngestionError` on a missing/unreadable file or a missing
    parser library. Unknown extensions are read as UTF-8 text (best-effort).

    ``ocr`` (``"auto"`` | ``"always"`` | ``"never"``) controls the scanned-PDF
    fallback; it applies only to PDFs (ignored for other formats).
    """
    path = Path(str(ref).strip())
    if not path.is_file():
        raise IngestionError(f"document not found: {ref!r}")
    fmt = _EXT_FORMAT.get(path.suffix.lower(), "text")

    from caliber.observability.mlflow_tracing import get_tracer  # noqa: PLC0415

    with get_tracer().span("tool.extract_document", attributes={"format": fmt}) as span:
        _attach_source(span, path, fmt)
        ocr_used = False
        try:
            if fmt == "pdf":
                text, ocr_used = _extract_pdf_with_ocr(path, ocr=ocr)
            else:
                text = _EXTRACTORS[fmt](path)
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError(f"failed to extract {fmt} {path.name!r}: {exc}") from exc
        text = text.strip()
        truncated = len(text) > max_chars
        span.set_attribute("caliber.chars", len(text))
        span.set_attribute("caliber.ocr_used", ocr_used)
        return {
            "text": text[:max_chars],
            "format": fmt,
            "chars": len(text),
            "truncated": truncated,
            "source": path.name,
            "ocr_used": ocr_used,
        }


def _attach_source(span: Any, path: Path, fmt: str) -> None:
    """Attach the source document to the extraction span (best-effort).

    No-ops silently when tracing is inert, the file is too large, or it can't be
    read — observability must never break extraction.
    """
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_ATTACH_BYTES:
            return
        content_type = _FORMAT_CONTENT_TYPE.get(fmt, "application/octet-stream")
        span.attach(f"source.{path.suffix.lstrip('.') or 'bin'}", path.read_bytes(), content_type)
    except Exception:  # pragma: no cover - defensive; attach is already guarded
        logger.debug("failed attaching source %s to extraction span", path.name, exc_info=True)


def _worker_exit(code: int, *, stdout: str = "", stderr: str = "") -> None:
    if stdout:
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
    os._exit(code)


def _run_ocr_worker_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="ingestion_tools OCR worker")
    parser.add_argument(_OCR_WORKER_FLAG, action="store_true")
    parser.add_argument("path")
    parser.add_argument("--dpi", type=int, default=220)
    parser.add_argument("--max-pages", type=int, default=100)
    args = parser.parse_args(argv)
    if not getattr(args, _OCR_WORKER_FLAG.lstrip("-").replace("-", "_")):
        _worker_exit(2, stderr=f"missing required flag {_OCR_WORKER_FLAG}")
    try:
        text = _ocr_pdf_worker_impl(Path(args.path), dpi=args.dpi, max_pages=args.max_pages)
    except Exception as exc:
        _worker_exit(1, stderr=f"{type(exc).__name__}: {exc}")
    _worker_exit(0, stdout=json.dumps({"text": text}, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess worker
    _run_ocr_worker_cli(sys.argv[1:])
