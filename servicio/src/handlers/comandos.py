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
        # Verificación opcional de token JWT de autorización
        token_jwt = datos.get("jwt")
        if token_jwt:
            try:
                from comun import jwt_auth
                claims = jwt_auth.verificar_token(token_jwt)
                logger.info(
                    "JWT verificado para operador '%s' (rol: %s, jti: %s)",
                    claims.get("sub"),
                    claims.get("rol"),
                    claims.get("jti"),
                )
            except Exception as e:
                logger.warning(
                    "Fallo de verificación JWT en comando de %s: %s",
                    despachador,
                    e,
                )
        return datos
    except payloads.PayloadInvalido as e:
        logger.warning("Comando inválido de %s: %s", despachador, e)
        return None
