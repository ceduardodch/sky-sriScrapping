"""
Parser de Claves de Acceso del SRI Ecuador.

Extrae y valida claves de 49 dígitos desde un archivo TXT descargado
del portal SRI (reporte de comprobantes electrónicos recibidos).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Regex: detecta cualquier secuencia de exactamente 49 dígitos
# delimitada por no-dígitos (o inicio/fin de línea)
CLAVE_PATTERN = re.compile(r"(?<!\d)(\d{49})(?!\d)")

TIPO_COMPROBANTE: dict[str, str] = {
    "01": "Factura",
    "03": "Liquidación de Compra",
    "04": "Nota de Crédito",
    "05": "Nota de Débito",
    "06": "Guía de Remisión",
    "07": "Comprobante de Retención",
}

AMBIENTE: dict[str, str] = {
    "1": "pruebas",
    "2": "produccion",
}


@dataclass
class ClaveDeAcceso:
    """Representación estructurada de una Clave de Acceso del SRI."""

    raw: str                        # Los 49 dígitos originales

    fecha_emision: date             # Fecha de emisión del comprobante
    tipo_comprobante: str           # Código "01", "03", etc.
    tipo_comprobante_desc: str      # Descripción legible
    ruc_emisor: str                 # RUC del proveedor (13 dígitos)
    ambiente: str                   # "pruebas" o "produccion"
    serie: str                      # Ej. "001001" (estab + pto emisión)
    secuencial: str                 # Ej. "000000123"
    codigo_numerico: str            # Código numérico de 9 dígitos
    digito_verificador: int         # Dígito 49 (calculado por Módulo 11)

    is_valid: bool = field(default=True)    # False si el checksum no coincide
    validation_error: Optional[str] = field(default=None)

    @property
    def numero_completo(self) -> str:
        """Retorna la serie y secuencial formateados: 001-001-000000123"""
        return f"{self.serie[:3]}-{self.serie[3:]}-{self.secuencial}"


def _compute_check_digit(clave_48: str) -> int:
    """
    Algoritmo Módulo 11 oficial del SRI Ecuador.

    Los factores 2,3,4,5,6,7 se aplican desde la derecha
    sobre los primeros 48 dígitos.
    """
    factors = [2, 3, 4, 5, 6, 7]
    total = sum(
        int(digit) * factors[i % 6]
        for i, digit in enumerate(reversed(clave_48))
    )
    remainder = total % 11

    if remainder == 0:
        return 0
    elif remainder == 1:
        return 1
    else:
        return 11 - remainder


def parse_clave(raw: str) -> ClaveDeAcceso:
    """
    Parsea y valida una clave de acceso de 49 dígitos.

    Estructura oficial (índices 0-based):
        [0:8]   fecha_emision    DDMMYYYY
        [8:10]  tipo_comprobante
        [10:23] ruc_emisor
        [23]    ambiente
        [24:30] serie
        [30:39] secuencial
        [39:48] codigo_numerico
        [48]    digito_verificador
    """
    raw = raw.strip()
    if len(raw) != 49 or not raw.isdigit():
        raise ValueError(f"Clave inválida: debe tener 49 dígitos numéricos, recibido '{raw[:20]}...'")

    # ── Parsear campos ────────────────────────────────────────────────────────
    fecha_str = raw[0:8]     # DDMMYYYY
    tipo = raw[8:10]
    ruc = raw[10:23]
    amb = raw[23]
    serie = raw[24:30]
    secuencial = raw[30:39]
    codigo_num = raw[39:48]
    check_received = int(raw[48])

    # ── Fecha ─────────────────────────────────────────────────────────────────
    try:
        dd, mm, yyyy = int(fecha_str[0:2]), int(fecha_str[2:4]), int(fecha_str[4:8])
        fecha = date(yyyy, mm, dd)
    except (ValueError, IndexError) as e:
        raise ValueError(f"Fecha inválida en clave '{raw[:8]}': {e}") from e

    # ── Validar dígito verificador ────────────────────────────────────────────
    check_expected = _compute_check_digit(raw[:48])
    is_valid = check_received == check_expected
    validation_error = None
    if not is_valid:
        validation_error = f"Módulo 11 fallido: esperado={check_expected}, recibido={check_received}"

    return ClaveDeAcceso(
        raw=raw,
        fecha_emision=fecha,
        tipo_comprobante=tipo,
        tipo_comprobante_desc=TIPO_COMPROBANTE.get(tipo, f"Tipo desconocido ({tipo})"),
        ruc_emisor=ruc,
        ambiente=AMBIENTE.get(amb, f"Ambiente desconocido ({amb})"),
        serie=serie,
        secuencial=secuencial,
        codigo_numerico=codigo_num,
        digito_verificador=check_received,
        is_valid=is_valid,
        validation_error=validation_error,
    )


def extract_claves_from_text(text: str) -> list[ClaveDeAcceso]:
    """
    Extrae todas las Claves de Acceso válidas de un string.

    Usa regex para encontrar secuencias de 49 dígitos sin importar
    el delimitador (tabs, comas, espacios, saltos de línea).
    """
    candidates = CLAVE_PATTERN.findall(text)
    results: list[ClaveDeAcceso] = []

    for raw in candidates:
        try:
            clave = parse_clave(raw)
            if not clave.is_valid:
                log.warning(
                    "clave_checksum_invalid",
                    raw_prefix=raw[:15],
                    error=clave.validation_error,
                )
            results.append(clave)
        except ValueError as e:
            log.warning("clave_parse_failed", raw_prefix=raw[:15], error=str(e))

    return results


def extract_claves_from_file(path: Path) -> list[ClaveDeAcceso]:
    """
    Lee un archivo TXT del SRI y extrae todas las Claves de Acceso.

    Maneja encodings UTF-8, Latin-1 y archivos con BOM.
    """
    # Intentar UTF-8 primero, luego Latin-1 como fallback
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = path.read_text(encoding=encoding, errors="replace")
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = path.read_bytes().decode("latin-1", errors="replace")

    log.info("file_read", path=str(path), size_bytes=path.stat().st_size)

    claves = extract_claves_from_text(text)
    valid_count = sum(1 for c in claves if c.is_valid)

    log.info(
        "parse_complete",
        total=len(claves),
        valid=valid_count,
        invalid=len(claves) - valid_count,
    )

    return claves
