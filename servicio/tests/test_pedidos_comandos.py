"""Tests unitarios para el procesamiento de pedidos y comandos (Issue #12).
Valida:
1. Flujo completo de pedidos de auxilio y asignación de incidentes (eid).
2. Deduplicación e idempotencia por (clientid + id) en pedidos y (despachador + id) en comandos.
3. Control de límites (Rate Limiting) por cliente y global.
4. Asignación exclusiva de unidades conectadas (online) y disponibles (libres).
5. Avisos de rechazo en respuestas/{despachador} ante infracciones de negocio.
6. Liberación de unidades y cierre de emergencias con retained vacío.
"""

import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from comun import payloads
from servicio.src.bd import BD
from servicio.src.handlers import comandos, pedidos, pub_salidas
from servicio.src.main import ServicioCentral


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class TestPedidosYComandos(unittest.TestCase):
    def setUp(self):
        self.bd = BD(":memory:")
        self.cliente = MagicMock()
        pedidos.reset_limites()

    def _publicaciones(self):
        return [
            (c.kwargs.get("topic") or (c.args[1] if len(c.args) > 1 else None),
             c.args[0] if c.args else c.kwargs.get("dato"),
             c.kwargs.get("retain", False),
             c.kwargs.get("qos", 1))
            for c in self.cliente.publish.call_args_list
        ]

    def _publicacion_en(self, topic):
        for t, payload, retain, qos in self._publicaciones():
            if t == topic:
                return payload, retain, qos
        self.fail(f"No se publicó nada en {topic}")

    # =========================================================================
    # TESTS DE PEDIDOS
    # =========================================================================

    def test_pedido_valido_crea_emergencia_y_publica(self):
        payload_bytes = payloads.pedido(lat=-45.864, lon=-67.485, id="ped-01")
        eid = pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)

        self.assertEqual(eid, "E1")
        # Verificar estado en BD
        emerg = self.bd.leer_emergencia("E1")
        self.assertIsNotNone(emerg)
        self.assertEqual(emerg["tipo"], "ambulancia")
        self.assertAlmostEqual(emerg["lat"], -45.864, places=3)
        self.assertIsNone(emerg["unidad"])

        # Verificar publicación MQTT (QoS 1, retain=True)
        payload, retain, qos = self._publicacion_en("emergencias/ambulancia/E1")
        self.assertEqual(payload["eid"], "E1")
        self.assertEqual(payload["estado"], "sin_asignar")
        self.assertNotIn("unidad", payload)
        self.assertTrue(retain)
        self.assertEqual(qos, 1)

    def test_pedido_idempotente_mismo_id_no_crea_otra_emergencia(self):
        payload_bytes = payloads.pedido(lat=-45.864, lon=-67.485, id="ped-repetido")

        # Primer envío
        eid1 = pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)
        self.assertEqual(eid1, "E1")
        self.assertEqual(self.cliente.publish.call_count, 1)

        # Segundo envío con el mismo clientid + id
        eid2 = pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)
        self.assertEqual(eid2, "E1")
        # Republica la misma emergencia, no crea otra
        self.assertEqual(self.bd.generar_siguiente_eid(), "E2")
        self.assertEqual(
            [t for t, _, _, _ in self._publicaciones()],
            ["emergencias/ambulancia/E1", "emergencias/ambulancia/E1"],
        )

    def test_pedido_reenviado_tras_caida_publica_la_emergencia(self):
        payload_bytes = payloads.pedido(lat=-45.864, lon=-67.485, id="ped-caida")

        # El servicio se cae después del commit y antes de publicar: el broker reenvía el pedido
        self.cliente.publish.side_effect = ConnectionError("caida simulada")
        with self.assertRaises(ConnectionError):
            pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)
        self.cliente.reset_mock(side_effect=True)

        eid = pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)

        self.assertEqual(eid, "E1")
        emerg, retain, _ = self._publicacion_en("emergencias/ambulancia/E1")
        self.assertEqual(emerg["estado"], "sin_asignar")
        self.assertTrue(retain)

    def test_pedido_duplicado_con_otro_tipo_republica_en_el_tipo_original(self):
        payload_bytes = payloads.pedido(lat=-45.864, lon=-67.485, id="ped-tipo")
        pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)
        self.cliente.reset_mock()

        pedidos.procesar(self.cliente, self.bd, "publico-123", "policia", payload_bytes)

        self.assertEqual([t for t, _, _, _ in self._publicaciones()], ["emergencias/ambulancia/E1"])

    def test_pedido_duplicado_no_revive_emergencia_cerrada(self):
        payload_bytes = payloads.pedido(lat=-45.864, lon=-67.485, id="ped-cerrado")
        pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)
        with self.bd.transaccion():
            self.bd.cerrar_emergencia("E1")
        self.cliente.reset_mock()

        eid = pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)

        self.assertEqual(eid, "E1")
        self.cliente.publish.assert_not_called()

    def test_pedido_mismo_id_distinto_clientid_crea_dos_emergencias(self):
        # Mismo id generado por dos teléfonos diferentes
        payload_bytes = payloads.pedido(lat=-45.864, lon=-67.485, id="id-comun")

        eid1 = pedidos.procesar(self.cliente, self.bd, "publico-111", "policia", payload_bytes)
        eid2 = pedidos.procesar(self.cliente, self.bd, "publico-222", "policia", payload_bytes)

        self.assertEqual(eid1, "E1")
        self.assertEqual(eid2, "E2")
        self.assertIsNotNone(self.bd.leer_emergencia("E1"))
        self.assertIsNotNone(self.bd.leer_emergencia("E2"))

    def test_pedido_fuera_de_zona_se_descarta(self):
        # Coordenadas en el océano Atlántico
        payload_bytes = payloads.pedido(lat=-45.864, lon=-60.000, id="ped-mar")
        eid = pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", payload_bytes)

        self.assertIsNone(eid)
        self.cliente.publish.assert_not_called()

    def test_pedido_invalido_se_descarta(self):
        eid = pedidos.procesar(self.cliente, self.bd, "publico-123", "ambulancia", b"no-json")
        self.assertIsNone(eid)
        self.cliente.publish.assert_not_called()

    def test_rate_limit_por_clientid(self):
        # Enviar MAX_PEDIDOS_CLIENTE (5) pedidos válidos
        for i in range(pedidos.MAX_PEDIDOS_CLIENTE):
            payload_bytes = payloads.pedido(lat=-45.864, lon=-67.485, id=f"ped-lim-{i}")
            eid = pedidos.procesar(self.cliente, self.bd, "publico-spam", "ambulancia", payload_bytes)
            self.assertIsNotNone(eid)

        # El 6to debe ser descartado por límite de cliente
        payload_bytes_extra = payloads.pedido(lat=-45.864, lon=-67.485, id="ped-lim-extra")
        eid_extra = pedidos.procesar(self.cliente, self.bd, "publico-spam", "ambulancia", payload_bytes_extra)
        self.assertIsNone(eid_extra)

    def test_rate_limit_global(self):
        for i in range(pedidos.MAX_PEDIDOS_GLOBAL):
            self.assertTrue(pedidos._verificar_limites(f"publico-{i}", 1000.0))

        self.assertFalse(pedidos._verificar_limites("publico-extra", 1000.0))
        # Pasada la ventana vuelve a aceptar
        self.assertTrue(pedidos._verificar_limites("publico-extra", 1000.0 + pedidos.VENTANA_TIEMPO_SEG + 1))

    def test_rate_limit_no_acumula_clientids_en_memoria(self):
        # Clientids que piden una vez y no vuelven, repartidos para no tocar el límite global
        for i in range(1000):
            pedidos._verificar_limites(f"publico-{i}", 1000.0 + i * (pedidos.VENTANA_TIEMPO_SEG + 1))
        self.assertEqual(len(pedidos._pedidos_por_cliente), 1)

        # Rechazado por el límite global: tampoco deja una entrada
        pedidos.reset_limites()
        for i in range(pedidos.MAX_PEDIDOS_GLOBAL):
            pedidos._verificar_limites(f"publico-{i}", 1000.0)
        pedidos._verificar_limites("publico-extra", 1000.0)
        self.assertNotIn("publico-extra", pedidos._pedidos_por_cliente)

    def test_eid_secuencial_no_reutiliza_cerradas(self):
        # 1. Crear E1
        p1 = payloads.pedido(lat=-45.864, lon=-67.485, id="p-1")
        eid1 = pedidos.procesar(self.cliente, self.bd, "publico-1", "ambulancia", p1)
        self.assertEqual(eid1, "E1")

        # 2. Asignar y cerrar E1
        with self.bd.transaccion():
            self.bd.actualizar_presencia("amb-01", "ambulancia", "online")
            self.bd.asignar_emergencia("E1", "amb-01")
            self.bd.cerrar_emergencia("E1")

        # 3. El siguiente pedido debe ser E2, no reutilizar E1
        p2 = payloads.pedido(lat=-45.864, lon=-67.485, id="p-2")
        eid2 = pedidos.procesar(self.cliente, self.bd, "publico-2", "ambulancia", p2)
        self.assertEqual(eid2, "E2")

    # =========================================================================
    # TESTS DE COMANDOS
    # =========================================================================

    def _preparar_unidad(self, unidad="amb-01", tipo="ambulancia", conexion="online"):
        with self.bd.transaccion():
            self.bd.actualizar_presencia(unidad, tipo, conexion)

    def _preparar_emergencia(self, eid="E1", tipo="ambulancia", unidad=None):
        with self.bd.transaccion():
            self.bd.crear_emergencia(eid, tipo, -45.864, -67.485, 100.0)
            if unidad:
                self.bd.asignar_emergencia(eid, unidad)

    def test_asignar_unidad_libre_y_online_exitoso(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia")

        payload_bytes = payloads.comando(accion="asignar", eid="E1", id="cmd-asig-01")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertTrue(resp["ok"])
        self.assertEqual(self.bd.leer_unidad("amb-01")["eid"], "E1")
        self.assertEqual(self.bd.leer_emergencia("E1")["unidad"], "amb-01")

        # Flota ocupada con retain=True
        flota, retain, _ = self._publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(flota["disponibilidad"], "ocupada")
        self.assertEqual(flota["eid"], "E1")
        self.assertTrue(retain)

        # Emergencia asignada con retain=True
        emerg, retain_e, _ = self._publicacion_en("emergencias/ambulancia/E1")
        self.assertEqual(emerg["estado"], "asignada")
        self.assertEqual(emerg["unidad"], "amb-01")
        self.assertTrue(retain_e)

        # Respuesta en respuestas/desp-01 (sin retain)
        resp_msg, retain_r, _ = self._publicacion_en("respuestas/desp-01")
        self.assertEqual(resp_msg["id"], "cmd-asig-01")
        self.assertTrue(resp_msg["ok"])
        self.assertFalse(retain_r)

    def test_asignar_unidad_desconectada_rechazado_y_avisado(self):
        self._preparar_unidad("amb-01", "ambulancia", "offline")
        self._preparar_emergencia("E1", "ambulancia")

        payload_bytes = payloads.comando(accion="asignar", eid="E1", id="cmd-offline")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertFalse(resp["ok"])
        self.assertIn("desconectada", resp["motivo"].lower())
        self.assertIsNone(self.bd.leer_emergencia("E1")["unidad"])

        # Aviso al despachador en respuestas/desp-01
        resp_msg, _, _ = self._publicacion_en("respuestas/desp-01")
        self.assertFalse(resp_msg["ok"])
        self.assertIn("offline", resp_msg["motivo"].lower())

    def test_asignar_unidad_ocupada_rechazado(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia", unidad="amb-01")
        self._preparar_emergencia("E2", "ambulancia")

        payload_bytes = payloads.comando(accion="asignar", eid="E2", id="cmd-ocupada")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertFalse(resp["ok"])
        self.assertIn("ocupada", resp["motivo"].lower())
        self.assertIsNone(self.bd.leer_emergencia("E2")["unidad"])

        resp_msg, _, _ = self._publicacion_en("respuestas/desp-01")
        self.assertFalse(resp_msg["ok"])

    def test_asignar_emergencia_ya_asignada_rechazado(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_unidad("amb-02", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia", unidad="amb-02")

        payload_bytes = payloads.comando(accion="asignar", eid="E1", id="cmd-ya-asig")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertFalse(resp["ok"])
        self.assertIn("ya asignada", resp["motivo"].lower())

    def test_asignar_emergencia_no_encontrada_rechazado(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")

        payload_bytes = payloads.comando(accion="asignar", eid="E-fantasma", id="cmd-404")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertFalse(resp["ok"])
        self.assertIn("no encontrada", resp["motivo"].lower())

    def test_liberar_unidad_ocupada_cierra_emergencia_con_retained_vacio(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia", unidad="amb-01")

        payload_bytes = payloads.comando(accion="liberar", id="cmd-lib-01")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertTrue(resp["ok"])
        # La emergencia desaparece de la BD
        self.assertIsNone(self.bd.leer_emergencia("E1"))
        # La unidad queda libre
        self.assertIsNone(self.bd.leer_unidad("amb-01")["eid"])

        # Flota libre publicada
        flota, _, _ = self._publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(flota["disponibilidad"], "libre")
        self.assertNotIn("eid", flota)

        # Emergencia cerrada publicada con payload vacío b"" y retain=True
        emerg_payload, retain_e, _ = self._publicacion_en("emergencias/ambulancia/E1")
        self.assertEqual(emerg_payload, b"")
        self.assertTrue(retain_e)

        # Confirmación enviada a respuestas/desp-01
        resp_msg, _, _ = self._publicacion_en("respuestas/desp-01")
        self.assertTrue(resp_msg["ok"])

    def test_liberar_unidad_ya_libre_rechazado(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")

        payload_bytes = payloads.comando(accion="liberar", id="cmd-lib-fail")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertFalse(resp["ok"])
        self.assertIn("ya está libre", resp["motivo"].lower())

    def test_liberar_con_tipo_cruzado_rechazado_sin_tocar_nada(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia", unidad="amb-01")

        payload_bytes = payloads.comando(accion="liberar", id="cmd-cruzado")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "policia", "amb-01", payload_bytes)

        self.assertFalse(resp["ok"])
        self.assertIn("incompatible", resp["motivo"].lower())
        self.assertEqual(self.bd.leer_unidad("amb-01")["eid"], "E1")
        self.assertIsNotNone(self.bd.leer_emergencia("E1"))
        self.assertEqual([t for t, _, _, _ in self._publicaciones()], ["respuestas/desp-01"])

    def test_comando_idempotente_reenvia_misma_respuesta(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia")

        payload_bytes = payloads.comando(accion="asignar", eid="E1", id="cmd-idem")

        # 1. Primera ejecución
        resp1 = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)
        self.assertTrue(resp1["ok"])

        # Contar cuántas veces se publicó en respuestas/desp-01
        cant_resp_1 = len([t for t, _, _, _ in self._publicaciones() if t == "respuestas/desp-01"])
        self.assertEqual(cant_resp_1, 1)

        # 2. Segunda ejecución idéntica
        resp2 = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)
        self.assertTrue(resp2["ok"])

        # Se reenvió la respuesta por respuestas/desp-01
        cant_resp_2 = len([t for t, _, _, _ in self._publicaciones() if t == "respuestas/desp-01"])
        self.assertEqual(cant_resp_2, 2)

    def _simular_caida_tras_commit(self, tipo, unidad, payload_bytes):
        """Corre el comando cayéndose en la primera publicación y deja el cliente limpio para el reenvío."""
        self.cliente.publish.side_effect = ConnectionError("caida simulada")
        with self.assertRaises(ConnectionError):
            comandos.procesar(self.cliente, self.bd, "desp-01", tipo, unidad, payload_bytes)
        self.cliente.reset_mock(side_effect=True)

    def test_asignar_reenviado_tras_caida_republica_estado(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia")
        payload_bytes = payloads.comando(accion="asignar", eid="E1", id="cmd-caida")

        self._simular_caida_tras_commit("ambulancia", "amb-01", payload_bytes)
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertTrue(resp["ok"])
        flota, retain_f, _ = self._publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(flota["disponibilidad"], "ocupada")
        self.assertTrue(retain_f)
        emerg, retain_e, _ = self._publicacion_en("emergencias/ambulancia/E1")
        self.assertEqual(emerg["estado"], "asignada")
        self.assertTrue(retain_e)
        self.assertTrue(self._publicacion_en("respuestas/desp-01")[0]["ok"])

    def test_liberar_reenviado_tras_caida_cierra_emergencia_sin_eid_en_el_comando(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia", unidad="amb-01")
        payload_bytes = payloads.comando(accion="liberar", id="cmd-lib-caida")

        self._simular_caida_tras_commit("ambulancia", "amb-01", payload_bytes)
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertTrue(resp["ok"])
        flota, _, _ = self._publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(flota["disponibilidad"], "libre")
        emerg, retain_e, _ = self._publicacion_en("emergencias/ambulancia/E1")
        self.assertEqual(emerg, b"")
        self.assertTrue(retain_e)

    def test_asignar_duplicado_tardio_no_revive_emergencia_cerrada(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia")
        asignar = payloads.comando(accion="asignar", eid="E1", id="cmd-asig-tarde")
        comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", asignar)
        liberar = payloads.comando(accion="liberar", id="cmd-lib-tarde")
        comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", liberar)
        self.cliente.reset_mock()

        comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", asignar)

        self.assertEqual(self._publicacion_en("emergencias/ambulancia/E1")[0], b"")
        flota, _, _ = self._publicacion_en("flota/ambulancia/amb-01/estado")
        self.assertEqual(flota["disponibilidad"], "libre")
        self.assertIsNone(self.bd.leer_emergencia("E1"))

    def test_rechazo_duplicado_solo_reenvia_la_respuesta(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        payload_bytes = payloads.comando(accion="liberar", id="cmd-rechazo")
        primera = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)
        self.cliente.reset_mock()

        segunda = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)

        self.assertEqual(segunda, primera)
        self.assertEqual([t for t, _, _, _ in self._publicaciones()], ["respuestas/desp-01"])

    def test_rechazo_sin_poder_guardar_no_avisa_y_se_puede_reevaluar(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia")
        with self.bd.transaccion():
            self.bd.actualizar_presencia("amb-01", "ambulancia", "offline")
        payload_bytes = payloads.comando(accion="asignar", eid="E1", id="cmd-sin-guardar")

        with patch.object(self.bd, "guardar_procesado", side_effect=sqlite3.OperationalError("disco lleno")):
            resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)
        self.assertIsNone(resp)
        self.cliente.publish.assert_not_called()

        # Sin rechazo informado, el mismo id se puede aplicar cuando la unidad vuelve
        with self.bd.transaccion():
            self.bd.actualizar_presencia("amb-01", "ambulancia", "online")
        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)
        self.assertTrue(resp["ok"])

    def test_respuesta_enviada_guardada_y_reenviada_es_la_misma(self):
        self._preparar_unidad("amb-01", "ambulancia", "online")
        self._preparar_emergencia("E1", "ambulancia")
        payload_bytes = payloads.comando(accion="asignar", eid="E1", id="cmd-misma")

        resp = comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)
        enviada = self._publicacion_en("respuestas/desp-01")[0]
        guardada = json.loads(self.bd.leer_id_procesado("comando", "desp-01", "cmd-misma")["resultado"])["respuesta"]
        self.cliente.reset_mock()
        comandos.procesar(self.cliente, self.bd, "desp-01", "ambulancia", "amb-01", payload_bytes)
        reenviada = self._publicacion_en("respuestas/desp-01")[0]

        self.assertEqual(enviada, resp)
        self.assertEqual(guardada, enviada)
        self.assertEqual(reenviada, enviada)

    # =========================================================================
    # TESTS DE ENRUTAMIENTO INTEGRADO (SERVICIO CENTRAL)
    # =========================================================================

    def test_router_on_pedido_y_on_comando_integracion(self):
        servicio = ServicioCentral(
            ip_broker="127.0.0.1",
            puerto=1883,
            usuario="servicio",
            ca_certs=None,
            ruta_bd=":memory:",
        )
        servicio.cliente = MagicMock()

        # 1. Llega pedido por MQTT
        p_bytes = payloads.pedido(lat=-45.864, lon=-67.485, id="ped-int-01")
        msg_pedido = FakeMessage("pedidos/publico-int/ambulancia", p_bytes)
        servicio._on_pedido(None, None, msg_pedido)

        self.assertIsNotNone(servicio.bd.leer_emergencia("E1"))

        # 2. Llega presencia de unidad
        pres_bytes = payloads.presencia("online")
        msg_pres = FakeMessage("reportes/ambulancia/amb-99/presencia", pres_bytes)
        servicio._on_presencia(None, None, msg_pres)

        # 3. Despachador asigna unidad a E1
        cmd_bytes = payloads.comando(accion="asignar", eid="E1", id="cmd-int-01")
        msg_cmd = FakeMessage("comandos/desp-01/ambulancia/amb-99", cmd_bytes)
        servicio._on_comando(None, None, msg_cmd)

        self.assertEqual(servicio.bd.leer_unidad("amb-99")["eid"], "E1")

        # 4. Despachador libera unidad
        cmd_lib = payloads.comando(accion="liberar", id="cmd-int-02")
        msg_lib = FakeMessage("comandos/desp-01/ambulancia/amb-99", cmd_lib)
        servicio._on_comando(None, None, msg_lib)

        self.assertIsNone(servicio.bd.leer_emergencia("E1"))
        self.assertIsNone(servicio.bd.leer_unidad("amb-99")["eid"])


if __name__ == "__main__":
    unittest.main()
