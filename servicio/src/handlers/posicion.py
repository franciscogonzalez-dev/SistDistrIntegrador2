"""Handler de reportes de posición (reportes/{tipo}/{unidad}/posicion).

Implementa el flujo de la Issue #9:
1. Extrae identidad exclusivamente del tópico (tipo, unidad).
2. Valida estructura con comun.payloads. Si es inválido -> emite alerta en alertas/{tipo}.
3. Valida física y tiempo con servicio.src.validador. Si es inválido -> emite alerta en alertas/{tipo}
   y preserva la inmutabilidad de la última posición válida.
4. Si es válido -> actualiza la última posición y publica en flota/{tipo}/{unidad}/estado (QoS 1, retain=True).
"""

import logging
<<<<<<< Updated upstream
from comun import payloads
import sqlite3
from . import pub_salidas
=======
import time
from comun import payloads, topics
from servicio.src import validador

>>>>>>> Stashed changes
logger = logging.getLogger("servicio.posicion")

# Registro en memoria de la última posición válida por unidad (inmutabilidad y cálculo de saltos)
_ultimas_posiciones: dict[str, dict] = {}

<<<<<<< Updated upstream
def procesar(cliente, bd, tipo, unidad, raw_payload):
=======

def reiniciar_memoria_posiciones():
    """Limpia el registro de últimas posiciones. Útil para tests unitarios."""
    _ultimas_posiciones.clear()


def procesar(cliente, tipo, unidad, raw_payload, hora_servicio=None):
>>>>>>> Stashed changes
    """Procesa un reporte de telemetría emitido por una unidad."""
    if hora_servicio is None:
        hora_servicio = time.time()

    # 1. Validación estructural del JSON
    try:
        datos = payloads.validar(payloads.POSICION, raw_payload)
    except payloads.PayloadInvalido as e:
        detalle = f"Payload con estructura inválida: {e}"
        logger.warning("Posición inválida descartada de %s (%s): %s", unidad, tipo, detalle)
        if cliente:
            cliente.publish(
                payloads.alerta(motivo="posicion_invalida", mensaje=detalle, unidad=unidad),
                topic=topics.alertas(tipo),
                qos=1,
                retain=False,
            )
        return None

    # 2. Validación geoespacial y temporal contra el último estado válido de la unidad
    ultimo_estado = _ultimas_posiciones.get(unidad)
    es_valido, codigo_error, detalle = validador.validar_posicion(
        datos, ultimo_estado=ultimo_estado, hora_servicio=hora_servicio
    )

    if not es_valido:
        logger.warning(
            "Telemetría rechazada de %s (%s) [%s]: %s",
            unidad,
            tipo,
<<<<<<< Updated upstream
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
=======
            codigo_error,
            detalle,
        )
        # Inmutabilidad: NO modificamos _ultimas_posiciones[unidad]
        if cliente:
            cliente.publish(
                payloads.alerta(motivo="posicion_invalida", mensaje=detalle, unidad=unidad),
                topic=topics.alertas(tipo),
                qos=1,
                retain=False,
            )
            logger.info("-> Emitida alerta en %s para %s", topics.alertas(tipo), unidad)
        return None

    # 3. Posición válida: actualizar última posición en memoria
    _ultimas_posiciones[unidad] = {
        "lat": datos["lat"],
        "lon": datos["lon"],
        "rumbo": datos.get("rumbo"),
        "ts": datos["ts"],
    }

    logger.info(
        "Telemetría aceptada de %s (%s): lat=%.4f, lon=%.4f, rumbo=%s",
        unidad,
        tipo,
        datos["lat"],
        datos["lon"],
        datos.get("rumbo"),
    )

    # 4. Publicación oficial en flota/{tipo}/{unidad}/estado (QoS 1, retained=True)
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
>>>>>>> Stashed changes
