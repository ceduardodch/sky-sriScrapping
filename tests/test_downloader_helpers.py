import asyncio
from datetime import date

import pytest

from sri_scraper.diagnostics import artifact_stem, classify_payload, slugify_label
from sri_scraper.downloader import (
    _filters_match_target_date,
    _is_empty_result,
    _tipo_matches_current,
)
from sri_scraper.exceptions import CaptchaChallengeError


def test_slugify_label_removes_accents_and_symbols():
    assert slugify_label("Notas de Débito / crédito") == "notas_de_debito_credito"


def test_artifact_stem_includes_date_slug_and_stage():
    assert artifact_stem(date(2026, 3, 24), "Comprobante de Retención", "post_consultar") == (
        "20260324_comprobante_de_retencion_post_consultar"
    )


def test_classify_payload_detects_attachment():
    assert classify_payload("text/plain", "attachment; filename=reporte.txt", "123456") == "attachment"


def test_classify_payload_detects_html_preview():
    assert classify_payload("text/plain", "", "<!DOCTYPE html><html>") == "html"


class _FakeLocator:
    def __init__(self, visible: bool = False, text: str = "") -> None:
        self._visible = visible
        self._text = text
        self.first = self

    async def is_visible(self, timeout: int | None = None) -> bool:
        return self._visible

    async def inner_text(self) -> str:
        return self._text


class _FakePage:
    def __init__(self, *, visible_text: str = "", selectors: dict[str, _FakeLocator] | None = None) -> None:
        self._visible_text = visible_text
        self._selectors = selectors or {}

    async def evaluate(self, script: str) -> str:
        if "document.body.innerText.toLowerCase()" in script:
            return self._visible_text.lower()
        raise AssertionError(f"Unexpected script: {script}")

    def locator(self, selector: str) -> _FakeLocator:
        return self._selectors.get(selector, _FakeLocator())


def test_is_empty_result_true_for_empty_keyword_text() -> None:
    page = _FakePage(visible_text="No existen datos para los parámetros seleccionados")

    assert asyncio.run(_is_empty_result(page)) is True


def test_is_empty_result_raises_for_persistent_captcha() -> None:
    page = _FakePage(
        selectors={
            "text=Captcha incorrecta": _FakeLocator(visible=True),
        }
    )

    with pytest.raises(CaptchaChallengeError, match="Captcha incorrecta persistente"):
        asyncio.run(_is_empty_result(page))


def test_filters_match_target_date_when_filters_already_match() -> None:
    current = {
        "year_value": "2026",
        "month_label": "Abril",
        "day_value": "3",
    }

    assert _filters_match_target_date(current, date(2026, 4, 3)) is True


def test_filters_match_target_date_false_when_any_filter_differs() -> None:
    current = {
        "year_value": "2026",
        "month_label": "Abril",
        "day_value": "4",
    }

    assert _filters_match_target_date(current, date(2026, 4, 3)) is False


def test_tipo_matches_current_by_value() -> None:
    current = {
        "tipo_value": "1",
        "tipo_label": "Factura",
    }

    assert _tipo_matches_current(current, "1", "Factura") is True


def test_tipo_matches_current_by_label_when_value_missing() -> None:
    current = {
        "tipo_value": "",
        "tipo_label": "Notas de Crédito",
    }

    assert _tipo_matches_current(current, "", "Notas de Crédito") is True
