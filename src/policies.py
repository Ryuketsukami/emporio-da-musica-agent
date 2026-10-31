"""Splits the store's policy manual (data/policies.md) into sections by top-level heading."""

import re
from pathlib import Path

from src.config import DATA_DIR

SECTION_KEYS = {"1": "sobre", "2": "horario", "3": "pagamento", "4": "trocas_devolucoes", "5": "frete",
                "6": "promocoes", "7": "atendimento", "8": "garantia", "9": "privacidade", "10": "disposicoes"}
SERVED_SECTIONS = ("sobre", "horario", "pagamento", "trocas_devolucoes", "frete", "promocoes", "garantia",
                    "privacidade")

_HEADING_RE = re.compile(r"^## (\d+)\. ", re.MULTILINE)


def load_policies(path: Path = DATA_DIR / "policies.md") -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    matches = list(_HEADING_RE.finditer(text))
    sections = {}
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        key = SECTION_KEYS[match.group(1)]
        sections[key] = text[start:end].rstrip()
    return sections
