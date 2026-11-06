"""Handler de comandos del despachador (comandos/{despachador}/{tipo}/{unidad}).

Se expandirá en Issue #12 con la asignación/liberación de móviles,
respuestas idempotentes y actualización de incidentes.
"""

import logging
from comun import payloads

logger = logging.getLogger("servicio.comandos")


def procesar(cliente, despachador, tipo, unidad, raw_payload):
    """Procesa una orden emitida por el despachador."""
    try:
        datos = payloads.validar(payloads.COMANDO, raw_payload)
        logger.info(
            "Comando recibido de %s para %s (%s): accion=%s, eid=%s",
            despachador,
            unidad,
            tipo,
            datos["accion"],
            datos.get("eid"),
        )
        return datos
    except payloads.PayloadInvalido as e:
        logger.warning("Comando inválido de %s: %s", despachador, e)
        return None
