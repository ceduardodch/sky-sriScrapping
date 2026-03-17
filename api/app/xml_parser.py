"""
Parser de XMLs de comprobantes electrónicos SRI Ecuador.

Soporta todos los tipos de comprobante:
  01 → Factura             (<factura>)
  03 → Liquidación compra  (<liquidacionCompra>)
  04 → Nota de Crédito     (<notaCredito>)
  05 → Nota de Débito      (<notaDebito>)
  06 → Guía de Remisión    (<guiaRemision>)
  07 → Retención           (<comprobanteRetencion>)
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from lxml import etree

# Raíz XML → código de tipo de comprobante SRI
_COD_DOC: dict[str, str] = {
    "factura": "01",
    "liquidacionCompra": "03",
    "notaCredito": "04",
    "notaDebito": "05",
    "guiaRemision": "06",
    "comprobanteRetencion": "07",
}


def _txt(el: etree._Element | None, tag: str) -> str | None:
    if el is None:
        return None
    found = el.find(tag)
    if found is None or not found.text:
        return None
    return found.text.strip() or None


def _dec(el: etree._Element | None, tag: str) -> float | None:
    val = _txt(el, tag)
    if val is None:
        return None
    try:
        return float(Decimal(val))
    except InvalidOperation:
        return None


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        # SRI usa dd/MM/yyyy
        from dateutil.parser import parse as _parse
        return _parse(s, dayfirst=True).date()
    except Exception:
        return None


def _parse_detalles(root: etree._Element) -> list[dict[str, Any]]:
    """Extrae líneas de detalles para facturas y liquidaciones."""
    detalles_el = root.find("detalles")
    if detalles_el is None:
        return []
    result = []
    for det in detalles_el.findall("detalle"):
        item: dict[str, Any] = {
            "codigo": _txt(det, "codigoPrincipal") or _txt(det, "codigoAuxiliar"),
            "descripcion": _txt(det, "descripcion"),
            "cantidad": _dec(det, "cantidad"),
            "precio_unitario": _dec(det, "precioUnitario"),
            "descuento": _dec(det, "descuento"),
            "subtotal": _dec(det, "precioTotalSinImpuesto"),
        }
        # IVA del detalle
        imp_el = det.find("impuestos/impuesto")
        if imp_el is not None:
            item["iva_tarifa"] = _dec(imp_el, "tarifa")
            item["iva_valor"] = _dec(imp_el, "valor")
        result.append(item)
    return result


def _parse_retenciones(root: etree._Element) -> list[dict[str, Any]]:
    """Extrae impuestos retenidos para comprobanteRetencion."""
    impuestos_el = root.find("impuestos")
    if impuestos_el is None:
        return []
    result = []
    for imp in impuestos_el.findall("impuesto"):
        result.append({
            "codigo": _txt(imp, "codigo"),
            "codigoPorcentaje": _txt(imp, "codigoPorcentaje"),
            "descripcion": _txt(imp, "descripcionImpuesto"),
            "baseImponible": _dec(imp, "baseImponible"),
            "porcentajeRetener": _dec(imp, "porcentajeRetener"),
            "valorRetenido": _dec(imp, "valorRetenido"),
        })
    return result


def parse_xml(xml_str: str) -> dict[str, Any]:
    """
    Parsea un XML SRI y retorna un dict listo para insertar en DB.

    Returns dict con claves: tipo_comprobante, cod_doc, ruc_emisor,
    razon_social_emisor, nombre_comercial, estab, pto_emi, secuencial,
    identificacion_receptor, razon_social_receptor, fecha_emision,
    total_sin_impuestos, iva, importe_total, detalles.
    """
    try:
        root = etree.fromstring(xml_str.encode("utf-8"))
    except etree.XMLSyntaxError:
        # Intentar limpiar namespace
        xml_str_clean = xml_str.split("?>", 1)[-1] if "?>" in xml_str else xml_str
        root = etree.fromstring(xml_str_clean.encode("utf-8"))

    # Tag sin namespace
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    cod_doc = _COD_DOC.get(tag, "")
    tipo_comprobante = tag

    # ── infoTributaria (común a todos) ────────────────────────────────────────
    info_trib = root.find("infoTributaria")
    ruc_emisor = _txt(info_trib, "ruc")
    razon_social_emisor = _txt(info_trib, "razonSocial")
    nombre_comercial = _txt(info_trib, "nombreComercial")
    estab = _txt(info_trib, "estab")
    pto_emi = _txt(info_trib, "ptoEmi")
    secuencial = _txt(info_trib, "secuencial")

    # ── Bloque de info específico por tipo ────────────────────────────────────
    info = (
        root.find("infoFactura")
        or root.find("infoNotaCredito")
        or root.find("infoNotaDebito")
        or root.find("infoGuiaRemision")
        or root.find("infoCompRetencion")
        or root.find("infoLiquidacionCompra")
    )

    fecha_emision = _parse_date(_txt(info, "fechaEmision"))

    # Receptor
    identificacion_receptor = (
        _txt(info, "identificacionComprador")
        or _txt(info, "identificacionSujetoRetenido")
        or _txt(info, "rucTransportista")
    )
    razon_social_receptor = (
        _txt(info, "razonSocialComprador")
        or _txt(info, "razonSocialSujetoRetenido")
    )

    # Totales
    total_sin_impuestos = _dec(info, "totalSinImpuestos") or _dec(info, "totalBaseImponible")
    importe_total = _dec(info, "importeTotal") or _dec(info, "totalComprobantesRetenidos")

    # IVA: buscar en totalConImpuestos
    iva: float | None = None
    total_imp_el = root.find(".//totalConImpuestos")
    if total_imp_el is not None:
        for imp in total_imp_el.findall("totalImpuesto"):
            codigo = _txt(imp, "codigo")
            if codigo == "2":  # IVA
                iva = _dec(imp, "valor")
                break

    # ── Detalles / líneas ─────────────────────────────────────────────────────
    if tag == "comprobanteRetencion":
        detalles = _parse_retenciones(root)
    else:
        detalles = _parse_detalles(root)

    return {
        "tipo_comprobante": tipo_comprobante,
        "cod_doc": cod_doc,
        "ruc_emisor": ruc_emisor,
        "razon_social_emisor": razon_social_emisor,
        "nombre_comercial": nombre_comercial,
        "estab": estab,
        "pto_emi": pto_emi,
        "secuencial": secuencial,
        "identificacion_receptor": identificacion_receptor,
        "razon_social_receptor": razon_social_receptor,
        "fecha_emision": fecha_emision,
        "total_sin_impuestos": total_sin_impuestos,
        "iva": iva,
        "importe_total": importe_total,
        "detalles": detalles,
    }
