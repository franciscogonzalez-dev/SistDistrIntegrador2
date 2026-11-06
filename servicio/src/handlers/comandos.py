"""Handler de comandos del despachador (comandos/{despachador}/{tipo}/{unidad}).

Implementación de Issue #12:
1. Extrae identidad y contexto exclusivamente de los tópicos (despachador, tipo, unidad).
2. Valida estructura con comun.payloads.COMANDO.
3. Si incluye token JWT, valida firma y claims con comun.jwt_auth.
4. Idempotencia: deduplica por despachador + id de comando. Si ya se procesó,
   no re-ejecuta la acción y reenvía la respuesta previa a respuestas/{despachador}.
5. Acción 'asignar':
   - Solo permite asignar unidades libres y online.
   - Si no cumple o la emergencia no existe/ya está asignada, rechaza la orden y avisa
     al despachador en respuestas/{despachador} (payloads.respuesta con ok=False y motivo).
   - Si es válido, pasa la unidad a ocupada, la emergencia a asignada, y notifica por
     flota/, emergencias/ y respuestas/{despachador}.
6. Acción 'liberar':
   - Marca la unidad libre en flota/ y cierra la emergencia (retained vacío en emergencias/{tipo}/{eid}).
   - Notifica confirmación en respuestas/{despachador}.
"""

import json
import logging
import sqlite3
import time

from comun import payloads, topics
from . import pub_salidas

logger = logging.getLogger("servicio.comandos")


def _rechazar(cliente, bd, despachador, cmd_id, motivo):
    """Emite una respuesta de rechazo oficial al despachador y la registra para idempotencia."""
    resp = pub_salidas.publicar_respuesta(cliente, despachador, cmd_id, ok=False, motivo=motivo)
    try:
        with bd.transaccion():
            bd.guardar_procesado("comando", despachador, cmd_id, json.dumps(resp), time.time())
    except sqlite3.Error:
        logger.exception("Error al registrar rechazo de comando %s en la base de datos", cmd_id)
    logger.warning("Comando %s de %s RECHAZADO: %s", cmd_id, despachador, motivo)
    return resp


