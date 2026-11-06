"""Pruebas unitarias para la App del Público (Issue #16).

Verifica:
- Generación y persistencia de Client ID según contrato EMQX (publico-<32 hex>).
- Creación y publicación de pedidos de emergencia en pedidos/{clientid}/{tipo}.
- Idempotencia del botón de reintento (mismo id).
- Manejo del estado del servicio (servicio/estado) y aviso de servicio caído.
- Comportamiento ante desconexión del broker / red.
"""

import unittest
from unittest.mock import MagicMock, patch
import json
import uuid
import re
from pathlib import Path
import tempfile

from comun import topics, payloads
from clientes.publico import AppPublico, obtener_client_id, ARCHIVO_CLIENT_ID


class DummyMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        if isinstance(payload, bytes):
            self.payload = payload
        elif isinstance(payload, str):
            self.payload = payload.encode("utf-8")
        else:
            self.payload = json.dumps(payload).encode("utf-8")


class BaseTestPublico(unittest.TestCase):

    def setUp(self):
        self.client_id = f"publico-{uuid.uuid4().hex}"
        with patch("clientes.publico.MQTTPub"):
            self.app = AppPublico(
                client_id=self.client_id,
                ip_broker="127.0.0.1",
                puerto=8883,
                password="test_password",
                ca_certs=None,
                modo_prueba=True,
            )
            # Inicializar componentes de ventana sin loop bloqueante
            self.app.root = MagicMock()
            self.app.lbl_red = MagicMock()
            self.app.frame_banner = MagicMock()
            self.app.lbl_banner_icono = MagicMock()
            self.app.lbl_banner_texto = MagicMock()
            self.app.lbl_confirm = MagicMock()
            self.app.btn_reenviar = MagicMock()

            # Mock de MQTTPub
            self.app.publish = MagicMock(return_value=True)

    def tearDown(self):
        self.app._cerrando = True


class TestPublicoClientId(unittest.TestCase):

    def test_formato_regex_client_id(self):
        """Verifica que el Client ID cumpla con la restricción de EMQX: publico-<32 hex>."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archivo_mock = Path(tmpdir) / ".client_id_publico"
            with patch("clientes.publico.ARCHIVO_CLIENT_ID", archivo_mock):
                cid = obtener_client_id()
                self.assertTrue(re.fullmatch(r"publico-[0-9a-fA-F]{32}", cid))
                self.assertTrue(archivo_mock.exists())
                self.assertEqual(archivo_mock.read_text().strip(), cid)

                # Si ya existe, lo reutiliza
                cid_reusado = obtener_client_id()
                self.assertEqual(cid, cid_reusado)

    def test_reemplazo_client_id_corrupto(self):
        """Si el archivo guardado tiene un formato no válido, genera uno nuevo válido."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archivo_mock = Path(tmpdir) / ".client_id_publico"
            archivo_mock.write_text("client_id_invalido_12345")
            with patch("clientes.publico.ARCHIVO_CLIENT_ID", archivo_mock):
                cid = obtener_client_id()
                self.assertTrue(re.fullmatch(r"publico-[0-9a-fA-F]{32}", cid))
                self.assertNotEqual(cid, "client_id_invalido_12345")


class TestPublicoPedidos(BaseTestPublico):

    def test_enviar_pedido_valido(self):
        """Al presionar un botón de emergencia, se publica con QoS 1 en el topic correcto."""
        self.app.estado_red = "conectado"
        self.app.lat_actual = -45.8641
        self.app.lon_actual = -67.4850

        self.app._enviar_pedido("policia")

        self.assertTrue(self.app.publish.called)
        args, kwargs = self.app.publish.call_args
        dato, topic = args[0], args[1]

        # Verificar tópico: pedidos/{clientid}/policia
        self.assertEqual(topic, topics.pedido(self.client_id, "policia"))
        self.assertEqual(kwargs.get("qos"), 1)
        self.assertFalse(kwargs.get("retain"))

        # Validar payload según el contrato
        validado = payloads.validar(payloads.PEDIDO, dato)
        self.assertEqual(validado["lat"], -45.8641)
        self.assertEqual(validado["lon"], -67.4850)
        self.assertEqual(validado["id"], self.app.ultimo_pedido_id)

    def test_idempotencia_reenviar_pedido(self):
        """Reenviar pedido debe reusar exactamente el mismo ID para evitar duplicados."""
        self.app.estado_red = "conectado"
        self.app._enviar_pedido("ambulancia")
        primer_id = self.app.ultimo_pedido_id

        # Reenviar
        self.app._reenviar_pedido()
        args, _ = self.app.publish.call_args
        dato = args[0]

        self.assertEqual(dato["id"], primer_id)
        self.assertEqual(self.app.ultimo_pedido_id, primer_id)

    def test_enviar_pedido_sin_red_muestra_en_cola(self):
        """Si no hay conexión, el pedido se encola en Paho y la UI avisa que saldrá al reconectar."""
        self.app.estado_red = "sin_red"
        # MQTTPub.publish retorna True cuando se encola localmente
        self.app.publish.return_value = True

        self.app._enviar_pedido("bomberos")

        # La confirmación en pantalla debe indicar que quedó en cola
        self.app.lbl_confirm.configure.assert_called()
        llamada_args = self.app.lbl_confirm.configure.call_args[1]
        self.assertIn("en cola", llamada_args["text"])


class TestPublicoServicioEstado(BaseTestPublico):

    def test_recibir_servicio_online(self):
        """Al recibir servicio/estado 'online', el banner se actualiza a operativo."""
        msg = DummyMessage(topics.SERVICIO_ESTADO, payloads.servicio_estado("online"))
        self.app._on_servicio_estado(None, None, msg)

        # Debe encolar banner online
        evento, valor = self.app._cola_ui.get_nowait()
        self.assertEqual(evento, "banner")
        self.assertEqual(valor, "online")

        # Al procesar en UI
        self.app._actualizar_banner("online")
        self.assertEqual(self.app.estado_servicio, "online")
        self.assertIn("operativo", self.app.lbl_banner_texto.configure.call_args[1]["text"])

    def test_recibir_servicio_offline(self):
        """Al recibir servicio/estado 'offline', el banner advierte y muestra teléfonos 107/101/100."""
        msg = DummyMessage(topics.SERVICIO_ESTADO, payloads.servicio_estado("offline"))
        self.app._on_servicio_estado(None, None, msg)

        evento, valor = self.app._cola_ui.get_nowait()
        self.assertEqual(evento, "banner")
        self.assertEqual(valor, "offline")

        self.app._actualizar_banner("offline")
        self.assertEqual(self.app.estado_servicio, "offline")
        texto_banner = self.app.lbl_banner_texto.configure.call_args[1]["text"]
        self.assertIn("problemas", texto_banner)
        self.assertIn("107", texto_banner)
        self.assertIn("101", texto_banner)
        self.assertIn("100", texto_banner)

    def test_desconexion_broker_marca_servicio_offline(self):
        """Si el broker se desconecta, se asume el servicio inalcanzable de inmediato."""
        self.app._on_disconnect(None, None, None, 0, None)

        eventos = []
        while not self.app._cola_ui.empty():
            eventos.append(self.app._cola_ui.get_nowait())

        self.assertIn(("red", "sin_red"), eventos)
        self.assertIn(("banner", "offline"), eventos)


if __name__ == "__main__":
    unittest.main()
