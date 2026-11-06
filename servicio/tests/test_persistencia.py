"""Tests de persistencia en SQLite (Issue #11).
No requieren broker MQTT: usan un archivo temporal, porque con ":memory:" la base
desaparece al cerrarla y no probarian nada.
"""

import os
import sqlite3
import tempfile
import unittest

from servicio.src.bd import BD


class TestPersistencia(unittest.TestCase):
    def setUp(self):
        self.carpeta = tempfile.TemporaryDirectory()
        self.addCleanup(self.carpeta.cleanup)
        self.ruta = os.path.join(self.carpeta.name, "bd_prueba.db")
        self.bd = BD(self.ruta)
        self.addCleanup(self._cerrar)

    def _cerrar(self):
        self.bd.cerrar_conn()

    def _reabrir(self):
        #simula que el servicio murio y volvio a arrancar con el mismo archivo
        self.bd.cerrar_conn()
        self.bd = BD(self.ruta)

    def test_id_procesado_sobrevive_al_reinicio(self):
        with self.bd.transaccion():
            self.bd.guardar_procesado("pedido", "publico-abc", "p-1", "E42", 100.0)

        self._reabrir()

        fila = self.bd.leer_id_procesado("pedido", "publico-abc", "p-1")
        self.assertIsNotNone(fila)
        self.assertEqual(fila["resultado"], "E42")

    def test_id_nuevo_devuelve_none(self):
        self.assertIsNone(self.bd.leer_id_procesado("pedido", "publico-abc", "p-1"))

    def test_mismo_id_de_otro_origen_o_clase_no_es_duplicado(self):
        with self.bd.transaccion():
            self.bd.guardar_procesado("pedido", "publico-abc", "x-1", "E1", 100.0)
            self.bd.guardar_procesado("pedido", "publico-def", "x-1", "E2", 100.0)
            self.bd.guardar_procesado("comando", "publico-abc", "x-1", "{}", 100.0)

        self.assertEqual(self.bd.leer_id_procesado("pedido", "publico-abc", "x-1")["resultado"], "E1")
        self.assertEqual(self.bd.leer_id_procesado("pedido", "publico-def", "x-1")["resultado"], "E2")
        self.assertEqual(self.bd.leer_id_procesado("comando", "publico-abc", "x-1")["resultado"], "{}")

    def test_guardar_dos_veces_el_mismo_id_falla(self):
        with self.bd.transaccion():
            self.bd.guardar_procesado("pedido", "publico-abc", "p-1", "E42", 100.0)

        with self.assertRaises(sqlite3.IntegrityError):
            with self.bd.transaccion():
                self.bd.guardar_procesado("pedido", "publico-abc", "p-1", "E99", 200.0)

        self.assertEqual(self.bd.leer_id_procesado("pedido", "publico-abc", "p-1")["resultado"], "E42")

    def test_sin_commit_no_queda_guardado(self):
        #si el servicio muere a mitad de la transaccion, el broker reenvia el mensaje
        #y tiene que procesarse como nuevo
        with self.assertRaises(RuntimeError):
            with self.bd.transaccion():
                self.bd.guardar_procesado("pedido", "publico-abc", "p-1", "E42", 100.0)
                raise RuntimeError("caida simulada antes del commit")

        self._reabrir()

        self.assertIsNone(self.bd.leer_id_procesado("pedido", "publico-abc", "p-1"))

    def test_estado_de_la_flota_sobrevive_al_reinicio(self):
        with self.bd.transaccion():
            self.bd.actualizar_presencia("amb-01", "ambulancia", "online")
            self.bd.guardar_posicion("ambulancia", "amb-01", -45.86, -67.48, 90.0, 100.0)

        self._reabrir()

        fila = self.bd.leer_unidad("amb-01")
        self.assertEqual(fila["conexion"], "online")
        self.assertEqual(fila["lat"], -45.86)
        self.assertEqual(fila["lon"], -67.48)
        self.assertEqual(fila["rumbo"], 90.0)
        self.assertEqual(fila["ts_posicion"], 100.0)


if __name__ == "__main__":
    unittest.main()
