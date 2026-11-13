#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

if ! .venv/bin/python -c "import fastapi, docx, reportlab, pypdf, PIL, pytesseract, bs4, httpx" >/dev/null 2>&1; then
  .venv/bin/pip install -r requirements.txt
fi

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Note: tesseract is not installed, so photo CVs will not OCR. PDF, Word, and text still work." >&2
fi

exec .venv/bin/python -m safu
