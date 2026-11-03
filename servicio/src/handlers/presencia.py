"""Handler de reportes de presencia (reportes/{tipo}/{unidad}/presencia).

Se expandirá en Issue #10 con la gestión de caídas de móviles,
desasignación de emergencias y emisión de alertas.
"""

import logging
from comun import payloads

logger = logging.getLogger("servicio.presencia")


def procesar(cliente, tipo, unidad, raw_payload):
    """Procesa el estado de conexión (online/offline vía LWT) de una unidad."""
    try:
        datos = payloads.validar(payloads.PRESENCIA, raw_payload)
        logger.info(
            "Presencia recibida de %s (%s): estado=%s",
            unidad,
            tipo,
            datos["estado"],
        )
        return datos
    except payloads.PayloadInvalido as e:
        logger.warning("Presencia inválida de %s: %s", unidad, e)
        return None
