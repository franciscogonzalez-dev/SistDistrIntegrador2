"""Handler de reportes de posición (reportes/{tipo}/{unidad}/posicion).

Implementa el flujo de la Issue #9:
1. Extrae identidad exclusivamente del tópico (tipo, unidad).
2. Valida estructura con comun.payloads. Si es inválido -> emite alerta en alertas/{tipo}.
3. Valida física y tiempo con servicio.src.validador contra la última posición guardada en la BD.
   Si es inválido -> emite alerta en alertas/{tipo} y no toca la BD ni la flota.
4. Si es válido -> guarda la posición y publica en flota/{tipo}/{unidad}/estado (QoS 1, retain=True).
"""

import logging
import sqlite3
import time
from comun import payloads
from servicio.src import validador
from . import pub_salidas

logger = logging.getLogger("servicio.posicion")


def _descartar(cliente, tipo, unidad, codigo, detalle):
    """Registra la posición descartada y avisa al despachador en alertas/{tipo}."""
    logger.warning("Posición descartada de %s (%s) [%s]: %s", unidad, tipo, codigo, detalle)
    pub_salidas.publicar_alerta(cliente, tipo, "posicion_invalida", detalle, unidad, None)


def procesar(cliente, bd, tipo, unidad, raw_payload, hora_servicio=None):
    """Procesa un reporte de telemetría emitido por una unidad."""
    if hora_servicio is None:
        hora_servicio = time.time()

    # 1. Validación estructural del JSON
    try:
        datos = payloads.validar(payloads.POSICION, raw_payload)
    except payloads.PayloadInvalido as e:
        _descartar(cliente, tipo, unidad, "payload_invalido", f"Payload con estructura inválida: {e}")
        return None

    try:
        # 2. Validación geoespacial y temporal contra la última posición aceptada de la unidad
        #la leemos de la bd y no de memoria: asi un reinicio del servicio no pierde el ultimo ts (docs/topics.md)
        fila = bd.leer_unidad(unidad)
        ultimo_estado = None
        if fila is not None:
            ultimo_estado = {"lat": fila["lat"], "lon": fila["lon"], "ts": fila["ts_posicion"]}

        es_valido, codigo_error, detalle = validador.validar_posicion(
            datos, ultimo_estado=ultimo_estado, hora_servicio=hora_servicio
        )
        if not es_valido:
            _descartar(cliente, tipo, unidad, codigo_error, detalle)
            return None

        logger.info(
            "Telemetría aceptada de %s (%s): lat=%.4f, lon=%.4f, rumbo=%s",
            unidad,
            tipo,
            datos["lat"],
            datos["lon"],
            datos.get("rumbo"),
        )

        # 3. Guardamos en la bd la posicion (unidad, tipo, lat, lon, rumbo, ts_posicion)
            #si falla no se publica: salta al except de sqlite3
        with bd.transaccion():
            bd.guardar_posicion(tipo, unidad, datos["lat"], datos["lon"], datos.get("rumbo"), datos["ts"])

        # 4. Publicación oficial en flota/{tipo}/{unidad}/estado (QoS 1, retained=True)
            #conexion y disponibilidad salen de la bd, no de la posicion
        pub_salidas.publicar_flota(cliente, bd, tipo, unidad)

        return datos
    except sqlite3.Error:
        logger.exception("No se pudo guardar la posición de %s, no se publica", unidad)
        return None
