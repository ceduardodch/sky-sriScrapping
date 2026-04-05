"""Helpers de diagnóstico reutilizables para artefactos del scraper."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path


def slugify_label(label: str) -> str:
    normalized = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_").lower()
    return slug or "default"


def artifact_stem(target_date: date, tipo_label: str, stage: str) -> str:
    return f"{target_date.strftime('%Y%m%d')}_{slugify_label(tipo_label)}_{stage}"


def classify_payload(content_type: str, content_disposition: str, preview: str) -> str:
    content_type = (content_type or "").lower()
    content_disposition = (content_disposition or "").lower()
    preview = (preview or "").lstrip().lower()

    if "attachment" in content_disposition:
        return "attachment"
    if preview.startswith("{"):
        return "json"
    if "text/html" in content_type or preview.startswith("<!doctype") or preview.startswith("<html"):
        return "html"
    if "text/plain" in content_type:
        return "text"
    if "octet-stream" in content_type:
        return "binary"
    return "other"


def write_text_artifact(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="replace")


def write_json_artifact(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def persist_binary_artifact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