def procesar(cliente, bd, despachador, tipo, unidad, raw_payload):
    """Procesa una orden emitida por el despachador."""
    # 1. Validación estructural del payload
    try:
        datos = payloads.validar(payloads.COMANDO, raw_payload)
    except payloads.PayloadInvalido as e:
        logger.warning("Comando inválido de %s para %s: %s", despachador, unidad, e)
        return None

    # 2. Verificación opcional de token JWT de autorización
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
            logger.warning("Fallo de verificación JWT en comando de %s: %s", despachador, e)

    cmd_id = datos["id"]
    accion = datos["accion"]

    logger.info(
        "Comando recibido de %s para %s (%s): accion=%s, eid=%s, id=%s",
        despachador,
        unidad,
        tipo,
        accion,
        datos.get("eid"),
        cmd_id,
    )

    # 3. Idempotencia: un comando con id ya procesado no se aplica dos veces
    try:
        procesado = bd.leer_id_procesado("comando", despachador, cmd_id)
        if procesado is not None:
            logger.info("Comando duplicado de %s con id=%s: reenviando respuesta previa", despachador, cmd_id)
            resp = json.loads(procesado["resultado"])
            topic_resp = topics.respuestas(despachador)
            cliente.publish(resp, topic=topic_resp, qos=1, retain=False)
            return resp
    except sqlite3.Error:
        logger.exception("Error al verificar idempotencia del comando %s de %s", cmd_id, despachador)
        return None

    # 4. Procesamiento según acción
    try:
        fila_unidad = bd.leer_unidad(unidad)

        if accion == "asignar":
            eid = datos.get("eid")
            if not eid:
                return _rechazar(cliente, bd, despachador, cmd_id, "Comando asignar requiere eid")

            if fila_unidad is None:
                return _rechazar(cliente, bd, despachador, cmd_id, f"Unidad {unidad} no registrada")

            if fila_unidad["tipo"] != tipo:
                return _rechazar(
                    cliente,
                    bd,
                    despachador,
                    cmd_id,
                    f"Tipo de unidad incompatible: {unidad} es {fila_unidad['tipo']}, no {tipo}",
                )

            # Requisito: Solo se puede asignar una unidad libre y conectada
            if fila_unidad["conexion"] != "online":
                return _rechazar(cliente, bd, despachador, cmd_id, f"Unidad {unidad} desconectada (offline)")

            if fila_unidad["eid"] is not None:
                return _rechazar(cliente, bd, despachador, cmd_id, f"Unidad {unidad} ocupada con {fila_unidad['eid']}")

            # Validar existencia y disponibilidad de la emergencia
            fila_emerg = bd.leer_emergencia(eid)
            if fila_emerg is None:
                return _rechazar(cliente, bd, despachador, cmd_id, f"Emergencia {eid} no encontrada")

            if fila_emerg["tipo"] != tipo:
                return _rechazar(
                    cliente,
                    bd,
                    despachador,
                    cmd_id,
                    f"Tipo de emergencia incompatible: {eid} es {fila_emerg['tipo']}, no {tipo}",
                )

            if fila_emerg["unidad"] is not None:
                return _rechazar(
                    cliente,
                    bd,
                    despachador,
                    cmd_id,
                    f"Emergencia {eid} ya asignada a {fila_emerg['unidad']}",
                )

            # Asignación aceptada: persistir cambios de forma atómica
            resp = payloads.respuesta(id=cmd_id, ok=True)
            with bd.transaccion():
                bd.asignar_emergencia(eid, unidad)
                bd.guardar_procesado("comando", despachador, cmd_id, json.dumps(resp), time.time())

            # Publicaciones de estado oficial
            pub_salidas.publicar_flota(cliente, bd, tipo, unidad)
            pub_salidas.publicar_emergencia(cliente, bd, tipo, eid)
            pub_salidas.publicar_respuesta(cliente, despachador, cmd_id, ok=True)

            logger.info("Móvil %s asignado con éxito a emergencia %s por %s", unidad, eid, despachador)
            return resp

        elif accion == "liberar":
            if fila_unidad is None:
                return _rechazar(cliente, bd, despachador, cmd_id, f"Unidad {unidad} no registrada")

            eid_asignado = fila_unidad["eid"]
            if eid_asignado is None:
                return _rechazar(cliente, bd, despachador, cmd_id, f"Unidad {unidad} ya está libre")

            # Si el comando incluía eid opcional, verificar coincidencia
            if datos.get("eid") and datos["eid"] != eid_asignado:
                return _rechazar(
                    cliente,
                    bd,
                    despachador,
                    cmd_id,
                    f"Unidad {unidad} asignada a {eid_asignado}, no a {datos['eid']}",
                )

            # Liberación aceptada: cerrar emergencia y liberar unidad
            resp = payloads.respuesta(id=cmd_id, ok=True)
            with bd.transaccion():
                bd.cerrar_emergencia(eid_asignado)
                bd.guardar_procesado("comando", despachador, cmd_id, json.dumps(resp), time.time())

            # Publicaciones de estado oficial
            pub_salidas.publicar_flota(cliente, bd, tipo, unidad)
            pub_salidas.cerrar_emergencia(cliente, tipo, eid_asignado)
            pub_salidas.publicar_respuesta(cliente, despachador, cmd_id, ok=True)

            logger.info(
                "Móvil %s liberado con éxito por %s; emergencia %s cerrada",
                unidad,
                despachador,
                eid_asignado,
            )
            return resp

        else:
            return _rechazar(cliente, bd, despachador, cmd_id, f"Acción desconocida: {accion}")

    except sqlite3.Error:
        logger.exception("Error de base de datos al procesar comando %s de %s", cmd_id, despachador)
        return None
