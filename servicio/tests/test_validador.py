"""Tests unitarios para la validación de contratos, tópicos y payloads (Issue #9).
No requieren broker MQTT ni Docker levantado.
"""

import math
import sys
import unittest
from pathlib import Path

# Asegurar que la raíz del proyecto esté en sys.path de forma nativa para encontrar 'comun'
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from comun import payloads, topics
from servicio.src import validador
from servicio.src.handlers import posicion

class TestContratosPayloadsYTopics(unittest.TestCase):
    """Verifica los contratos de tópicos y payloads según Paso 2 e Issue #9."""

    def test_parse_reporte_extrae_tipo_e_id(self):
        """Verifica que la identidad (tipo e id) se extraiga exclusivamente del tópico."""
        topic = "reportes/policia/pol-01/posicion"
        tipo, unidad, evento = topics.parse_reporte(topic)
        self.assertEqual(tipo, "policia")
        self.assertEqual(unidad, "pol-01")
        self.assertEqual(evento, "posicion")

        # Probar con ambulancia y bomberos
        tipo, unidad, evento = topics.parse_reporte("reportes/ambulancia/amb-02/posicion")
        self.assertEqual(tipo, "ambulancia")
        self.assertEqual(unidad, "amb-02")

        tipo, unidad, evento = topics.parse_reporte("reportes/bomberos/bom-03/posicion")
        self.assertEqual(tipo, "bomberos")
        self.assertEqual(unidad, "bom-03")

    def test_builders_topicos(self):
        """Verifica los constructores de tópicos oficiales de salida y alertas."""
        self.assertEqual(
            topics.flota_estado("policia", "pol-01"),
            "flota/policia/pol-01/estado",
        )
        self.assertEqual(
            topics.alertas("policia"),
            "alertas/policia",
        )

    def test_payload_posicion_campos_obligatorios(self):
        """Exige v, ts, lat y lon como obligatorios."""
        base = {"v": 1, "ts": 1730000000, "lat": -45.86, "lon": -67.48}

        # El payload base completo debe ser válido
        limpio = payloads.validar(payloads.POSICION, base)
        self.assertEqual(limpio["lat"], -45.86)

        # Faltan campos obligatorios
        for campo in ("v", "ts", "lat", "lon"):
            incompleto = dict(base)
            del incompleto[campo]
            with self.assertRaises(payloads.PayloadInvalido, msg=f"Debió fallar sin {campo}"):
                payloads.validar(payloads.POSICION, incompleto)

    def test_payload_posicion_rumbo_opcional(self):
        """Rumbo es opcional pero debe estar en [0, 360) si se envía."""
        base = {"v": 1, "ts": 1730000000, "lat": -45.86, "lon": -67.48}

        # Sin rumbo es válido
        limpio = payloads.validar(payloads.POSICION, base)
        self.assertNotIn("rumbo", limpio)

        # Rumbo válido
        con_rumbo = dict(base, rumbo=180.5)
        limpio = payloads.validar(payloads.POSICION, con_rumbo)
        self.assertEqual(limpio["rumbo"], 180.5)

        # Rumbos inválidos (< 0 o >= 360)
        for r_invalido in (-1.0, 360.0, 360.1, 400.0):
            with self.assertRaises(payloads.PayloadInvalido):
                payloads.validar(payloads.POSICION, dict(base, rumbo=r_invalido))

    def test_payload_posicion_rechaza_bool_y_nan_inf(self):
        """Rechaza booleanos disfrazados de int, NaN e Infinity."""
        base = {"v": 1, "ts": 1730000000, "lat": -45.86, "lon": -67.48}

        # Booleanos en campos numéricos
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, lat=True))
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, lon=False))
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, ts=True))

        # NaN e Infinity
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, lat=float("nan")))
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, lon=float("inf")))
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, ts=float("-inf")))

    def test_payload_posicion_rangos_geograficos(self):
        """Latitud en [-90, 90] y longitud en [-180, 180]."""
        base = {"v": 1, "ts": 1730000000, "lat": -45.86, "lon": -67.48}

        # Fuera de rango
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, lat=91.0))
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, lat=-90.1))
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, lon=180.1))
        with self.assertRaises(payloads.PayloadInvalido):
            payloads.validar(payloads.POSICION, dict(base, lon=-180.5))

    def test_payload_posicion_ignora_campos_desconocidos(self):
        """Campos desconocidos (como id o tipo inyectados) deben ignorarse sin fallar."""
        sucio = {
            "v": 1,
            "ts": 1730000000,
            "lat": -45.86,
            "lon": -67.48,
            "rumbo": 90.0,
            "id": "unidad_falsa",
            "tipo": "policia",
            "campo_fantasma": 12345,
        }
        limpio = payloads.validar(payloads.POSICION, sucio)

        # Solo deben permanecer los campos de POSICION
        self.assertEqual(set(limpio.keys()), {"v", "ts", "lat", "lon", "rumbo"})
        self.assertNotIn("id", limpio)
        self.assertNotIn("tipo", limpio)
        self.assertNotIn("campo_fantasma", limpio)


