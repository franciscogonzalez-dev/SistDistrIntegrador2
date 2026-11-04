"""Tests del handler de presencia (Issue #10).
No requieren broker MQTT: base SQLite en memoria y cliente MQTT falso (MagicMock).
"""

import unittest
from unittest.mock import MagicMock

from comun import payloads
from servicio.src.bd import BD
from servicio.src.handlers import presencia


class TestPresencia(unittest.TestCase):
    def setUp(self):
        self.bd = BD(":memory:")
        self.cliente = MagicMock()

    # helpers

    def procesar(self, estado, unidad="amb-01"):
        presencia.procesar(self.cliente, self.bd, "ambulancia", unidad, payloads.presencia(estado))

    def publicaciones(self):
        """Lista de (topic, payload, retain) en el orden en que se publicaron."""
        return [
            (c.kwargs["topic"], c.args[0], c.kwargs.get("retain", False))
            for c in self.cliente.publish.call_args_list
        ]

    def publicacion_en(self, topic):
        for t, payload, retain in self.publicaciones():
            if t == topic:
                return payload, retain
        self.fail(f"no se publico nada en {topic}")

    def asignar_emergencia(self, eid="E42", unidad="amb-01"):
        # lo que va a hacer #12; BD todavia no tiene un metodo para crear emergencias
        with self.bd.transaccion():
            self.bd._conn.execute(
                "INSERT INTO emergencias (eid, tipo, lat, lon, ts, unidad) VALUES (?, 'ambulancia', -45.87, -67.50, 1, ?)",
                (eid, unidad),
            )

    # online / offline sin emergencia

    def test_online_de_unidad_nueva_se_guarda_y_publica(self):
        self.procesar("online")

        self.assertEqual(self.bd.leer_unidad("amb-01")["conexion"], "online")
        payload, retain = self.publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(payload["conexion"], "online")
        self.assertEqual(payload["disponibilidad"], "libre")
        self.assertTrue(retain)

    def test_mismo_estado_dos_veces_no_publica(self):
        self.procesar("online")
        self.cliente.reset_mock()

        self.procesar("online")  # ej: el retained que llega al reconectar

        self.cliente.publish.assert_not_called()

    def test_offline_de_unidad_desconocida_no_publica(self):
        # sin fila en la bd cuenta como offline: no cambia nada
        self.procesar("offline")

        self.cliente.publish.assert_not_called()
        self.assertIsNone(self.bd.leer_unidad("amb-01"))

    def test_offline_sin_emergencia_publica_flota_sin_alerta(self):
        self.procesar("online")
        self.cliente.reset_mock()

        self.procesar("offline")

        payload, _ = self.publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(payload["conexion"], "offline")
        topics = [t for t, _, _ in self.publicaciones()]
        self.assertNotIn("alertas/ambulancia", topics)

    # unidad ocupada que se cae (duda 8)

    def test_ocupada_que_se_cae_queda_libre_offline_y_sin_eid(self):
        self.procesar("online")
        self.asignar_emergencia()

        self.procesar("offline")

        fila = self.bd.leer_unidad("amb-01")
        self.assertEqual(fila["conexion"], "offline")
        self.assertIsNone(fila["eid"])
        self.assertIsNone(self.bd.leer_emergencia("E42")["unidad"])

    def test_ocupada_que_se_cae_publica_flota_emergencia_y_alerta_en_orden(self):
        self.procesar("online")
        self.asignar_emergencia()
        self.cliente.reset_mock()

        self.procesar("offline")

        topics = [t for t, _, _ in self.publicaciones()]
        self.assertEqual(topics, [
            "flota/ambulancia/amb-01/estado",
            "emergencias/ambulancia/E42",
            "alertas/ambulancia",
        ])

        flota, _ = self.publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(flota["disponibilidad"], "libre")
        self.assertNotIn("eid", flota)

        emergencia, retain = self.publicacion_en("emergencias/ambulancia/E42")
        self.assertEqual(emergencia["estado"], "sin_asignar")
        self.assertNotIn("unidad", emergencia)
        self.assertTrue(retain)

        alerta, retain = self.publicacion_en("alertas/ambulancia")
        self.assertEqual(alerta["motivo"], "unidad_desconectada")
        self.assertEqual(alerta["unidad"], "amb-01")
        self.assertEqual(alerta["eid"], "E42")
        self.assertFalse(retain)

    def test_vuelve_online_con_emergencia_no_la_desasigna(self):
        # estado que no deberia darse (#12 no asigna unidades offline), pero si pasa no se pierde la emergencia
        self.procesar("online")
        self.asignar_emergencia()
        with self.bd.transaccion():
            self.bd.actualizar_presencia("amb-01", "ambulancia", "offline")
        self.cliente.reset_mock()

        self.procesar("online")

        self.assertEqual(self.bd.leer_emergencia("E42")["unidad"], "amb-01")
        topics = [t for t, _, _ in self.publicaciones()]
        self.assertEqual(topics, ["flota/ambulancia/amb-01/estado"])

    def test_cuando_vuelve_queda_libre(self):
        self.procesar("online")
        self.asignar_emergencia()
        self.procesar("offline")
        self.cliente.reset_mock()

        self.procesar("online")

        payload, _ = self.publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(payload["conexion"], "online")
        self.assertEqual(payload["disponibilidad"], "libre")

    # payload invalido

    def test_payload_invalido_no_toca_la_bd_ni_publica(self):
        presencia.procesar(self.cliente, self.bd, "ambulancia", "amb-01", b'{"v":1,"estado":"conectado"}')

        self.assertIsNone(self.bd.leer_unidad("amb-01"))
        self.cliente.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
