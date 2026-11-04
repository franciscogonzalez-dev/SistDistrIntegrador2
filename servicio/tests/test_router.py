"""Tests unitarios para el enrutador y handlers del Servicio Central (Issue #8).
No requieren broker MQTT activo.
"""

import unittest
from unittest.mock import MagicMock
from pathlib import Path
import sys

from comun import payloads
from servicio.src.main import ServicioCentral


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class TestRouterServicio(unittest.TestCase):
    def setUp(self):
        # Instanciar el servicio sin conectar
        self.servicio = ServicioCentral(
            ip_broker="127.0.0.1",
            puerto=1883,
            usuario="servicio",
            ca_certs=None,
            ruta_bd=":memory:",
        )

    def test_on_posicion_valida(self):
        payload_bytes = payloads.posicion(lat=-45.864, lon=-67.485, rumbo=180)
        msg = FakeMessage("reportes/ambulancia/amb-01/posicion", payload_bytes)
        # No debe lanzar excepciones
        self.servicio._on_posicion(None, None, msg)

    def test_on_presencia_valida(self):
        payload_bytes = payloads.presencia("online")
        msg = FakeMessage("reportes/policia/pol-03/presencia", payload_bytes)
        self.servicio._on_presencia(None, None, msg)

    def test_on_comando_valido(self):
        payload_bytes = payloads.comando(accion="asignar", eid="E01")
        msg = FakeMessage("comandos/desp-01/ambulancia/amb-01", payload_bytes)
        self.servicio._on_comando(None, None, msg)

    def test_on_pedido_valido(self):
        payload_bytes = payloads.pedido(lat=-45.86, lon=-67.48)
        msg = FakeMessage("pedidos/publico-abcdef123456/bomberos", payload_bytes)
        self.servicio._on_pedido(None, None, msg)

    def test_topico_malformado_se_descarta(self):
        msg = FakeMessage("reportes/invalido", b"{}")
        # Debe descartar con aviso en log sin arrojar excepción
        self.servicio._on_posicion(None, None, msg)
        self.servicio._on_presencia(None, None, msg)
        self.servicio._on_comando(None, None, msg)
        self.servicio._on_pedido(None, None, msg)


if __name__ == "__main__":
    unittest.main()
