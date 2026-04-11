"""
Tests unitarios para sri_scraper/soap_client.py.

NO realizan llamadas reales al WS del SRI — usan unittest.mock para
simular las respuestas de zeep y verificar la lógica de parsing y guardado.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sri_scraper.soap_client import (
    AutorizacionResult,
    _parse_respuesta,
    save_xml,
)


# ── Fixtures de respuesta zeep (objetos SimpleNamespace) ──────────────────────

def _make_autorizacion(
    estado: str = "AUTORIZADO",
    numero: str = "2302202600000000000000000000000000000000001",
    fecha: str = "2026-03-09T09:09:22",
    ambiente: str = "PRODUCCION",
    comprobante: str = "<factura><id>TEST</id></factura>",
    mensajes: list | None = None,
) -> SimpleNamespace:
    """Construye un objeto que imita la respuesta zeep de autorizacionComprobante."""
    msgs_ns = SimpleNamespace(mensaje=[]) if not mensajes else SimpleNamespace(
        mensaje=[
            SimpleNamespace(
                identificador=m.get("identificador", ""),
                mensaje=m.get("mensaje", ""),
                informacionAdicional=m.get("informacionAdicional", ""),
                tipo=m.get("tipo", ""),
            )
            for m in mensajes
        ]
    )

    auth = SimpleNamespace(
        estado=estado,
        numeroAutorizacion=numero,
        fechaAutorizacion=fecha,
        ambiente=ambiente,
        comprobante=comprobante,
        mensajes=msgs_ns,
    )
    return SimpleNamespace(
        claveAccesoConsultada="0903202601171219926200120029020000161150002018312",
        numeroComprobantes="1",
        autorizaciones=SimpleNamespace(autorizacion=[auth]),
    )


CLAVE_REAL = "0903202601171219926200120029020000161150002018312"
XML_SAMPLE = "<factura><infoTributaria><claveAcceso>0903202601171219926200120029020000161150002018312</claveAcceso></infoTributaria></factura>"


# ── Tests de AutorizacionResult ────────────────────────────────────────────────

class TestAutorizacionResult:
    def test_autorizado_true_cuando_estado_autorizado(self):
        r = AutorizacionResult(clave_acceso=CLAVE_REAL, estado="AUTORIZADO")
        assert r.autorizado is True

    def test_autorizado_false_cuando_no_autorizado(self):
        r = AutorizacionResult(clave_acceso=CLAVE_REAL, estado="NO AUTORIZADO")
        assert r.autorizado is False

    def test_tiene_xml_true_cuando_hay_xml(self):
        r = AutorizacionResult(clave_acceso=CLAVE_REAL, estado="AUTORIZADO", xml_comprobante=XML_SAMPLE)
        assert r.tiene_xml is True

    def test_tiene_xml_false_cuando_none(self):
        r = AutorizacionResult(clave_acceso=CLAVE_REAL, estado="AUTORIZADO", xml_comprobante=None)
        assert r.tiene_xml is False

    def test_tiene_xml_false_cuando_vacio(self):
        r = AutorizacionResult(clave_acceso=CLAVE_REAL, estado="AUTORIZADO", xml_comprobante="   ")
        assert r.tiene_xml is False

    def test_valores_default(self):
        r = AutorizacionResult(clave_acceso=CLAVE_REAL, estado="AUTORIZADO")
        assert r.mensajes == []
        assert r.error is None
        assert r.numero_autorizacion == ""


# ── Tests de _parse_respuesta ──────────────────────────────────────────────────

class TestParseRespuesta:
    def test_parsea_respuesta_autorizada(self):
        respuesta = _make_autorizacion(estado="AUTORIZADO", comprobante=XML_SAMPLE)
        result = _parse_respuesta(CLAVE_REAL, respuesta)

        assert result.clave_acceso == CLAVE_REAL
        assert result.estado == "AUTORIZADO"
        assert result.autorizado is True
        assert result.xml_comprobante == XML_SAMPLE
        assert result.ambiente == "PRODUCCION"
        assert result.tiene_xml is True

    def test_parsea_respuesta_no_autorizada(self):
        respuesta = _make_autorizacion(estado="NO AUTORIZADO", comprobante="")
        result = _parse_respuesta(CLAVE_REAL, respuesta)

        assert result.estado == "NO AUTORIZADO"
        assert result.autorizado is False

    def test_sin_autorizaciones_retorna_sin_autorizacion(self):
        respuesta = SimpleNamespace(autorizaciones=None)
        result = _parse_respuesta(CLAVE_REAL, respuesta)

        assert result.estado == "SIN_AUTORIZACION"
        assert result.error is not None

    def test_lista_autorizaciones_vacia(self):
        respuesta = SimpleNamespace(
            autorizaciones=SimpleNamespace(autorizacion=[])
        )
        result = _parse_respuesta(CLAVE_REAL, respuesta)

        assert result.estado == "SIN_AUTORIZACION"

    def test_parsea_mensajes(self):
        respuesta = _make_autorizacion(
            estado="NO AUTORIZADO",
            mensajes=[
                {"identificador": "35", "mensaje": "CLAVE ACCESO NO REGISTRADA", "tipo": "ERROR"}
            ],
        )
        result = _parse_respuesta(CLAVE_REAL, respuesta)

        assert len(result.mensajes) == 1
        assert result.mensajes[0]["identificador"] == "35"
        assert result.mensajes[0]["mensaje"] == "CLAVE ACCESO NO REGISTRADA"
        assert result.mensajes[0]["tipo"] == "ERROR"

    def test_mensajes_vacios(self):
        respuesta = _make_autorizacion(estado="AUTORIZADO", mensajes=[])
        result = _parse_respuesta(CLAVE_REAL, respuesta)
        assert result.mensajes == []

    def test_fecha_autorizacion_convertida_a_string(self):
        respuesta = _make_autorizacion(fecha="2026-03-09T09:09:22")
        result = _parse_respuesta(CLAVE_REAL, respuesta)
        assert result.fecha_autorizacion == "2026-03-09T09:09:22"

    def test_respuesta_con_atributos_none(self):
        """Debe manejar graciosamente atributos None en la respuesta."""
        auth = SimpleNamespace(
            estado="AUTORIZADO",
            numeroAutorizacion=None,
            fechaAutorizacion=None,
            ambiente=None,
            comprobante=None,
            mensajes=None,
        )
        respuesta = SimpleNamespace(
            autorizaciones=SimpleNamespace(autorizacion=[auth])
        )
        result = _parse_respuesta(CLAVE_REAL, respuesta)

        assert result.estado == "AUTORIZADO"
        assert result.numero_autorizacion == ""
        assert result.fecha_autorizacion is None or result.fecha_autorizacion == "None"
        assert result.xml_comprobante is None or result.xml_comprobante == ""


# ── Tests de save_xml ─────────────────────────────────────────────────────────

class TestSaveXml:
    def test_guarda_xml_en_disco(self, tmp_path: Path):
        result = AutorizacionResult(
            clave_acceso=CLAVE_REAL,
            estado="AUTORIZADO",
            xml_comprobante=XML_SAMPLE,
        )
        saved = save_xml(result, tmp_path)

        assert saved is not None
        assert saved.exists()
        assert saved.name == f"{CLAVE_REAL}.xml"
        content = saved.read_text(encoding="utf-8")
        assert XML_SAMPLE in content

    def test_agrega_declaracion_xml_si_falta(self, tmp_path: Path):
        result = AutorizacionResult(
            clave_acceso=CLAVE_REAL,
            estado="AUTORIZADO",
            xml_comprobante="<factura/>",
        )
        saved = save_xml(result, tmp_path)
        content = saved.read_text(encoding="utf-8")
        assert content.startswith("<?xml")

    def test_no_agrega_declaracion_si_ya_existe(self, tmp_path: Path):
        xml_con_declaracion = '<?xml version="1.0" encoding="UTF-8"?>\n<factura/>'
        result = AutorizacionResult(
            clave_acceso=CLAVE_REAL,
            estado="AUTORIZADO",
            xml_comprobante=xml_con_declaracion,
        )
        saved = save_xml(result, tmp_path)
        content = saved.read_text(encoding="utf-8")
        # No debe haber declaración duplicada
        assert content.count("<?xml") == 1

    def test_retorna_none_si_no_hay_xml(self, tmp_path: Path):
        result = AutorizacionResult(clave_acceso=CLAVE_REAL, estado="AUTORIZADO")
        saved = save_xml(result, tmp_path)
        assert saved is None

    def test_retorna_none_si_xml_vacio(self, tmp_path: Path):
        result = AutorizacionResult(
            clave_acceso=CLAVE_REAL,
            estado="AUTORIZADO",
            xml_comprobante="  ",
        )
        saved = save_xml(result, tmp_path)
        assert saved is None

    def test_crea_directorio_si_no_existe(self, tmp_path: Path):
        nested = tmp_path / "xml" / "2026" / "03"
        result = AutorizacionResult(
            clave_acceso=CLAVE_REAL,
            estado="AUTORIZADO",
            xml_comprobante=XML_SAMPLE,
        )
        saved = save_xml(result, nested)
        assert nested.exists()
        assert saved is not None and saved.exists()


# ── Tests de SRISOAPClient (con mock de zeep) ─────────────────────────────────

class TestSRISOAPClient:
    """
    Verifica la lógica del cliente SOAP sin hacer llamadas reales al SRI.
    Mockea zeep.Client para simular respuestas del WS.
    """

    def _make_client(self, ambiente: str = "produccion"):
        """Crea un SRISOAPClient con zeep.Client mockeado."""
        from sri_scraper.soap_client import SRISOAPClient

        with patch("zeep.Client") as mock_zeep, patch("zeep.transports.Transport"):
            mock_zeep.return_value = MagicMock()
            client = SRISOAPClient(ambiente=ambiente, timeout=5)
            # Reemplazar el cliente interno con uno que podamos controlar
            client._client = MagicMock()
            return client

    def test_autorizar_comprobante_ok(self):
        client = self._make_client()
        respuesta_mock = _make_autorizacion(estado="AUTORIZADO", comprobante=XML_SAMPLE)
        client._client.service.autorizacionComprobante.return_value = respuesta_mock

        result = client.autorizar_comprobante(CLAVE_REAL)

        assert result.autorizado is True
        assert result.clave_acceso == CLAVE_REAL
        assert result.tiene_xml is True
        client._client.service.autorizacionComprobante.assert_called_once_with(
            claveAccesoComprobante=CLAVE_REAL
        )

    def test_autorizar_comprobante_no_autorizado(self):
        client = self._make_client()
        respuesta_mock = _make_autorizacion(
            estado="NO AUTORIZADO",
            comprobante="",
            mensajes=[{"mensaje": "CLAVE ACCESO NO REGISTRADA", "tipo": "ERROR"}],
        )
        client._client.service.autorizacionComprobante.return_value = respuesta_mock

        result = client.autorizar_comprobante(CLAVE_REAL)

        assert result.autorizado is False
        assert result.estado == "NO AUTORIZADO"
        assert len(result.mensajes) == 1

    def test_autorizar_comprobante_soap_fault(self):
        import zeep.exceptions

        client = self._make_client()
        client._client.service.autorizacionComprobante.side_effect = (
            zeep.exceptions.Fault("Service unavailable")
        )

        result = client.autorizar_comprobante(CLAVE_REAL)

        assert result.estado == "ERROR"
        assert result.error is not None
        assert "SOAP Fault" in result.error

    def test_autorizar_comprobante_excepcion_generica(self):
        client = self._make_client()
        client._client.service.autorizacionComprobante.side_effect = (
            ConnectionError("No route to host")
        )

        result = client.autorizar_comprobante(CLAVE_REAL)

        assert result.estado == "ERROR"
        assert result.error is not None

    def test_ambiente_produccion_usa_wsdl_correcto(self):
        from sri_scraper.soap_client import WSDL_PRODUCCION

        with patch("zeep.Client") as mock_zeep, patch("zeep.transports.Transport"):
            mock_zeep.return_value = MagicMock()
            from sri_scraper.soap_client import SRISOAPClient
            SRISOAPClient(ambiente="produccion")
            args, kwargs = mock_zeep.call_args
            assert WSDL_PRODUCCION in (args[0] if args else kwargs.get("wsdl", ""))

    def test_ambiente_pruebas_usa_wsdl_correcto(self):
        from sri_scraper.soap_client import WSDL_PRUEBAS

        with patch("zeep.Client") as mock_zeep, patch("zeep.transports.Transport"):
            mock_zeep.return_value = MagicMock()
            from sri_scraper.soap_client import SRISOAPClient
            SRISOAPClient(ambiente="pruebas")
            args, kwargs = mock_zeep.call_args
            assert WSDL_PRUEBAS in (args[0] if args else kwargs.get("wsdl", ""))
