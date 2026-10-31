"""Arbol de topicos del sistema (contrato en docs/topics.md).

Nadie arma ni parsea topicos a mano: se usan estas funciones.

    from comun import topics
    client.publish(topics.posicion("ambulancia", "amb-01"), ...)
    client.subscribe(topics.SUB_POSICIONES)
    tipo, unidad, evento = topics.parse_reporte(msg.topic)
"""

TIPOS = ("policia", "ambulancia", "bomberos")

SERVICIO_ESTADO = "servicio/estado"

#patrones de suscripcion
#el servicio usa + (no #) para recibir solo los topicos que existen en el contrato
SUB_POSICIONES = "reportes/+/+/posicion"
SUB_PRESENCIAS = "reportes/+/+/presencia"
SUB_COMANDOS = "comandos/+/+/+"
SUB_PEDIDOS = "pedidos/+/+"
#el despachador ve todas las fuerzas, por eso #
SUB_FLOTA = "flota/#"
SUB_EMERGENCIAS = "emergencias/#"
SUB_ALERTAS = "alertas/#"


def _nivel(valor, nombre):
    #un nivel no puede estar vacio ni tener separadores o wildcards, si no el topico cambia de forma
    if not isinstance(valor, str) or not valor or any(c in valor for c in "/+#"):
        raise ValueError(f"{nombre} invalido para un topico: {valor!r}")
    return valor


def _tipo(tipo):
    if tipo not in TIPOS:
        raise ValueError(f"tipo desconocido: {tipo!r}, tiene que ser uno de {TIPOS}")
    return tipo


# ENTRADA (sin validar) -> lee solo el servicio

def posicion(tipo, unidad):
    return f"reportes/{_tipo(tipo)}/{_nivel(unidad, 'unidad')}/posicion"


def presencia(tipo, unidad):
    return f"reportes/{_tipo(tipo)}/{_nivel(unidad, 'unidad')}/presencia"


def comando(despachador, tipo, unidad):
    return f"comandos/{_nivel(despachador, 'despachador')}/{_tipo(tipo)}/{_nivel(unidad, 'unidad')}"


def pedido(clientid, tipo):
    return f"pedidos/{_nivel(clientid, 'clientid')}/{_tipo(tipo)}"


# SALIDA (oficial) -> publica solo el servicio

def flota_estado(tipo, unidad):
    return f"flota/{_tipo(tipo)}/{_nivel(unidad, 'unidad')}/estado"


def emergencia(tipo, eid):
    return f"emergencias/{_tipo(tipo)}/{_nivel(eid, 'eid')}"


def alertas(tipo):
    return f"alertas/{_tipo(tipo)}"


def respuestas(despachador):
    return f"respuestas/{_nivel(despachador, 'despachador')}"


# PARSERS: la identidad de quien publica sale del topico, nunca del payload

def _partes(topic, prefijo, cantidad):
    partes = topic.split("/")
    if len(partes) != cantidad or partes[0] != prefijo:
        raise ValueError(f"topico inesperado: {topic!r}")
    return partes


def parse_reporte(topic):
    """reportes/{tipo}/{unidad}/{evento} -> (tipo, unidad, evento)"""
    _, tipo, unidad, evento = _partes(topic, "reportes", 4)
    if evento not in ("posicion", "presencia"):
        raise ValueError(f"evento desconocido en {topic!r}")
    return _tipo(tipo), _nivel(unidad, "unidad"), evento


def parse_comando(topic):
    """comandos/{despachador}/{tipo}/{unidad} -> (despachador, tipo, unidad)"""
    _, despachador, tipo, unidad = _partes(topic, "comandos", 4)
    return _nivel(despachador, "despachador"), _tipo(tipo), _nivel(unidad, "unidad")


def parse_pedido(topic):
    """pedidos/{clientid}/{tipo} -> (clientid, tipo)"""
    _, clientid, tipo = _partes(topic, "pedidos", 3)
    return _nivel(clientid, "clientid"), _tipo(tipo)


def parse_flota_estado(topic):
    """flota/{tipo}/{unidad}/estado -> (tipo, unidad)"""
    _, tipo, unidad, fin = _partes(topic, "flota", 4)
    if fin != "estado":
        raise ValueError(f"topico inesperado: {topic!r}")
    return _tipo(tipo), _nivel(unidad, "unidad")


def parse_emergencia(topic):
    """emergencias/{tipo}/{eid} -> (tipo, eid)"""
    _, tipo, eid = _partes(topic, "emergencias", 3)
    return _tipo(tipo), _nivel(eid, "eid")


def parse_alertas(topic):
    """alertas/{tipo} -> tipo"""
    _, tipo = _partes(topic, "alertas", 2)
    return _tipo(tipo)
