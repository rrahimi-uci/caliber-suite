#!/usr/bin/env python
"""Run pytest and exit via ``os._exit`` to dodge a PyMuPDF/Python-3.14 crash.

On Python 3.14, PyMuPDF's SWIG ``_extra.so`` segfaults in
``SWIG_Python_DestroyModule`` during ``Py_Finalize`` once the OCR/ingest tests
have opened a document (it is fine until the interpreter tears the module
down). The crash fires *after* pytest has run every test, evaluated
``--cov-fail-under``, and written its reports — so it corrupts only the
process exit code (SIGSEGV → 139), never the results. A bare ``import
pymupdf`` exits cleanly; it takes a full-suite run's worth of document
handling to trip the finaliser.

Running pytest in-process and terminating with ``os._exit(code)`` skips the
crashy interpreter teardown entirely. The exit code returned by
``pytest.main()`` is already final — it reflects test failures *and* the
coverage gate — and pytest-cov flushes coverage data + reports during the
session (not in an ``atexit`` handler), so short-circuiting finalisation
loses nothing that matters.

This is the entry point behind ``make test``. CI is unaffected: it runs on
Python 3.10-3.12 with the ``[dev]`` extra only (no PyMuPDF) and invokes
``pytest`` directly, so it never reaches this code path.
"""

from __future__ import annotations

import os
import sys

import pytest


def main() -> None:
    code = pytest.main(sys.argv[1:])
    # os._exit skips atexit + buffer flushing; flush the streams ourselves so
    # no captured output is lost when we bypass the normal interpreter exit.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(code))


if __name__ == "__main__":
    main()
