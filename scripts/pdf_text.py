"""Extracts raw text from a PDF with pypdf, for transcription and fidelity checks."""

from pathlib import Path

import pypdf

from src.config import DATA_DIR


def extract_text(pdf_path: Path) -> str:
    reader = pypdf.PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() for page in reader.pages)


if __name__ == "__main__":
    print(extract_text(DATA_DIR / "políticas_da_loja.pdf"))
