"""Handler de reportes de posición (reportes/{tipo}/{unidad}/posicion).

Se expandirá en Issue #9 con la integración de validador geoespacial,
descarte de saltos imposibles y publicación oficial en flota/{tipo}/{unidad}/estado.
"""

import logging
from comun import payloads
import sqlite3
from . import pub_salidas
logger = logging.getLogger("servicio.posicion")


def procesar(cliente, bd, tipo, unidad, raw_payload):
    """Procesa un reporte de telemetría emitido por una unidad."""
    try:
        datos = payloads.validar(payloads.POSICION, raw_payload)
        logger.info(
            "Telemetría recibida de %s (%s): lat=%.4f, lon=%.4f, rumbo=%s",
            unidad,
            tipo,
            datos["lat"],
            datos["lon"],
            datos.get("rumbo"),
        )

        # #9: leer la posicion anterior con bd.leer_unidad y descartar ts viejos o saltos imposibles

        #primero guardamos en la bd la posicion (unidad, tipo, lat, lon, rumbo, ts_posicion)
            #si falla no se publica: salta al except de sqlite3
        with bd.transaccion():
            bd.guardar_posicion(tipo, unidad, datos["lat"], datos["lon"], datos.get("rumbo"), datos["ts"])

        #publicacion oficial en flota/{tipo}/{unidad}/estado (QoS 1, retained=True)
        pub_salidas.publicar_flota(cliente, bd, tipo, unidad)

        return datos
    except payloads.PayloadInvalido as e:
        logger.warning("Posición inválida descartada de %s: %s", unidad, e)
        return None
    except sqlite3.Error:
        logger.exception("No se pudo guardar la posición de %s, no se publica", unidad)
        return None