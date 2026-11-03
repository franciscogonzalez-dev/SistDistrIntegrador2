"""Handler de reportes de posición (reportes/{tipo}/{unidad}/posicion).

Se expandirá en Issue #9 con la integración de validador geoespacial,
descarte de saltos imposibles y publicación oficial en flota/{tipo}/{unidad}/estado.
"""

import logging
from comun import payloads, topics

logger = logging.getLogger("servicio.posicion")


def procesar(cliente, tipo, unidad, raw_payload):
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

        # Publicación oficial en flota/{tipo}/{unidad}/estado (QoS 1, retained=True)
        topic_flota = topics.flota_estado(tipo, unidad)
        estado_oficial = payloads.flota_estado(
            conexion="online",
            disponibilidad="libre",
            lat=datos["lat"],
            lon=datos["lon"],
            rumbo=datos.get("rumbo"),
            ts=datos["ts"],
        )
        if cliente:
            cliente.publish(estado_oficial, topic=topic_flota, qos=1, retain=True)
            logger.info("-> Publicado estado oficial en %s", topic_flota)

        return datos
    except payloads.PayloadInvalido as e:
        logger.warning("Posición inválida descartada de %s: %s", unidad, e)
        return None
