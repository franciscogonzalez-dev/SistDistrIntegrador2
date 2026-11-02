"""Handler de pedidos de emergencia de ciudadanos (pedidos/{clientid}/{tipo}).

Se expandirá en Issue #12 con la generación de incidentes (eid),
deduplicación por clientid + id y publicación en emergencias/.
"""

import logging
from comun import payloads

logger = logging.getLogger("servicio.pedidos")


def procesar(cliente, clientid, tipo, raw_payload):
    """Procesa una solicitud de auxilio enviada por el público civil."""
    try:
        datos = payloads.validar(payloads.PEDIDO, raw_payload)
        logger.info(
            "Pedido recibido de %s para %s: lat=%.4f, lon=%.4f (id=%s)",
            clientid,
            tipo,
            datos["lat"],
            datos["lon"],
            datos["id"],
        )
        return datos
    except payloads.PayloadInvalido as e:
        logger.warning("Pedido inválido de %s: %s", clientid, e)
        return None
