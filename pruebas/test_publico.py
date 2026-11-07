"""Pruebas unitarias para la App del Público (Issue #16).

Verifica:
- Generación y persistencia de Client ID según contrato EMQX (publico-<32 hex>).
- Creación y publicación de pedidos de emergencia en pedidos/{clientid}/{tipo}.
- Idempotencia del botón de reintento (mismo id).
- Manejo del estado del servicio (servicio/estado) y aviso de servicio caído.
- Comportamiento ante desconexión del broker / red.
- Estado real del pedido según el PUBACK, toques repetidos y cierre con pedidos pendientes.
"""

import socket
import time
import unittest
from unittest.mock import MagicMock, patch
import json
import uuid
import re
from pathlib import Path
import tempfile

from comun import topics, payloads
from comun.publisher import MQTTPub
from clientes import publico
from clientes.publico import AppPublico, obtener_client_id


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
        # Se reemplaza el cliente de paho: AppPublico ya heredó de MQTTPub, parchear esa clase no haría nada
        parche = patch("comun.publisher.mqtt.Client")
        parche.start()
        self.addCleanup(parche.stop)

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

        # publish aceptado por paho: cada mensaje recibe un mid nuevo
        self.mid = 0
        self.app.publish = MagicMock(side_effect=self._publish_aceptado)

    def _publish_aceptado(self, *_args, **_kwargs):
        self.mid += 1
        self.app.ultimo_mid = self.mid
        return True

    def _texto_confirmacion(self):
        return self.app.lbl_confirm.configure.call_args[1]["text"]

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

    def test_cada_perfil_tiene_su_client_id_y_lo_conserva(self):
        """Dos instancias con el mismo Client ID se echarían entre sí del broker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archivo_mock = Path(tmpdir) / ".client_id_publico"
            with patch("clientes.publico.ARCHIVO_CLIENT_ID", archivo_mock):
                sin_perfil = obtener_client_id()
                perfil_1 = obtener_client_id(1)
                perfil_2 = obtener_client_id(2)

                self.assertEqual(len({sin_perfil, perfil_1, perfil_2}), 3)
                self.assertTrue(re.fullmatch(r"publico-[0-9a-fA-F]{32}", perfil_1))
                self.assertEqual(obtener_client_id(1), perfil_1)
                self.assertEqual(obtener_client_id(), sin_perfil)


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
        self.assertEqual(validado["id"], self.app.ultimo_pedido["dato"]["id"])

    def test_idempotencia_reenviar_pedido(self):
        """Reenviar pedido debe reusar exactamente el mismo ID para evitar duplicados."""
        self.app.estado_red = "conectado"
        self.app._enviar_pedido("ambulancia")
        primer_id = self.app.ultimo_pedido["dato"]["id"]

        # Reenviar
        self.app._reenviar_pedido()
        args, _ = self.app.publish.call_args
        dato = args[0]

        self.assertEqual(self.app.publish.call_count, 2)
        self.assertEqual(dato["id"], primer_id)
        self.assertEqual(self.app.ultimo_pedido["dato"]["id"], primer_id)

    def test_reenviar_manda_el_pedido_original_aunque_cambie_la_ubicacion(self):
        """Con otra ubicación y el id viejo, el servicio la ignoraría por duplicado."""
        self.app._enviar_pedido("ambulancia")
        original = self.app.publish.call_args[0][0]

        self.app.lat_actual, self.app.lon_actual = -45.7980, -67.4680
        self.app._reenviar_pedido()

        self.assertEqual(self.app.publish.call_args[0][0], original)

    def test_enviar_pedido_sin_red_muestra_en_cola(self):
        """Si no hay conexión, el pedido se encola en Paho y la UI avisa que saldrá al reconectar."""
        self.app.estado_red = "sin_red"
        self.app._enviar_pedido("bomberos")

        # La confirmación en pantalla debe indicar que quedó en cola
        self.app.lbl_confirm.configure.assert_called()
        llamada_args = self.app.lbl_confirm.configure.call_args[1]
        self.assertIn("en cola", llamada_args["text"])

    def test_sin_puback_no_se_muestra_como_enviado(self):
        """Que paho acepte el mensaje no significa que llegó al broker."""
        self.app.estado_red = "conectado"
        self.app._enviar_pedido("policia")

        self.assertIn("Enviando", self._texto_confirmacion())
        self.assertNotIn("✅", self._texto_confirmacion())
        self.assertTrue(self.app._pendientes)

        self.app._on_publish(None, None, self.mid, MagicMock(is_failure=False), None)
        self.app._poll_mqtt()

        self.assertIn("✅", self._texto_confirmacion())
        self.assertFalse(self.app._pendientes)

    def test_pedido_en_cola_se_actualiza_al_reconectar_y_confirmar(self):
        self.app.estado_red = "sin_red"
        self.app._enviar_pedido("bomberos")
        self.assertIn("en cola", self._texto_confirmacion())

        self.app._actualizar_red("conectado")
        self.assertIn("Enviando", self._texto_confirmacion())

        self.app._procesar_puback(self.mid, False)
        self.assertIn("✅", self._texto_confirmacion())

    def test_corte_con_pedido_sin_confirmar_vuelve_a_en_cola(self):
        self.app.estado_red = "conectado"
        self.app._enviar_pedido("bomberos")

        self.app._actualizar_red("sin_red")

        self.assertIn("en cola", self._texto_confirmacion())

    def test_puback_rechazado_muestra_error_con_telefonos(self):
        self.app.estado_red = "conectado"
        self.app._enviar_pedido("policia")

        self.app._procesar_puback(self.mid, True)

        self.assertIn("No se pudo", self._texto_confirmacion())
        self.assertIn("107", self._texto_confirmacion())

    def test_puback_sin_suscriptores_no_se_muestra_como_enviado(self):
        """El broker confirma con 16 si nadie lo recibe: el servicio nunca arrancó y el pedido se pierde."""
        self.app.estado_red = "conectado"
        self.app._enviar_pedido("policia")

        rc = MagicMock(is_failure=False, value=publico.RC_SIN_SUSCRIPTORES)
        self.app._on_publish(None, None, self.mid, rc, None)
        self.app._poll_mqtt()

        self.assertNotIn("✅", self._texto_confirmacion())
        self.assertIn("107", self._texto_confirmacion())

    def test_publish_fallido_no_se_muestra_en_cola(self):
        """publish en False significa que el mensaje se perdió, no que quedó encolado."""
        self.app.estado_red = "sin_red"
        self.app.publish = MagicMock(return_value=False)

        self.app._enviar_pedido("bomberos")

        self.assertNotIn("en cola", self._texto_confirmacion())
        self.assertIn("No se pudo", self._texto_confirmacion())
        self.assertIn("107", self._texto_confirmacion())
        self.assertFalse(self.app._pendientes)

    def test_tras_un_fallo_otro_toque_es_un_pedido_nuevo(self):
        self.app.publish = MagicMock(return_value=False)
        self.app._enviar_pedido("bomberos")
        fallido = self.app.publish.call_args[0][0]["id"]

        self.app.publish = MagicMock(side_effect=self._publish_aceptado)
        self.app._enviar_pedido("bomberos")

        self.assertEqual(self.app.publish.call_count, 1)
        self.assertNotEqual(self.app.publish.call_args[0][0]["id"], fallido)

    def test_toques_repetidos_sin_confirmar_no_crean_otro_pedido(self):
        """Tocar varias veces sin conexión no tiene que mandar varias unidades."""
        self.app.estado_red = "sin_red"
        for _ in range(5):
            self.app._enviar_pedido("ambulancia")

        self.assertEqual(self.app.publish.call_count, 1)

    def test_doble_clic_confirmado_es_el_mismo_pedido_hasta_que_pasa_la_ventana(self):
        self.app.estado_red = "conectado"
        with patch("clientes.publico.time.monotonic", return_value=1000.0):
            self.app._enviar_pedido("ambulancia")
            primer_id = self.app.publish.call_args[0][0]["id"]
            self.app._procesar_puback(self.mid, False)
            self.app._enviar_pedido("ambulancia")
        self.assertEqual(self.app.publish.call_count, 1)

        despues = 1000.0 + publico.VENTANA_REPETICION_SEG + 1
        with patch("clientes.publico.time.monotonic", return_value=despues):
            self.app._enviar_pedido("ambulancia")
        self.assertEqual(self.app.publish.call_count, 2)
        self.assertNotEqual(self.app.publish.call_args[0][0]["id"], primer_id)

    def test_otro_tipo_u_otra_ubicacion_son_pedidos_distintos(self):
        self.app._enviar_pedido("ambulancia")
        self.app._enviar_pedido("policia")
        self.assertEqual(self.app.publish.call_count, 2)

        # En --prueba se cambia de punto para probar otra emergencia
        self.app.lat_actual, self.app.lon_actual = -45.7980, -67.4680
        self.app._enviar_pedido("ambulancia")
        self.assertEqual(self.app.publish.call_count, 3)

    def test_puback_de_otro_pedido_no_pisa_el_estado_del_ultimo(self):
        self.app.estado_red = "conectado"
        self.app._enviar_pedido("ambulancia")
        mid_ambulancia = self.mid
        self.app._enviar_pedido("policia")

        self.app._procesar_puback(mid_ambulancia, False)

        self.assertIn("Enviando solicitud de Policía", self._texto_confirmacion())

    def test_publish_real_expone_el_mid(self):
        """El seguimiento del PUBACK depende del mid que deja MQTTPub.publish."""
        cliente = MQTTPub("127.0.0.1", 8883, self.client_id)
        cliente._client.publish = MagicMock(return_value=MagicMock(rc=0, mid=77))

        self.assertTrue(cliente.publish({"a": 1}, "t", qos=1))
        self.assertEqual(cliente.ultimo_mid, 77)


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

    def test_fallo_de_conexion_avisa_con_los_telefonos(self):
        self.app._on_connect_fail(None, None)
        self.app._poll_mqtt()

        self.app.lbl_red.configure.assert_called_with(text="● Sin conexión", fg=publico.COLOR_OFFLINE)
        self.assertIn("107", self.app.lbl_banner_texto.configure.call_args[1]["text"])

    def test_conectado_sin_estado_del_servicio_termina_avisando(self):
        """Sin retained (el servicio nunca arrancó) no llega ningún mensaje de estado."""
        self.app._actualizar_red("conectado")

        demora, callback = self.app.root.after.call_args[0]
        self.assertEqual(demora, publico.ESPERA_ESTADO_MS)
        self.app.lbl_banner_texto.configure.assert_not_called()

        callback()
        self.assertIn("107", self.app.lbl_banner_texto.configure.call_args[1]["text"])

    def test_si_llega_el_estado_no_avisa_al_vencer_la_espera(self):
        self.app._actualizar_red("conectado")
        _, callback = self.app.root.after.call_args[0]

        self.app._actualizar_banner("online")
        callback()

        self.assertIn("operativo", self.app.lbl_banner_texto.configure.call_args[1]["text"])


class TestPublicoCierre(BaseTestPublico):

    def setUp(self):
        super().setUp()
        self.app.detener = MagicMock()

    def test_cerrar_sin_pendientes_no_pregunta(self):
        with patch("clientes.publico.messagebox.askyesno") as preguntar:
            self.app._al_cerrar_ventana()

        preguntar.assert_not_called()
        self.app.detener.assert_called_once()

    def test_cerrar_con_pendientes_avisa_y_se_puede_cancelar(self):
        self.app._enviar_pedido("ambulancia")

        with patch("clientes.publico.messagebox.askyesno", return_value=False):
            self.app._al_cerrar_ventana()
        self.app.detener.assert_not_called()

        with patch("clientes.publico.messagebox.askyesno", return_value=True):
            self.app._al_cerrar_ventana()
        self.app.detener.assert_called_once()


class TestPublicoBrokerCaido(unittest.TestCase):

    def test_broker_inalcanzable_al_arrancar_llega_a_la_ui(self):
        """Con paho real: el fallo de la primera conexión no pasa por _on_connect."""
        with socket.socket() as libre:
            libre.bind(("127.0.0.1", 0))
            puerto = libre.getsockname()[1]

        app = AppPublico(f"publico-{uuid.uuid4().hex}", "127.0.0.1", puerto, "x")
        self.addCleanup(app.stop)
        app.start()

        fin = time.monotonic() + 5
        while app._cola_ui.empty() and time.monotonic() < fin:
            time.sleep(0.05)

        self.assertEqual(app._cola_ui.get_nowait(), ("red", "sin_red"))
        self.assertEqual(app._cola_ui.get_nowait(), ("banner", "offline"))


if __name__ == "__main__":
    unittest.main()
