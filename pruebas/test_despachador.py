"""
Pruebas unitarias para el Despachador de Emergencias (Issue Mapa modo despachador)
Verifica suscripciones, manejo de emergencias, alertas, respuestas, asignación y liberación idempotente.
"""

import unittest
from unittest.mock import MagicMock, patch
import json
import time

from comun import topics, payloads, jwt_auth
from clientes.despachador import DespachadorBasico
from servicio.src.bd import BD
from servicio.src.handlers import posicion, presencia


class DummyMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else (payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8"))


class BaseDespachador(unittest.TestCase):

    def setUp(self):
        # Parchear MQTTPub y la descarga de tiles del mapa para pruebas rápidas
        with patch("clientes.despachador.MQTTPub"), patch("tkintermapview.TkinterMapView.set_tile_server"):
            self.desp = DespachadorBasico(
                ip_broker="127.0.0.1",
                puerto=8883,
                client_id="desp-01",
                usuario="desp-01",
                password="test_password",
                ca_certs=None,
                token_jwt=jwt_auth.generar_token("desp-01"),
            )
            # Ocultar la ventana de Tkinter durante los tests
            self.desp.root.withdraw()

            # Mockear cliente MQTT
            self.mock_cliente = MagicMock()
            self.desp.cliente = self.mock_cliente

    def tearDown(self):
        try:
            self.desp.root.destroy()
        except Exception:
            pass


class TestDespachadorLogica(BaseDespachador):

    def test_on_emergencia_valida(self):
        """Verifica que una emergencia válida se parsee y se encole correctamente."""
        payload_dict = {
            "v": 1,
            "eid": "E-10",
            "lat": -45.86,
            "lon": -67.48,
            "estado": "sin_asignar",
            "ts": time.time(),
        }
        msg = DummyMessage("emergencias/ambulancia/E-10", payload_dict)
        self.desp._on_emergencia(None, None, msg)

        self.assertFalse(self.desp.mensajes.empty())
        item = self.desp.mensajes.get()
        self.assertEqual(item[0], "emergencia")
        self.assertEqual(item[1], "ambulancia")
        self.assertEqual(item[2], "E-10")
        self.assertEqual(item[3]["estado"], "sin_asignar")

    def test_on_emergencia_cierre_payload_vacio(self):
        """Verifica que un payload vacío en emergencias/# signifique cierre (retained vacío)."""
        msg = DummyMessage("emergencias/bomberos/E-20", b"")
        self.desp._on_emergencia(None, None, msg)

        self.assertFalse(self.desp.mensajes.empty())
        item = self.desp.mensajes.get()
        self.assertEqual(item[0], "emergencia_cierre")
        self.assertEqual(item[1], "bomberos")
        self.assertEqual(item[2], "E-20")

    def test_on_alerta_valida(self):
        """Verifica recepción de alertas operativas (caída de unidad ocupada)."""
        payload_dict = {
            "v": 1,
            "ts": time.time(),
            "motivo": "unidad_desconectada",
            "mensaje": "pol-01 se desconectó con emergencia en curso",
            "unidad": "pol-01",
            "eid": "E-30",
        }
        msg = DummyMessage("alertas/policia", payload_dict)
        self.desp._on_alerta(None, None, msg)

        self.assertFalse(self.desp.mensajes.empty())
        item = self.desp.mensajes.get()
        self.assertEqual(item[0], "alerta")
        self.assertEqual(item[1], "policia")
        self.assertEqual(item[2]["unidad"], "pol-01")
        self.assertEqual(item[2]["eid"], "E-30")

    def test_on_respuesta_rechazo(self):
        """Verifica recepción de comandos rechazados por el servicio."""
        payload_dict = {
            "v": 1,
            "id": "cmd-1234",
            "ts": time.time(),
            "ok": False,
            "motivo": "Unidad amb-01 ya ocupada",
        }
        msg = DummyMessage("respuestas/desp-01", payload_dict)
        self.desp._on_respuesta(None, None, msg)

        self.assertFalse(self.desp.mensajes.empty())
        item = self.desp.mensajes.get()
        self.assertEqual(item[0], "respuesta")
        self.assertFalse(item[1]["ok"])
        self.assertEqual(item[1]["motivo"], "Unidad amb-01 ya ocupada")

    def test_asignar_unidad_idempotente(self):
        """Verifica que la asignación publique en el tópico correcto y reuse el mismo ID en reintentos."""
        self.mock_cliente.publish.return_value = True

        # Primer intento de asignación
        self.desp.asignar_unidad("amb-01", "ambulancia", "E-101")
        self.assertEqual(self.mock_cliente.publish.call_count, 1)

        args, kwargs = self.mock_cliente.publish.call_args
        payload_enviado = args[0]
        topic_enviado = kwargs.get("topic")

        self.assertEqual(topic_enviado, "comandos/desp-01/ambulancia/amb-01")
        self.assertEqual(payload_enviado["accion"], "asignar")
        self.assertEqual(payload_enviado["eid"], "E-101")
        cmd_id_1 = payload_enviado["id"]
        self.assertTrue(len(cmd_id_1) > 0)

        # Segundo intento de asignación (reintento de la misma acción)
        self.desp.asignar_unidad("amb-01", "ambulancia", "E-101")
        self.assertEqual(self.mock_cliente.publish.call_count, 2)

        args2, kwargs2 = self.mock_cliente.publish.call_args
        payload_enviado_2 = args2[0]
        cmd_id_2 = payload_enviado_2["id"]

        # Debe ser exactamente el mismo ID para garantizar idempotencia
        self.assertEqual(cmd_id_1, cmd_id_2)

        # Una acción diferente debe generar un ID diferente
        self.desp.asignar_unidad("amb-02", "ambulancia", "E-101")
        args3, _ = self.mock_cliente.publish.call_args
        cmd_id_3 = args3[0]["id"]
        self.assertNotEqual(cmd_id_1, cmd_id_3)

    def test_liberar_unidad_idempotente(self):
        """Verifica que la orden liberar publique en el tópico correcto y reuse el mismo ID en reintentos."""
        self.mock_cliente.publish.return_value = True

        self.desp.liberar_unidad("pol-03", "policia")
        self.assertEqual(self.mock_cliente.publish.call_count, 1)

        args, kwargs = self.mock_cliente.publish.call_args
        payload_enviado = args[0]
        topic_enviado = kwargs.get("topic")

        self.assertEqual(topic_enviado, "comandos/desp-01/policia/pol-03")
        self.assertEqual(payload_enviado["accion"], "liberar")
        cmd_id_lib_1 = payload_enviado["id"]

        # Reintento de la misma liberación
        self.desp.liberar_unidad("pol-03", "policia")
        args2, _ = self.mock_cliente.publish.call_args
        cmd_id_lib_2 = args2[0]["id"]

        self.assertEqual(cmd_id_lib_1, cmd_id_lib_2)

    def test_actualizar_y_cerrar_emergencia_en_estado(self):
        """Verifica que actualizar y cerrar emergencias actualice el diccionario interno y marcadores."""
        datos_em = {
            "lat": -45.86,
            "lon": -67.48,
            "estado": "sin_asignar",
            "unidad": None,
            "ts": time.time(),
        }
        with patch.object(self.desp.mapa, "set_marker") as mock_set_marker:
            mock_marker = MagicMock()
            mock_set_marker.return_value = mock_marker

            # Crear emergencia
            self.desp.actualizar_emergencia("ambulancia", "E-50", datos_em)
            self.assertIn("E-50", self.desp.emergencias)
            self.assertEqual(self.desp.emergencias["E-50"]["estado"], "sin_asignar")
            self.assertIn("E-50", self.desp.marcadores_emergencias)

            # Cerrar emergencia
            self.desp.cerrar_emergencia("ambulancia", "E-50")
            self.assertNotIn("E-50", self.desp.emergencias)
            self.assertNotIn("E-50", self.desp.marcadores_emergencias)
            mock_marker.delete.assert_called_once()

    def test_procesar_alerta_unidad_desconectada_muestra_banner(self):
        """Verifica que la caída de un móvil ocupado despliegue el banner con opción de reasignación."""
        datos_alerta = {
            "motivo": "unidad_desconectada",
            "mensaje": "amb-01 se cayó",
            "unidad": "amb-01",
            "eid": "E-99",
        }
        self.desp.procesar_alerta("ambulancia", datos_alerta)

        # El banner debe estar empacado
        self.assertEqual(self.desp.frame_alerta.winfo_manager(), "pack")
        self.assertIn("amb-01", self.desp.lbl_alerta_msg.cget("text"))
        self.assertIn("E-99", self.desp.lbl_alerta_msg.cget("text"))

    def test_procesar_respuesta_rechazo_muestra_banner(self):
        """Verifica que un comando rechazado despliegue el banner de advertencia con el motivo."""
        datos_rechazo = {
            "id": "cmd-test-1",
            "ok": False,
            "motivo": "Unidad amb-01 ya ocupada",
        }
        self.desp.procesar_respuesta(datos_rechazo)

        self.assertEqual(self.desp.frame_alerta.winfo_manager(), "pack")
        self.assertIn("Unidad amb-01 ya ocupada", self.desp.lbl_alerta_msg.cget("text"))

    def _id_publicado(self):
        return self.mock_cliente.publish.call_args.args[0]["id"]

    def test_liberar_otra_vez_tras_la_respuesta_usa_id_nuevo(self):
        """Liberar la misma unidad en otra emergencia es una acción nueva: el servicio descartaría el id repetido."""
        self.mock_cliente.publish.return_value = True

        self.desp.liberar_unidad("amb-01", "ambulancia")
        primer_id = self._id_publicado()
        self.desp.procesar_respuesta({"id": primer_id, "ok": True})

        self.desp.liberar_unidad("amb-01", "ambulancia")
        self.assertNotEqual(primer_id, self._id_publicado())

    def test_reintentar_tras_un_rechazo_usa_id_nuevo(self):
        """Con el mismo id el servicio reenviaría el rechazo anterior sin volver a evaluar la orden."""
        self.mock_cliente.publish.return_value = True

        self.desp.asignar_unidad("amb-01", "ambulancia", "E-101")
        primer_id = self._id_publicado()
        self.desp.procesar_respuesta({"id": primer_id, "ok": False, "motivo": "amb-01 ocupada"})

        self.desp.asignar_unidad("amb-01", "ambulancia", "E-101")
        self.assertNotEqual(primer_id, self._id_publicado())

    def test_respuesta_de_otro_comando_no_borra_el_id_pendiente(self):
        self.mock_cliente.publish.return_value = True

        self.desp.liberar_unidad("amb-01", "ambulancia")
        primer_id = self._id_publicado()
        self.desp.procesar_respuesta({"id": "otro-comando", "ok": True})

        self.desp.liberar_unidad("amb-01", "ambulancia")
        self.assertEqual(primer_id, self._id_publicado())

    def _unidad_libre(self, unidad, conexion="online"):
        estado = {"conexion": conexion, "disponibilidad": "libre", "lat": -45.86, "lon": -67.48}
        self.desp.actualizar_unidad(unidad, "ambulancia", estado)

    def _emergencia_seleccionada(self, eid="E-60"):
        self.desp.actualizar_emergencia("ambulancia", eid, {"lat": -45.86, "lon": -67.48, "estado": "sin_asignar"})
        self.desp.seleccionar_emergencia_por_id(eid)

    def test_mensajes_de_flota_no_cambian_el_movil_elegido(self):
        """La flota real se publica con cada posición: la selección tiene que sobrevivir a esas actualizaciones."""
        with patch.object(self.desp.mapa, "set_marker", side_effect=lambda *a, **k: MagicMock(polygon=None, big_circle=None)):
            self._unidad_libre("amb-01")
            self._unidad_libre("amb-02")
            self._emergencia_seleccionada()
            self.desp.cbo_unidades_libres.set("amb-02")

            self._unidad_libre("amb-01")
            self.assertEqual(self.desp.cbo_unidades_libres.get(), "amb-02")

            # Si el elegido deja de estar disponible, pasa al primero libre
            self._unidad_libre("amb-02", conexion="offline")
            self.assertEqual(self.desp.cbo_unidades_libres.get(), "amb-01")

    def test_reasignar_emergencia_ya_cerrada_no_rompe_el_mapa(self):
        """Antes dejaba seleccionado un eid inexistente y el siguiente mensaje de flota daba KeyError."""
        with patch.object(self.desp.mapa, "set_marker", side_effect=lambda *a, **k: MagicMock(polygon=None, big_circle=None)):
            self._emergencia_seleccionada("E-61")
            self.desp.cerrar_emergencia("ambulancia", "E-61")

            self.desp.preparar_reasignacion("E-61", "ambulancia")
            self.assertIsNone(self.desp.emergencia_seleccionada_id)
            self._unidad_libre("amb-01")

        self.assertIn("amb-01", self.desp.flota_info)


class TestDespachadorConServicioReal(BaseDespachador):
    """Lo que publican los handlers reales de servicio/ tiene que entenderlo el despachador.

    Sin broker: base SQLite en memoria y lo publicado se le entrega al despachador con el mismo
    json que viajaría por MQTT. Asignar y liberar no están acá porque los implementa #12.
    """

    def setUp(self):
        super().setUp()
        self.bd = BD(":memory:")
        self.addCleanup(self.bd.cerrar_conn)
        self.servicio = MagicMock()
        self.parche_marcador = patch.object(
            self.desp.mapa, "set_marker", side_effect=lambda *a, **k: MagicMock(polygon=None, big_circle=None)
        )
        self.parche_marcador.start()
        self.addCleanup(self.parche_marcador.stop)

    def _entregar_publicaciones(self):
        """Pasa por los callbacks del despachador todo lo publicado por el servicio y lo procesa."""
        callbacks = {
            "flota": self.desp._on_flota,
            "emergencias": self.desp._on_emergencia,
            "alertas": self.desp._on_alerta,
        }
        for llamada in self.servicio.publish.call_args_list:
            topic = llamada.kwargs["topic"]
            callbacks[topic.split("/")[0]](None, None, DummyMessage(topic, llamada.args[0]))
        self.servicio.publish.reset_mock()
        self.desp._procesar_mensajes()

    def _unidad_conectada(self):
        presencia.procesar(self.servicio, self.bd, "ambulancia", "amb-01", payloads.presencia("online"))
        posicion.procesar(self.servicio, self.bd, "ambulancia", "amb-01", payloads.posicion(-45.86, -67.48, rumbo=90))
        self._entregar_publicaciones()

    def test_unidad_del_servicio_aparece_libre_en_el_mapa(self):
        self._unidad_conectada()

        self.assertEqual(self.desp.flota_info["amb-01"]["estado"], "libre")
        self.assertIn("amb-01", self.desp.marcadores)

    def test_caida_de_unidad_ocupada_alerta_y_permite_reasignar(self):
        self._unidad_conectada()
        # lo que va a hacer #12; BD todavia no tiene un metodo para crear emergencias
        with self.bd.transaccion():
            self.bd._conn.execute(
                "INSERT INTO emergencias (eid, tipo, lat, lon, ts, unidad) "
                "VALUES ('E42', 'ambulancia', -45.87, -67.50, 1, 'amb-01')"
            )

        presencia.procesar(self.servicio, self.bd, "ambulancia", "amb-01", payloads.presencia("offline"))
        self._entregar_publicaciones()

        self.assertEqual(self.desp.flota_info["amb-01"]["estado"], "sin_conexion")
        self.assertEqual(self.desp.emergencias["E42"]["estado"], "sin_asignar")
        self.assertIsNone(self.desp.emergencias["E42"]["unidad"])
        self.assertEqual(self.desp.frame_alerta.winfo_manager(), "pack")
        self.assertIn("E42", self.desp.btn_alerta_accion.cget("text"))


if __name__ == "__main__":
    unittest.main()
