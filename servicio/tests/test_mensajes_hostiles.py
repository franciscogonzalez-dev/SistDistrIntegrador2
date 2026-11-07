"""Tests de mensajes hostiles de un cliente autenticado (Issue #45).
No requieren broker MQTT: un solo mensaje no tiene que poder dejar al servicio sin atender.
"""

import unittest
from unittest.mock import MagicMock, patch

from comun import payloads
from comun.publisher import MQTTPub
from servicio.src.handlers import posicion
from servicio.src.main import ServicioCentral

ENTERO_ENORME = b'{"v":1,"id":"x","ts":1' + b"0" * 400 + b',"lat":-45.86,"lon":-67.48,"estado":"online","accion":"liberar"}'
JSON_ANIDADO = b"[" * 200000

TOPICOS = [
    ("_on_pedido", "pedidos/publico-" + "a" * 32 + "/bomberos"),
    ("_on_posicion", "reportes/ambulancia/amb-01/posicion"),
    ("_on_presencia", "reportes/ambulancia/amb-01/presencia"),
    ("_on_comando", "comandos/desp-01/ambulancia/amb-01"),
]


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class TestPayloadsHostiles(unittest.TestCase):
    def test_entero_enorme_es_payload_invalido(self):
        for spec in (payloads.POSICION, payloads.PEDIDO, payloads.PRESENCIA, payloads.COMANDO):
            with self.assertRaises(payloads.PayloadInvalido):
                payloads.validar(spec, b'{"v":1' + b"0" * 400 + b"}")

    def test_entero_enorme_en_un_campo_es_payload_invalido(self):
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.PEDIDO, ENTERO_ENORME)

    def test_json_muy_anidado_es_payload_invalido(self):
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.PEDIDO, JSON_ANIDADO)

    def test_el_error_recorta_el_valor_recibido(self):
        with self.assertRaises(payloads.PayloadInvalido) as ctx:
            payloads.validar(payloads.POSICION, {"v": 1, "ts": "x" * 100000, "lat": 0, "lon": 0})
        self.assertLess(len(str(ctx.exception)), 200)


class TestServicioAnteMensajesHostiles(unittest.TestCase):
    def setUp(self):
        self.servicio = ServicioCentral(ip_broker="127.0.0.1", puerto=1883, ca_certs=None, ruta_bd=":memory:")
        self.addCleanup(self.servicio.bd.cerrar_conn)
        self.addCleanup(posicion._ultima_alerta.clear)
        self.servicio.cliente = MagicMock()

    def test_los_callbacks_no_dejan_escapar_excepciones(self):
        for nombre, topic in TOPICOS:
            for payload in (ENTERO_ENORME, JSON_ANIDADO):
                with self.subTest(callback=nombre, largo=len(payload)):
                    getattr(self.servicio, nombre)(None, None, FakeMessage(topic, payload))

    def test_sigue_procesando_despues_de_un_mensaje_hostil(self):
        topic = "reportes/ambulancia/amb-01/posicion"
        self.servicio._on_posicion(None, None, FakeMessage(topic, ENTERO_ENORME))

        self.servicio._on_posicion(None, None, FakeMessage(topic, payloads.posicion(lat=-45.864, lon=-67.485)))

        self.assertEqual(self.servicio.bd.leer_unidad("amb-01")["lat"], -45.864)


class TestCallbackProtegido(unittest.TestCase):
    """Lo que no esté previsto en los handlers tampoco puede matar el hilo de red de paho."""

    def _callback_registrado(self, callback):
        cliente = MQTTPub("127.0.0.1", 1883, "prueba")
        with patch.object(cliente._client, "message_callback_add") as agregar:
            cliente.subscribe("a/b", callback=callback)
        return agregar.call_args.args[1]

    def test_una_excepcion_no_sale_del_callback(self):
        def explota(client, userdata, msg):
            raise RuntimeError("imprevisto")

        with self.assertLogs("publisher", level="ERROR"):
            self._callback_registrado(explota)(None, None, FakeMessage("a/b", b""))

    def test_el_mensaje_le_llega_al_callback(self):
        callback = MagicMock()
        mensaje = FakeMessage("a/b", b"{}")

        self._callback_registrado(callback)("cliente", "datos", mensaje)

        callback.assert_called_once_with("cliente", "datos", mensaje)


class TestCertificadoObligatorio(unittest.TestCase):
    def test_sin_el_certificado_de_la_ca_no_arranca(self):
        with self.assertRaises(SystemExit):
            ServicioCentral(ip_broker="127.0.0.1", puerto=8883, ca_certs="no/existe/ca.crt", ruta_bd=":memory:")


if __name__ == "__main__":
    unittest.main()
