"""Handler de pedidos de emergencia de ciudadanos (pedidos/{clientid}/{tipo}).

Implementación de Issue #12:
1. Valida el payload estructural con comun.payloads.PEDIDO.
2. Valida que la coordenada GPS se encuentre dentro de la zona operativa (validador.en_zona).
3. Idempotencia: deduplica por clientid + id consultando pedidos_comandos_procesados.
   Si ya fue procesado, no crea otra emergencia y no vuelve a publicar.
4. Aplica rate limit por clientid y global para evitar spam o ataques DoS sobre el usuario compartido.
5. Genera un identificador único propio del servicio (eid, ej. E1, E2...).
6. Guarda la emergencia en la BD y publica en emergencias/{tipo}/{eid} (QoS 1, retain=True).
7. El público civil no recibe respuesta directa; solo consulta servicio/estado (duda 4).
"""

import collections
import logging
import sqlite3
import time

from comun import payloads
from servicio.src import validador
from . import pub_salidas

logger = logging.getLogger("servicio.pedidos")

# Parámetros de Rate Limiting (Protección DoS para el usuario compartido 'publico')
MAX_PEDIDOS_CLIENTE = 5       # Máximo de pedidos por clientid en la ventana
MAX_PEDIDOS_GLOBAL = 50       # Máximo de pedidos totales en la ventana
VENTANA_TIEMPO_SEG = 60.0     # Ventana de tiempo en segundos

_historial_por_cliente = collections.defaultdict(list)
_historial_global = []


def reset_limites():
    """Limpia las marcas de tiempo de control de tasa (útil en pruebas)."""
    _historial_por_cliente.clear()
    _historial_global.clear()


def _verificar_limites(clientid: str, ahora: float) -> bool:
    """Verifica si el pedido cumple con los límites de tasa por cliente y global."""
    limite_tiempo = ahora - VENTANA_TIEMPO_SEG

    # Filtrar timestamps viejos en el contador global
    while _historial_global and _historial_global[0] < limite_tiempo:
        _historial_global.pop(0)

    # Filtrar timestamps viejos para el clientid
    hist_cliente = _historial_por_cliente[clientid]
    while hist_cliente and hist_cliente[0] < limite_tiempo:
        hist_cliente.pop(0)

    if len(_historial_global) >= MAX_PEDIDOS_GLOBAL:
        logger.warning(
            "Pedido de %s descartado: límite global de pedidos alcanzado (%d en %.0fs)",
            clientid,
            len(_historial_global),
            VENTANA_TIEMPO_SEG,
        )
        return False

    if len(hist_cliente) >= MAX_PEDIDOS_CLIENTE:
        logger.warning(
            "Pedido de %s descartado: límite de pedidos por cliente alcanzado (%d en %.0fs)",
            clientid,
            len(hist_cliente),
            VENTANA_TIEMPO_SEG,
        )
        return False

    _historial_global.append(ahora)
    hist_cliente.append(ahora)
    return True


def procesar(cliente, bd, clientid, tipo, raw_payload, hora_servicio=None):
    """Procesa una solicitud de auxilio enviada por el público civil."""
    if hora_servicio is None:
        hora_servicio = time.time()

    # 1. Validación estructural del payload
    try:
        datos = payloads.validar(payloads.PEDIDO, raw_payload)
    except payloads.PayloadInvalido as e:
        logger.warning("Pedido descartado de %s: payload inválido (%s)", clientid, e)
        return None

    # 2. Validación de zona geográfica
    if not validador.en_zona(datos["lon"], datos["lat"]):
        logger.warning(
            "Pedido descartado de %s (%s): coordenadas (lat=%.4f, lon=%.4f) fuera de zona operativa",
            clientid,
            tipo,
            datos["lat"],
            datos["lon"],
        )
        return None

    # 3. Idempotencia: un pedido (clientid + id) ya procesado no se aplica dos veces
    try:
        procesado = bd.leer_id_procesado("pedido", clientid, datos["id"])
        if procesado is not None:
            logger.info(
                "Pedido duplicado de %s con id=%s: ya corresponde a la emergencia %s (no se crea otra)",
                clientid,
                datos["id"],
                procesado["resultado"],
            )
            return procesado["resultado"]
    except sqlite3.Error:
        logger.exception("Error al verificar idempotencia del pedido de %s (id=%s)", clientid, datos["id"])
        return None

    # 4. Control de límites (Rate Limiting)
    if not _verificar_limites(clientid, hora_servicio):
        return None

    # 5. Generación de identificador único propio del servicio (eid)
    try:
        eid = bd.generar_siguiente_eid()
    except sqlite3.Error:
        logger.exception("No se pudo generar un nuevo eid para el pedido de %s", clientid)
        return None

    logger.info(
        "Pedido aceptado de %s para %s (lat=%.4f, lon=%.4f, id=%s) -> Asignando %s",
        clientid,
        tipo,
        datos["lat"],
        datos["lon"],
        datos["id"],
        eid,
    )

    # 6. Persistencia atómica de la emergencia y el registro de idempotencia
    try:
        with bd.transaccion():
            bd.crear_emergencia(eid, tipo, datos["lat"], datos["lon"], datos["ts"])
            bd.guardar_procesado("pedido", clientid, datos["id"], eid, hora_servicio)
    except sqlite3.Error:
        logger.exception("Error al guardar la emergencia %s en la base de datos", eid)
        return None

    # 7. Publicación oficial en emergencias/{tipo}/{eid} (QoS 1, retain=True)
    pub_salidas.publicar_emergencia(cliente, bd, tipo, eid)

    return eid