class TestValidadorPosicion(unittest.TestCase):
    """Verifica las reglas físicas y temporales del módulo validador.py (Paso 3 e Issue #9)."""

    def setUp(self):
        # Centro de Comodoro Rivadavia (punto seguro terrestre)
        self.centro_lat = -45.8641
        self.centro_lon = -67.4850
        self.hora_base = 1730000000.0

    def test_distancia_metros(self):
        """Calcula distancias geodésicas razonables."""
        # Misma coordenada = 0 metros
        d0 = validador.distancia_metros(self.centro_lat, self.centro_lon, self.centro_lat, self.centro_lon)
        self.assertAlmostEqual(d0, 0.0, places=2)

        # ~1 grado de latitud equivale a ~111.1 km
        d1 = validador.distancia_metros(-45.0, -67.0, -46.0, -67.0)
        self.assertTrue(110000.0 < d1 < 112000.0)

    def test_en_zona(self):
        """Verifica pertenencia a la zona terrestre vs mar del Golfo y puntos lejanos."""
        # Centro de Comodoro: adentro
        self.assertTrue(validador.en_zona(self.centro_lon, self.centro_lat))

        # Punto en el mar del Golfo San Jorge (-45.8700, -67.3000): afuera
        self.assertFalse(validador.en_zona(-67.3000, -45.8700))

        # Punto claramente fuera del Bounding Box (ej: Buenos Aires)
        self.assertFalse(validador.en_zona(-58.3816, -34.6037))

    def test_posicion_valida(self):
        """Un reporte sano dentro de Comodoro con reloj sincronizado debe ser aceptado."""
        payload = {
            "v": 1,
            "ts": self.hora_base,
            "lat": self.centro_lat,
            "lon": self.centro_lon,
        }
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=None, hora_servicio=self.hora_base
        )
        self.assertTrue(valido)
        self.assertIsNone(codigo)
        self.assertIsNone(detalle)

    def test_posicion_en_el_mar_rechazada(self):
        """Una coordenada en el agua del Golfo debe rechazarse por fuera_de_zona."""
        payload = {
            "v": 1,
            "ts": self.hora_base,
            "lat": -45.8700,
            "lon": -67.3000,
        }
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=None, hora_servicio=self.hora_base
        )
        self.assertFalse(valido)
        self.assertEqual(codigo, "fuera_de_zona")
        self.assertIn("fuera de la zona", detalle)

    def test_movimiento_valido(self):
        """Avanzar ~40 metros en 2 segundos (~20 m/s) debe ser aceptado."""
        ultimo_estado = {
            "ts": self.hora_base,
            "lat": self.centro_lat,
            "lon": self.centro_lon,
        }
        # 40 metros hacia el norte: delta_lat = 40 / 111132 ≈ 0.00036°
        nuevo_lat = self.centro_lat + (40.0 / 111132.0)
        payload = {
            "v": 1,
            "ts": self.hora_base + 2.0,
            "lat": nuevo_lat,
            "lon": self.centro_lon,
        }
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=ultimo_estado, hora_servicio=self.hora_base + 2.0
        )
        self.assertTrue(valido)
        self.assertIsNone(codigo)

    def test_salto_imposible(self):
        """Saltar >= 150 metros en 1 segundo (150 m/s > 75 m/s) debe rechazarse."""
        ultimo_estado = {
            "ts": self.hora_base,
            "lat": self.centro_lat,
            "lon": self.centro_lon,
        }
        # 160 metros hacia el norte en 1 segundo = 160 m/s
        nuevo_lat = self.centro_lat + (160.0 / 111132.0)
        payload = {
            "v": 1,
            "ts": self.hora_base + 1.0,
            "lat": nuevo_lat,
            "lon": self.centro_lon,
        }
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=ultimo_estado, hora_servicio=self.hora_base + 1.0
        )
        self.assertFalse(valido)
        self.assertEqual(codigo, "salto_imposible")
        self.assertIn("excede el límite máximo", detalle)

    def test_reloj_desfasado(self):
        """Un timestamp desfasado por más de 10s del reloj del servicio debe rechazarse."""
        # 15 segundos en el futuro (intento de disimular velocidad)
        payload = {
            "v": 1,
            "ts": self.hora_base + 15.0,
            "lat": self.centro_lat,
            "lon": self.centro_lon,
        }
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=None, hora_servicio=self.hora_base
        )
        self.assertFalse(valido)
        self.assertEqual(codigo, "ts_desfasado")

        # 15 segundos en el pasado
        payload["ts"] = self.hora_base - 15.0
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=None, hora_servicio=self.hora_base
        )
        self.assertFalse(valido)
        self.assertEqual(codigo, "ts_desfasado")

    def test_monotonia_temporal(self):
        """Un timestamp igual o anterior al último aceptado debe rechazarse."""
        ultimo_estado = {
            "ts": self.hora_base,
            "lat": self.centro_lat,
            "lon": self.centro_lon,
        }
        # Timestamp igual (duplicado)
        payload = {
            "v": 1,
            "ts": self.hora_base,
            "lat": self.centro_lat,
            "lon": self.centro_lon,
        }
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=ultimo_estado, hora_servicio=self.hora_base
        )
        self.assertFalse(valido)
        self.assertEqual(codigo, "ts_repetido_o_viejo")

        # Timestamp viejo (atrasado)
        payload["ts"] = self.hora_base - 1.0
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=ultimo_estado, hora_servicio=self.hora_base
        )
        self.assertFalse(valido)
        self.assertEqual(codigo, "ts_repetido_o_viejo")

    def test_reconeccion_omite_salto_imposible(self):
        """Si la unidad se reconectó tras estar offline, el primer reporte no rechaza por salto."""
        ultimo_estado = {
            "ts": self.hora_base - 100.0,
            "lat": self.centro_lat,
            "lon": self.centro_lon,
            "recien_conectado": True,
        }
        # Coordenada a 2 km dentro de la ciudad
        nuevo_lat = self.centro_lat + (2000.0 / 111132.0)
        payload = {
            "v": 1,
            "ts": self.hora_base,
            "lat": nuevo_lat,
            "lon": self.centro_lon,
        }
        valido, codigo, detalle = validador.validar_posicion(
            payload, ultimo_estado=ultimo_estado, hora_servicio=self.hora_base
        )
        self.assertTrue(valido)
        self.assertIsNone(codigo)


