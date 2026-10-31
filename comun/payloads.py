"""Payloads de cada mensaje (contrato en docs/topics.md, seccion Payloads).

Reglas:
- "v" en todos, "ts" en segundos Unix, ids uuid4 en texto
- si falta un campo obligatorio o tiene tipo incorrecto, el mensaje se descarta (PayloadInvalido)
- los campos desconocidos se ignoran (validar devuelve solo los campos del contrato)
- la identidad de quien publica sale del topico, nunca del payload

Del lado de quien publica se usan los builders, que arman el dict listo para publicar:

    datos = payloads.posicion(lat=-45.86, lon=-67.49, rumbo=180)

Del lado de quien recibe se usa validar con el spec del mensaje:

    datos = payloads.validar(payloads.POSICION, msg.payload)
"""

import json
import time
import uuid

VERSION = 1
VERSIONES_SOPORTADAS = (1,)

#valores permitidos de los campos tipo enum
ESTADOS_CONEXION = ("online", "offline")
DISPONIBILIDADES = ("libre", "ocupada")
ACCIONES = ("asignar", "liberar")
ESTADOS_EMERGENCIA = ("sin_asignar", "asignada")
MOTIVOS_ALERTA = ("unidad_desconectada", "posicion_invalida")


class PayloadInvalido(ValueError):
    pass


def ahora():
    return time.time()


def nuevo_id():
    return str(uuid.uuid4())


# tipos de campo

def _numero(valor):
    #bool es subclase de int en python, no queremos aceptar true como latitud
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _texto(valor):
    return isinstance(valor, str) and valor != ""


def _bool(valor):
    return isinstance(valor, bool)


def _uno_de(opciones):
    return lambda valor: valor in opciones


# SPECS: campo -> (chequeo, obligatorio)

POSICION = {
    "v": (_numero, True),
    "ts": (_numero, True),
    "lat": (_numero, True),
    "lon": (_numero, True),
    "rumbo": (_numero, False),
}

#ts opcional: el payload del Last Will se define al conectar, tendria la hora de conexion y no la de la caida
PRESENCIA = {
    "v": (_numero, True),
    "estado": (_uno_de(ESTADOS_CONEXION), True),
    "ts": (_numero, False),
}

COMANDO = {
    "v": (_numero, True),
    "id": (_texto, True),
    "ts": (_numero, True),
    "accion": (_uno_de(ACCIONES), True),
    "eid": (_texto, False), #obligatorio si accion es asignar, se chequea en validar
}

PEDIDO = {
    "v": (_numero, True),
    "id": (_texto, True),
    "ts": (_numero, True),
    "lat": (_numero, True),
    "lon": (_numero, True),
}

FLOTA_ESTADO = {
    "v": (_numero, True),
    "ts": (_numero, True),
    "conexion": (_uno_de(ESTADOS_CONEXION), True),
    "disponibilidad": (_uno_de(DISPONIBILIDADES), True),
    "lat": (_numero, False), #sin posicion valida todavia, la unidad no aparece en el mapa
    "lon": (_numero, False),
    "rumbo": (_numero, False),
    "eid": (_texto, False),
}

EMERGENCIA = {
    "v": (_numero, True),
    "eid": (_texto, True),
    "ts": (_numero, True),
    "lat": (_numero, True),
    "lon": (_numero, True),
    "estado": (_uno_de(ESTADOS_EMERGENCIA), True),
    "unidad": (_texto, False),
}

ALERTA = {
    "v": (_numero, True),
    "ts": (_numero, True),
    "motivo": (_uno_de(MOTIVOS_ALERTA), True),
    "mensaje": (_texto, True),
    "unidad": (_texto, False),
    "eid": (_texto, False),
}

RESPUESTA = {
    "v": (_numero, True),
    "id": (_texto, True),
    "ts": (_numero, True),
    "ok": (_bool, True),
    "motivo": (_texto, False),
}

SERVICIO_ESTADO = PRESENCIA


def validar(spec, payload):
    """Valida un payload recibido (bytes, str o dict) contra su spec.

    Devuelve un dict solo con los campos del contrato. Si el payload no es JSON,
    falta un obligatorio o un campo tiene tipo incorrecto, lanza PayloadInvalido.
    """
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as e:
            raise PayloadInvalido(f"payload no es JSON: {e}") from e

    if not isinstance(payload, dict):
        raise PayloadInvalido("el payload tiene que ser un objeto JSON")

    datos = {}
    for campo, (chequeo, obligatorio) in spec.items():
        if campo not in payload or payload[campo] is None:
            if obligatorio:
                raise PayloadInvalido(f"falta el campo obligatorio {campo!r}")
            continue
        if not chequeo(payload[campo]):
            raise PayloadInvalido(f"campo {campo!r} con valor invalido: {payload[campo]!r}")
        datos[campo] = payload[campo]

    if datos["v"] not in VERSIONES_SOPORTADAS:
        raise PayloadInvalido(f"version no soportada: {datos['v']!r}")

    if spec is COMANDO and datos["accion"] == "asignar" and "eid" not in datos:
        raise PayloadInvalido("el comando asignar necesita eid")

    return datos


def es_cierre(payload):
    """En emergencias/{tipo}/{eid}, un payload vacio (retained) significa emergencia cerrada."""
    return payload in (b"", "", None)


# BUILDERS: arman el dict listo para publicar (publisher.publish hace el json.dumps)

def _armar(spec, **campos):
    datos = {"v": VERSION, **{k: v for k, v in campos.items() if v is not None}}
    return validar(spec, datos) #si el que publica arma mal el mensaje, falla aca y no en el que recibe


def posicion(lat, lon, rumbo=None, ts=None):
    return _armar(POSICION, ts=ts or ahora(), lat=lat, lon=lon, rumbo=rumbo)


def presencia(estado, ts=None):
    #para el Last Will no pasar ts
    return _armar(PRESENCIA, estado=estado, ts=ts)


def comando(accion, eid=None, id=None, ts=None):
    return _armar(COMANDO, id=id or nuevo_id(), ts=ts or ahora(), accion=accion, eid=eid)


def pedido(lat, lon, id=None, ts=None):
    return _armar(PEDIDO, id=id or nuevo_id(), ts=ts or ahora(), lat=lat, lon=lon)


def flota_estado(conexion, disponibilidad, lat=None, lon=None, rumbo=None, eid=None, ts=None):
    return _armar(FLOTA_ESTADO, ts=ts or ahora(), conexion=conexion, disponibilidad=disponibilidad,
                  lat=lat, lon=lon, rumbo=rumbo, eid=eid)


def emergencia(eid, lat, lon, estado, unidad=None, ts=None):
    return _armar(EMERGENCIA, eid=eid, ts=ts or ahora(), lat=lat, lon=lon, estado=estado, unidad=unidad)


def alerta(motivo, mensaje, unidad=None, eid=None, ts=None):
    return _armar(ALERTA, ts=ts or ahora(), motivo=motivo, mensaje=mensaje, unidad=unidad, eid=eid)


def respuesta(id, ok, motivo=None, ts=None):
    #id es el del comando al que responde
    return _armar(RESPUESTA, id=id, ts=ts or ahora(), ok=ok, motivo=motivo)


def servicio_estado(estado, ts=None):
    return _armar(SERVICIO_ESTADO, estado=estado, ts=ts)