class MockCliente:
    """Simulador simple de cliente MQTT para registrar qué publicó el servicio."""

    def __init__(self):
        self.mensajes = []

    def publish(self, dato, topic, qos=0, retain=False):
        self.mensajes.append({"topic": topic, "dato": dato, "qos": qos, "retain": retain})


class TestHandlerPosicionSimple(unittest.TestCase):
    """Tests simples para el flujo de publicación oficial y alertas en handlers/posicion.py."""

    def setUp(self):
        posicion.reiniciar_memoria_posiciones()
        self.cliente = MockCliente()
        self.hora = 1730000000.0
        self.lat_centro = -45.8641
        self.lon_centro = -67.4850

    def test_posicion_valida_publica_en_flota(self):
        """Una posición válida se publica en flota/{tipo}/{unidad}/estado (QoS 1, retain=True)."""
        raw = payloads.posicion(lat=self.lat_centro, lon=self.lon_centro, ts=self.hora)
        posicion.procesar(self.cliente, "policia", "pol-01", raw, hora_servicio=self.hora)

        # Debe haber 1 publicación en flota y 0 en alertas
        self.assertEqual(len(self.cliente.mensajes), 1)
        publicado = self.cliente.mensajes[0]
        self.assertEqual(publicado["topic"], "flota/policia/pol-01/estado")
        self.assertEqual(publicado["qos"], 1)
        self.assertTrue(publicado["retain"])

    def test_posicion_en_el_mar_publica_alerta(self):
        """Una posición en el mar se publica en alertas/{tipo} (QoS 1, retain=False) y no en flota."""
        raw_mar = payloads.posicion(lat=-45.8700, lon=-67.3000, ts=self.hora)
        posicion.procesar(self.cliente, "policia", "pol-01", raw_mar, hora_servicio=self.hora)

        # Debe haber 1 publicación en alertas y 0 en flota
        self.assertEqual(len(self.cliente.mensajes), 1)
        alerta = self.cliente.mensajes[0]
        self.assertEqual(alerta["topic"], "alertas/policia")
        self.assertEqual(alerta["qos"], 1)
        self.assertFalse(alerta["retain"])
        self.assertEqual(alerta["dato"]["motivo"], "posicion_invalida")

    def test_salto_imposible_y_recuperacion(self):
        """Un salto imposible genera alerta, no pisa la flota y el reporte siguiente sano se acepta."""
        # 1. Reporte sano inicial en el centro
        p0 = payloads.posicion(lat=self.lat_centro, lon=self.lon_centro, ts=self.hora)
        posicion.procesar(self.cliente, "policia", "pol-01", p0, hora_servicio=self.hora)

        # 2. Salto imposible (+1.5 km en 1 segundo)
        p1 = payloads.posicion(lat=self.lat_centro + 0.014, lon=self.lon_centro, ts=self.hora + 1.0)
        posicion.procesar(self.cliente, "policia", "pol-01", p1, hora_servicio=self.hora + 1.0)

        # 3. Reporte sano (+40 metros en 2 segundos respecto al reporte 1)
        p2 = payloads.posicion(
            lat=self.lat_centro + (40.0 / 111132.0), lon=self.lon_centro, ts=self.hora + 2.0
        )
        posicion.procesar(self.cliente, "policia", "pol-01", p2, hora_servicio=self.hora + 2.0)

        # Filtramos qué se publicó en cada tópico
        en_flota = [m for m in self.cliente.mensajes if m["topic"] == "flota/policia/pol-01/estado"]
        en_alertas = [m for m in self.cliente.mensajes if m["topic"] == "alertas/policia"]

        # Debe haber 2 publicaciones en flota (reporte 1 y reporte 3) y 1 alerta (reporte 2)
        self.assertEqual(len(en_flota), 2)
        self.assertEqual(len(en_alertas), 1)


if __name__ == "__main__":
    unittest.main()
