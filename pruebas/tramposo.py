#!/usr/bin/env python3
"""
pruebas/tramposo.py
===================
Suite interactiva y automatizada de Cliente Tramposo para la Demostración.
Cubre integralmente los requisitos de Seguridad (Req. 5) y Fallas/Seguridad de la defensa:
  1. Conexión y Atadura de Client ID (cinfo en EMQX)
  2. Espionaje y Suscripciones Prohibidas (ACL en EMQX)
  3. Suplantación de Identidad y Tópicos Prohibidos (ACL en EMQX)
  4. Mentiras de Estado y Mensajes Hostiles (Validador en Servicio Central)

Uso rápido:
    python pruebas/tramposo.py                     # Menú interactivo
    python pruebas/tramposo.py --todos             # Ejecuta todos los casos
    python pruebas/tramposo.py --cat 1             # Ejecuta categoría 1
    python pruebas/tramposo.py --caso espiar-flota # Ejecuta caso puntual
    python pruebas/tramposo.py --listar            # Lista los 20 casos disponibles
"""

import argparse
import json
import math
import os
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path

# Ajustar sys.path para importar módulos comunes del proyecto
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: falta instalar paho-mqtt. Activá el entorno virtual:")
    print("    source venv/bin/activate  (Linux) / .venv\\Scripts\\activate (Windows)")
    sys.exit(1)

from clientes.credenciales import clave
from comun import payloads, topics

# Configuración de conexión por defecto
CA_CERT_DEFAULT = str(ROOT_DIR / "broker" / "certs" / "ca.crt")
HOST_DEFAULT = os.getenv("MQTT_HOST", "localhost")
PORT_DEFAULT = int(os.getenv("MQTT_PORT", "8883"))

# Colores ANSI para terminal
USE_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _c(texto, color_code):
    return f"\033[{color_code}m{texto}\033[0m" if USE_COLOR else str(texto)


def verde(t): return _c(t, "1;32")
def rojo(t): return _c(t, "1;31")
def amarillo(t): return _c(t, "1;33")
def azul(t): return _c(t, "1;34")
def cyan(t): return _c(t, "1;36")
def negrita(t): return _c(t, "1")
def tenue(t): return _c(t, "2")


# =============================================================================
# HELPERS DE CONEXIÓN MQTT v5
# =============================================================================

def verificar_broker(host=HOST_DEFAULT, port=PORT_DEFAULT, timeout=2.0):
    """Verifica si el puerto SSL del broker está accesible a nivel socket."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except Exception as e:
        return False, str(e)


def crear_cliente_raw(client_id, host=HOST_DEFAULT, port=PORT_DEFAULT, ca_cert=CA_CERT_DEFAULT):
    """Crea una instancia paho-mqtt v5 con TLS configurado."""
    c = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTProtocolVersion.MQTTv5,
    )
    if Path(ca_cert).exists():
        c.tls_set(ca_certs=ca_cert, cert_reqs=ssl.CERT_REQUIRED)
        c.tls_insecure_set(True)
    return c


def intentar_conexion(client_id, username=None, password=None, host=HOST_DEFAULT, port=PORT_DEFAULT, ca_cert=CA_CERT_DEFAULT, timeout=3.0):
    """
    Intenta conectar al broker.
    Retorna: (conectado: bool, reason_code: int/str, razon_humana: str)
    """
    resultado = {"conectado": False, "codigo": None, "desconectado": False}
    ev = threading.Event()

    def on_connect(c, userdata, flags, rc, properties=None):
        cod = getattr(rc, "value", rc)
        resultado["codigo"] = cod
        if cod == 0:
            resultado["conectado"] = True
        ev.set()

    def on_disconnect(c, userdata, flags, rc, properties=None):
        resultado["desconectado"] = True
        ev.set()

    c = crear_cliente_raw(client_id, host=host, port=port, ca_cert=ca_cert)
    if username is not None:
        c.username_pw_set(username, password)
    c.on_connect = on_connect
    c.on_disconnect = on_disconnect

    try:
        c.connect(host, port, keepalive=5)
        c.loop_start()
        ev.wait(timeout)
        c.loop_stop()
        c.disconnect()
    except Exception as e:
        return False, "ERROR_SOCKET", f"Fallo al conectar socket: {e}"

    cod = resultado["codigo"]
    if cod is None:
        # Se desconectó o dio timeout sin CONNACK explícito
        if resultado["desconectado"]:
            return False, "DESCONECTADO_POR_BROKER", "El broker cerró la conexión en el handshake (Rechazo TLS/cinfo)"
        return False, "TIMEOUT", "Sin respuesta CONNACK del broker"

    # Mapeo de códigos MQTT v5 frecuentes
    mapa_cod = {
        0: "0 (Success / Conexión exitosa)",
        133: "133 (Client Identifier not valid - Rechazado por regla cinfo)",
        134: "134 (Bad user name or password - Clave o usuario incorrecto)",
        135: "135 (Not authorized - Conexión denegada por política del broker)",
    }
    nombre_cod = mapa_cod.get(cod, f"{cod} (Rechazo MQTT v5)")
    return resultado["conectado"], cod, nombre_cod


def cliente_conectado(client_id, username, password, host=HOST_DEFAULT, port=PORT_DEFAULT, ca_cert=CA_CERT_DEFAULT):
    """Conecta un cliente legítimo y lo deja corriendo en loop para pruebas de suscripción/publicación."""
    ev = threading.Event()
    cod_conexion = {"codigo": None}

    def on_connect(c, userdata, flags, rc, properties=None):
        cod_conexion["codigo"] = getattr(rc, "value", rc)
        if cod_conexion["codigo"] == 0:
            ev.set()

    c = crear_cliente_raw(client_id, host=host, port=port, ca_cert=ca_cert)
    c.username_pw_set(username, password)
    c.on_connect = on_connect

    try:
        c.connect(host, port, keepalive=15)
        c.loop_start()
        if not ev.wait(3.0):
            c.loop_stop()
            return None, f"No se pudo conectar {username} (código: {cod_conexion['codigo']})"
        return c, None
    except Exception as e:
        return None, str(e)


def intentar_suscripcion(cliente, topic, qos=1, timeout=2.0):
    """
    Intenta suscribirse a un tópico.
    Retorna: (autorizado: bool, codigo: int/str, detalle: str)
    """
    resp = {"codigo": None}
    ev = threading.Event()

    def on_subscribe(c, userdata, mid, reason_codes, properties=None):
        cod = getattr(reason_codes[0], "value", reason_codes[0])
        resp["codigo"] = cod
        ev.set()

    cliente.on_subscribe = on_subscribe
    cliente.subscribe(topic, qos=qos)
    ev.wait(timeout)

    cod = resp["codigo"]
    if cod is None:
        return False, "TIMEOUT", "Sin respuesta SUBACK del broker"
    if cod < 128:
        return True, cod, f"Granted QoS {cod} (Autorizado)"
    if cod == 135:
        return False, cod, "135 (Not authorized - Rechazado por ACL de EMQX)"
    return False, cod, f"{cod} (Denegado MQTT v5)"


def intentar_publicacion(cliente, topic, payload="{}", qos=1, retain=False, timeout=2.5):
    """
    Intenta publicar en un tópico con QoS 1.
    Retorna: (autorizado: bool, codigo: int/str, detalle: str)
    """
    resp = {"codigo": None, "confirmado": False}
    ev = threading.Event()

    def on_publish(c, userdata, mid, reason_code=None, properties=None):
        cod = getattr(reason_code, "value", reason_code) if reason_code is not None else 0
        resp["codigo"] = cod
        resp["confirmado"] = True
        ev.set()

    cliente.on_publish = on_publish
    msg_info = cliente.publish(topic, payload, qos=qos, retain=retain)
    try:
        msg_info.wait_for_publish(timeout=timeout)
        rc_info = getattr(msg_info.rc, "value", msg_info.rc)
        cod = resp["codigo"] if resp["codigo"] is not None else rc_info
        if cod == 0 and resp["confirmado"]:
            return True, 0, "0 (Success - Publicación autorizada por ACL)"
        if cod == 135 or cod >= 128:
            return False, cod, f"{cod} (Not authorized - Denegado por ACL de EMQX)"
        return (cod < 128), cod, f"Código {cod}"
    except Exception as e:
        return False, "EXCEPCION", str(e)


# =============================================================================
# DEFINICIÓN DE CASOS DE PRUEBA
# =============================================================================

CASOS = {}


def registrar_caso(id_caso, nombre, categoria, descripcion, accion_maliciosa, defensa_esperada, verificacion_visual):
    def decorador(func):
        CASOS[id_caso] = {
            "id": id_caso,
            "nombre": nombre,
            "categoria": categoria,
            "descripcion": descripcion,
            "accion": accion_maliciosa,
            "defensa": defensa_esperada,
            "verificacion": verificacion_visual,
            "handler": func,
        }
        return func
    return decorador


# -----------------------------------------------------------------------------
# CATEGORÍA 1: AUTENTICACIÓN Y CLIENT ID ATADO AL USUARIO (cinfo)
# -----------------------------------------------------------------------------

@registrar_caso(
    "robar-servicio",
    "Público intenta robar Client ID del Servicio Central",
    1,
    "Un cliente civil autenticado con 'publico' usa client_id='servicio' para robar su sesión persistente.",
    "CONNECT con usuario 'publico' y client_id='servicio'",
    "Autenticador 'cinfo' rechaza la conexión porque 'publico' exige 'publico-<32 hex>'",
    "Dashboard EMQX -> Log / Connections: Intento rechazado. El servicio real sigue conectado.",
)
def caso_robar_servicio(host, port, ca):
    conectado, cod, det = intentar_conexion("servicio", "publico", clave("publico"), host, port, ca)
    bloqueado = not conectado
    return bloqueado, cod, det


@registrar_caso(
    "robar-despachador",
    "Público intenta robar Client ID del Despachador General",
    1,
    "Un cliente civil autenticado con 'publico' usa client_id='desp-01' para robar su sesión persistente.",
    "CONNECT con usuario 'publico' y client_id='desp-01'",
    "Autenticador 'cinfo' rechaza la conexión (no coincide con el formato de público ni con su username)",
    "Dashboard EMQX -> Conexión rechazada con código 133.",
)
def caso_robar_despachador(host, port, ca):
    conectado, cod, det = intentar_conexion("desp-01", "publico", clave("publico"), host, port, ca)
    bloqueado = not conectado
    return bloqueado, cod, det


@registrar_caso(
    "suplantar-clientid",
    "Unidad móvil intenta conectar con Client ID de otra unidad",
    1,
    "La unidad amb-01 se autentica con sus credenciales pero con client_id='amb-02'.",
    "CONNECT con usuario 'amb-01' y client_id='amb-02'",
    "Autenticador 'cinfo' rechaza porque en unidades se exige client_id == username estrictamente",
    "Dashboard EMQX -> Conexión rechazada.",
)
def caso_suplantar_clientid(host, port, ca):
    conectado, cod, det = intentar_conexion("amb-02", "amb-01", clave("amb-01"), host, port, ca)
    bloqueado = not conectado
    return bloqueado, cod, det


@registrar_caso(
    "conexion-anonima",
    "Intento de conexión anónima (sin credenciales)",
    1,
    "Un cliente intenta conectarse sin proveer usuario ni contraseña.",
    "CONNECT sin username ni password",
    "EMQX rechaza porque allow_anonymous=false y hay autenticadores activos",
    "Dashboard EMQX -> Metrics / Authentication: Fallos de autenticación incrementan.",
)
def caso_conexion_anonima(host, port, ca):
    conectado, cod, det = intentar_conexion("anon-12345", None, None, host, port, ca)
    bloqueado = not conectado
    return bloqueado, cod, det


@registrar_caso(
    "clave-invalida",
    "Intento de conexión con clave incorrecta",
    1,
    "Un cliente intenta conectarse como 'publico' pero con una clave errónea.",
    "CONNECT con usuario 'publico' y password='clave_totalmente_falsa'",
    "Base interna de EMQX rechaza por clave incorrecta (Bad username or password)",
    "Dashboard EMQX -> Conexión rechazada con código 134.",
)
def caso_clave_invalida(host, port, ca):
    cid = f"publico-{uuid.uuid4().hex}"
    conectado, cod, det = intentar_conexion(cid, "publico", "clave_totalmente_falsa", host, port, ca)
    bloqueado = not conectado
    return bloqueado, cod, det


# -----------------------------------------------------------------------------
# CATEGORÍA 2: ESPIONAJE Y SUSCRIPCIONES PROHIBIDAS (ACL Broker)
# -----------------------------------------------------------------------------

@registrar_caso(
    "espiar-flota",
    "Público intenta espiar la flota oficial de emergencias",
    2,
    "Un civil intenta suscribirse a 'flota/#' para ver la posición y estado de ambulancias y patrulleros.",
    "SUBSCRIBE a 'flota/#' con usuario 'publico'",
    "ACL de EMQX deniega la suscripción (público solo puede leer 'servicio/estado')",
    "SUBACK recibido con código 135 (Not authorized). Contador Deny en EMQX Dashboard sube.",
)
def caso_espiar_flota(host, port, ca):
    cid = f"publico-{uuid.uuid4().hex}"
    cli, err = cliente_conectado(cid, "publico", clave("publico"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        autorizado, cod, det = intentar_suscripcion(cli, topics.SUB_FLOTA)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


@registrar_caso(
    "espiar-comandos",
    "Unidad móvil intenta espiar órdenes de despacho ajenas",
    2,
    "La unidad amb-01 intenta suscribirse a 'comandos/#' para enterarse de órdenes del despachador.",
    "SUBSCRIBE a 'comandos/#' con usuario 'amb-01'",
    "ACL de EMQX deniega la suscripción (unidades no tienen permitido suscribirse a ningún tópico)",
    "SUBACK recibido con código 135 (Not authorized).",
)
def caso_espiar_comandos(host, port, ca):
    cli, err = cliente_conectado("amb-01", "amb-01", clave("amb-01"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        autorizado, cod, det = intentar_suscripcion(cli, topics.SUB_COMANDOS)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


@registrar_caso(
    "espiar-reportes",
    "Público intenta leer reportes de telemetría cruda",
    2,
    "Un usuario civil intenta suscribirse a 'reportes/+/+/posicion'.",
    "SUBSCRIBE a 'reportes/+/+/posicion' con usuario 'publico'",
    "ACL deniega la suscripción (solo el Servicio Central puede leer reportes en crudo)",
    "SUBACK recibido con código 135.",
)
def caso_espiar_reportes(host, port, ca):
    cid = f"publico-{uuid.uuid4().hex}"
    cli, err = cliente_conectado(cid, "publico", clave("publico"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        autorizado, cod, det = intentar_suscripcion(cli, topics.SUB_POSICIONES)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


@registrar_caso(
    "espiar-emergencias",
    "Público intenta ver todas las emergencias del sistema",
    2,
    "Un civil intenta suscribirse a 'emergencias/#' para listar incidentes policiales y médicos.",
    "SUBSCRIBE a 'emergencias/#' con usuario 'publico'",
    "ACL deniega la suscripción (solo despachador y servicio leen emergencias)",
    "SUBACK recibido con código 135.",
)
def caso_espiar_emergencias(host, port, ca):
    cid = f"publico-{uuid.uuid4().hex}"
    cli, err = cliente_conectado(cid, "publico", clave("publico"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        autorizado, cod, det = intentar_suscripcion(cli, topics.SUB_EMERGENCIAS)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


# -----------------------------------------------------------------------------
# CATEGORÍA 3: SUPLANTACIÓN DE IDENTIDAD Y TÓPICOS PROHIBIDOS (ACL Broker)
# -----------------------------------------------------------------------------

@registrar_caso(
    "publicar-otra-fuerza",
    "Ambulancia intenta publicar reporte en jerarquía de Policía",
    3,
    "La unidad amb-01 intenta emitir posición en 'reportes/policia/pol-01/posicion'.",
    "PUBLISH en 'reportes/policia/pol-01/posicion' con usuario 'amb-01'",
    "ACL deniega porque el usuario no coincide con la regex de policía (^pol-) y dispara WEBHOOK",
    "Receptor HTTP (puerto 5000): Aparece banner rojo de 'Publicación denegada'. PUBACK con código 135.",
)
def caso_publicar_otra_fuerza(host, port, ca):
    cli, err = cliente_conectado("amb-01", "amb-01", clave("amb-01"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        topic_prohibido = topics.posicion("policia", "pol-01")
        payload_data = json.dumps(payloads.posicion(-45.86, -67.48))
        autorizado, cod, det = intentar_publicacion(cli, topic_prohibido, payload_data, qos=1)
        time.sleep(1.0)  # margen para que el broker dispare el webhook HTTP
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


@registrar_caso(
    "publicar-otra-unidad",
    "Ambulancia intenta publicar posición en nombre de otra ambulancia",
    3,
    "La unidad amb-01 intenta emitir posición en 'reportes/ambulancia/amb-02/posicion'.",
    "PUBLISH en 'reportes/ambulancia/amb-02/posicion' con usuario 'amb-01'",
    "ACL deniega porque ${username} en la regla exige que sea 'amb-01' y dispara WEBHOOK",
    "Receptor HTTP (puerto 5000): Aparece 'Publicación denegada'. PUBACK con código 135.",
)
def caso_publicar_otra_unidad(host, port, ca):
    cli, err = cliente_conectado("amb-01", "amb-01", clave("amb-01"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        topic_prohibido = topics.posicion("ambulancia", "amb-02")
        payload_data = json.dumps(payloads.posicion(-45.86, -67.48))
        autorizado, cod, det = intentar_publicacion(cli, topic_prohibido, payload_data, qos=1)
        time.sleep(1.0)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


@registrar_caso(
    "suplantar-pedido",
    "Público intenta emitir pedido de auxilio con ID de otro teléfono",
    3,
    "Un cliente civil con su client_id intenta publicar en 'pedidos/<otro-client-id>/bomberos'.",
    "PUBLISH en 'pedidos/publico-OTRO/bomberos' desde un client_id distinto",
    "ACL deniega porque la regla 'pedidos/${clientid}/+' exige coincidencia exacta con su client_id",
    "PUBACK recibido con código 135. Receptor HTTP registra la denegación.",
)
def caso_suplantar_pedido(host, port, ca):
    cid_real = f"publico-{uuid.uuid4().hex}"
    cid_victima = f"publico-{uuid.uuid4().hex}"
    cli, err = cliente_conectado(cid_real, "publico", clave("publico"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        topic_prohibido = topics.pedido(cid_victima, "bomberos")
        payload_data = json.dumps(payloads.pedido(-45.86, -67.48))
        autorizado, cod, det = intentar_publicacion(cli, topic_prohibido, payload_data, qos=1)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


@registrar_caso(
    "ordenar-despacho",
    "Público intenta enviar comando de despacho de unidades",
    3,
    "Un usuario civil intenta publicar una orden de asignación en 'comandos/desp-01/ambulancia/amb-01'.",
    "PUBLISH en 'comandos/desp-01/ambulancia/amb-01' con usuario 'publico'",
    "ACL deniega porque solo usuarios con regex ^desp- pueden publicar comandos",
    "PUBACK recibido con código 135. El comando jamás llega al Servicio Central.",
)
def caso_ordenar_despacho(host, port, ca):
    cid = f"publico-{uuid.uuid4().hex}"
    cli, err = cliente_conectado(cid, "publico", clave("publico"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        topic_prohibido = topics.comando("desp-01", "ambulancia", "amb-01")
        payload_data = json.dumps(payloads.comando("asignar", eid="E-01"))
        autorizado, cod, det = intentar_publicacion(cli, topic_prohibido, payload_data, qos=1)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


@registrar_caso(
    "publicar-estado-falso",
    "Unidad intenta publicar estado oficial directamente en flota/#",
    3,
    "La unidad amb-01 intenta emitir su estado directamente en 'flota/ambulancia/amb-01/estado'.",
    "PUBLISH en 'flota/ambulancia/amb-01/estado' con usuario 'amb-01'",
    "ACL deniega porque 'flota/#' es monopolio exclusivo del Servicio Central",
    "PUBACK recibido con código 135. Ningún cliente puede adulterar la verdad oficial.",
)
def caso_publicar_estado_falso(host, port, ca):
    cli, err = cliente_conectado("amb-01", "amb-01", clave("amb-01"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        topic_prohibido = topics.flota_estado("ambulancia", "amb-01")
        payload_data = json.dumps({"v": 1, "estado": "online", "disponibilidad": "libre"})
        autorizado, cod, det = intentar_publicacion(cli, topic_prohibido, payload_data, qos=1)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


@registrar_caso(
    "retain-prohibido",
    "Unidad intenta publicar posición con flag 'retain=True'",
    3,
    "La unidad amb-01 intenta dejar retenida su posición en 'reportes/ambulancia/amb-01/posicion'.",
    "PUBLISH con retain=True en su propio topic de reporte de posición",
    "ACL deniega porque la regla fija {publish, [{retain, false}]} (retain solo se permite en presencia)",
    "PUBACK recibido con código 135. Evita que un mensaje viejo se reentregue en reconexiones.",
)
def caso_retain_prohibido(host, port, ca):
    cli, err = cliente_conectado("amb-01", "amb-01", clave("amb-01"), host, port, ca)
    if not cli:
        return False, "ERR_CONEXION", err
    try:
        topic_propio = topics.posicion("ambulancia", "amb-01")
        payload_data = json.dumps(payloads.posicion(-45.86, -67.48))
        autorizado, cod, det = intentar_publicacion(cli, topic_propio, payload_data, qos=1, retain=True)
        return not autorizado, cod, det
    finally:
        cli.loop_stop()
        cli.disconnect()


# -----------------------------------------------------------------------------
# CATEGORÍA 4: MENTIRAS SOBRE EL ESTADO Y ATAQUES AL SERVICIO CENTRAL
# -----------------------------------------------------------------------------

def servicio_esta_online(host, port, ca, timeout=2.0):
    """Consulta el tópico retained servicio/estado para verificar si el Servicio Central está activo."""
    cid = f"publico-{uuid.uuid4().hex}"
    cli, err = cliente_conectado(cid, "publico", clave("publico"), host, port, ca)
    if not cli:
        return False, f"No se pudo conectar para chequear servicio/estado: {err}"

    estado = {"online": False, "recibido": False, "raw": None}
    ev = threading.Event()

    def on_msg(c, userdata, msg):
        try:
            data = json.loads(msg.payload)
            estado["raw"] = data
            if data.get("estado") == "online":
                estado["online"] = True
            estado["recibido"] = True
            ev.set()
        except Exception:
            pass

    cli.on_message = on_msg
    cli.subscribe(topics.SERVICIO_ESTADO, qos=1)
    ev.wait(timeout)
    cli.loop_stop()
    cli.disconnect()

    if not estado["recibido"]:
        return False, "Sin estado retained en 'servicio/estado' (el servicio central nunca arrancó)"
    if not estado["online"]:
        return False, f"El Servicio Central está apagado (Last Will: {estado['raw']})"
    return True, "Servicio Central online"


def escuchar_alertas(topic_alerta, host, port, ca):
    """Inicia un cliente desp-01 para capturar alertas emitidas por el Servicio Central."""
    desp, err = cliente_conectado("desp-01", "desp-01", clave("desp-01"), host, port, ca)
    if not desp:
        return None, None, f"No se pudo conectar desp-01 para monitorear alertas: {err}"

    alerta_recibida = {"recibida": False, "datos": None}
    ev = threading.Event()

    def on_alerta(c, userdata, msg):
        try:
            data = json.loads(msg.payload)
            alerta_recibida["recibida"] = True
            alerta_recibida["datos"] = data
            ev.set()
        except Exception:
            pass

    desp.on_message = on_alerta
    desp.subscribe(topic_alerta, qos=1)
    time.sleep(0.5)  # asegurar que la suscripción quedó activa antes de publicar la trampa
    return desp, ev, alerta_recibida


@registrar_caso(
    "posicion-mar",
    "Unidad reporta coordenadas en el mar (fuera de zona)",
    4,
    "La unidad amb-01 reporta posición en el Golfo San Jorge (-45.87, -67.30), a 13 km de la costa.",
    "PUBLISH en 'reportes/ambulancia/amb-01/posicion' con coordenadas marítimas",
    "La ACL lo deja pasar (es su topic), pero el Servicio Central lo descarta ('fuera_de_zona') y emite alerta",
    "Mapa Despachador: El móvil no se mueve al agua. Servicio emite alerta en 'alertas/ambulancia'.",
)
def caso_posicion_mar(host, port, ca):
    # 1. Comprobar que el servicio esté online
    srv_ok, srv_msg = servicio_esta_online(host, port, ca)
    if not srv_ok:
        return False, "SERVICIO_OFFLINE", f"El Servicio Central no está activo: {srv_msg}"

    unidad = "amb-01"
    # 2. Conectar despachador para escuchar la alerta generada por el servicio
    desp, ev_alerta, alerta_info = escuchar_alertas(topics.alertas("ambulancia"), host, port, ca)
    if not desp:
        return False, "ERR_MONITOR", alerta_info

    cli, err = cliente_conectado(unidad, unidad, clave(unidad), host, port, ca)
    if not cli:
        desp.loop_stop()
        desp.disconnect()
        return False, "ERR_CONEXION", err

    try:
        punto_mar = (-45.8700, -67.3000)
        datos = payloads.posicion(lat=punto_mar[0], lon=punto_mar[1])
        intentar_publicacion(cli, topics.posicion("ambulancia", unidad), json.dumps(datos), qos=0)

        # Esperar que el Servicio Central procese y publique la alerta
        recibida = ev_alerta.wait(3.5)
        if recibida:
            det = alerta_info["datos"]
            return True, "ALERTA_CONFIRMADA", f"Alerta recibida en vivo del Servicio Central: motivo={det.get('motivo')}, mensaje='{det.get('mensaje')}'"
        else:
            return False, "SIN_ALERTA", "El Servicio Central no emitió la alerta en 'alertas/ambulancia' tras 3.5s"
    finally:
        cli.loop_stop()
        cli.disconnect()
        desp.loop_stop()
        desp.disconnect()


@registrar_caso(
    "salto-imposible",
    "Unidad reporta un salto de velocidad físicamente imposible",
    4,
    "La unidad amb-02 reporta una posición y 1 segundo después salta 8.6 km dentro de zona (>75 m/s límite).",
    "PUBLISH de dos posiciones consecutivas con delta de 8.600 metros en 1 segundo",
    "Servicio Central calcula distancia/dt > 75 m/s, descarta el salto y emite alerta 'salto_imposible'",
    "Mapa Despachador: El icono no se teletransporta. Consola del servicio registra el descarte.",
)
def caso_salto_imposible(host, port, ca):
    srv_ok, srv_msg = servicio_esta_online(host, port, ca)
    if not srv_ok:
        return False, "SERVICIO_OFFLINE", f"El Servicio Central no está activo: {srv_msg}"

    unidad = "amb-02"
    desp, ev_alerta, alerta_info = escuchar_alertas(topics.alertas("ambulancia"), host, port, ca)
    if not desp:
        return False, "ERR_MONITOR", alerta_info

    cli, err = cliente_conectado(unidad, unidad, clave(unidad), host, port, ca)
    if not cli:
        desp.loop_stop()
        desp.disconnect()
        return False, "ERR_CONEXION", err

    try:
        t0 = time.time()
        # Coordenada 1: centro de Comodoro (-45.8647, -67.4812)
        p1 = payloads.posicion(lat=-45.8647, lon=-67.4812, ts=t0)
        intentar_publicacion(cli, topics.posicion("ambulancia", unidad), json.dumps(p1), qos=0)
        time.sleep(1.0)

        # Coordenada 2: salto a 8.6 km al norte (-45.7960, -67.4286) dentro de zona en 1 segundo (~8.600 m/s > 75 m/s)
        p2 = payloads.posicion(lat=-45.7960, lon=-67.4286, ts=t0 + 1.0)
        intentar_publicacion(cli, topics.posicion("ambulancia", unidad), json.dumps(p2), qos=0)

        recibida = ev_alerta.wait(3.5)
        if recibida:
            det = alerta_info["datos"]
            return True, "ALERTA_CONFIRMADA", f"Alerta recibida en vivo del Servicio Central: motivo={det.get('motivo')}, mensaje='{det.get('mensaje')}'"
        else:
            return False, "SIN_ALERTA", "El Servicio Central no emitió la alerta 'salto_imposible' en 'alertas/ambulancia'"
    finally:
        cli.loop_stop()
        cli.disconnect()
        desp.loop_stop()
        desp.disconnect()


@registrar_caso(
    "ts-futuro",
    "Unidad reporta con timestamp desfasado 1 hora en el futuro",
    4,
    "La unidad amb-03 reporta telemetría con 'ts' 3600 segundos adelantado respecto al reloj real.",
    "PUBLISH con ts = ahora() + 3600",
    "Servicio Central verifica |hora_servicio - ts| <= 10.0s (TOLERANCIA_TS), descarta por 'ts_desfasado'",
    "El servicio no acepta telemetría adelantada para burlar la velocidad calculada.",
)
def caso_ts_futuro(host, port, ca):
    srv_ok, srv_msg = servicio_esta_online(host, port, ca)
    if not srv_ok:
        return False, "SERVICIO_OFFLINE", f"El Servicio Central no está activo: {srv_msg}"

    unidad = "amb-03"
    desp, ev_alerta, alerta_info = escuchar_alertas(topics.alertas("ambulancia"), host, port, ca)
    if not desp:
        return False, "ERR_MONITOR", alerta_info

    cli, err = cliente_conectado(unidad, unidad, clave(unidad), host, port, ca)
    if not cli:
        desp.loop_stop()
        desp.disconnect()
        return False, "ERR_CONEXION", err

    try:
        ts_adelantado = time.time() + 3600.0
        datos = payloads.posicion(lat=-45.8647, lon=-67.4812, ts=ts_adelantado)
        intentar_publicacion(cli, topics.posicion("ambulancia", unidad), json.dumps(datos), qos=0)

        recibida = ev_alerta.wait(3.5)
        if recibida:
            det = alerta_info["datos"]
            return True, "ALERTA_CONFIRMADA", f"Alerta recibida en vivo del Servicio Central: motivo={det.get('motivo')}, mensaje='{det.get('mensaje')}'"
        else:
            return False, "SIN_ALERTA", "El Servicio Central no emitió la alerta 'ts_desfasado' en 'alertas/ambulancia'"
    finally:
        cli.loop_stop()
        cli.disconnect()
        desp.loop_stop()
        desp.disconnect()


@registrar_caso(
    "ts-pasado",
    "Unidad reporta con timestamp de hace 1 hora (paquete viejo)",
    4,
    "La unidad amb-04 reporta telemetría con 'ts' 3600 segundos atrasado respecto al reloj real.",
    "PUBLISH con ts = ahora() - 3600",
    "Servicio Central descarta por desfasaje temporal respecto a la hora del servicio y monotonía",
    "El servicio ignora paquetes viejos acumulados por demoras o caídas de red.",
)
def caso_ts_pasado(host, port, ca):
    srv_ok, srv_msg = servicio_esta_online(host, port, ca)
    if not srv_ok:
        return False, "SERVICIO_OFFLINE", f"El Servicio Central no está activo: {srv_msg}"

    unidad = "amb-04"
    desp, ev_alerta, alerta_info = escuchar_alertas(topics.alertas("ambulancia"), host, port, ca)
    if not desp:
        return False, "ERR_MONITOR", alerta_info

    cli, err = cliente_conectado(unidad, unidad, clave(unidad), host, port, ca)
    if not cli:
        desp.loop_stop()
        desp.disconnect()
        return False, "ERR_CONEXION", err

    try:
        ts_atrasado = time.time() - 3600.0
        datos = payloads.posicion(lat=-45.8647, lon=-67.4812, ts=ts_atrasado)
        intentar_publicacion(cli, topics.posicion("ambulancia", unidad), json.dumps(datos), qos=0)

        recibida = ev_alerta.wait(3.5)
        if recibida:
            det = alerta_info["datos"]
            return True, "ALERTA_CONFIRMADA", f"Alerta recibida en vivo del Servicio Central: motivo={det.get('motivo')}, mensaje='{det.get('mensaje')}'"
        else:
            return False, "SIN_ALERTA", "El Servicio Central no emitió la alerta por timestamp atrasado en 'alertas/ambulancia'"
    finally:
        cli.loop_stop()
        cli.disconnect()
        desp.loop_stop()
        desp.disconnect()


@registrar_caso(
    "mensaje-hostil",
    "Cliente envía payloads maliciosos diseñados para voltear el servicio",
    4,
    "Envía enteros de 400 dígitos, JSON de 200.000 niveles y bytes no-UTF8 al topic de pedidos.",
    "PUBLISH en 'pedidos/<cid>/bomberos' con payloads destructivos (Issue #45)",
    "payloads.validar captura OverflowError y límites de recursión; el hilo de red de paho no muere",
    "El Servicio Central sigue activo. Se comprueba enviando un mensaje válido posterior y verificando vida.",
)
def caso_mensaje_hostil(host, port, ca):
    srv_ok, srv_msg = servicio_esta_online(host, port, ca)
    if not srv_ok:
        return False, "SERVICIO_OFFLINE", f"El Servicio Central no está activo: {srv_msg}"

    unidad = os.getenv("TRAMPOSO_UNIDAD", "amb-05")
    desp, err = cliente_conectado("desp-01", "desp-01", clave("desp-01"), host, port, ca)
    if not desp:
        return False, "ERR_MONITOR", err

    estado_recibido = threading.Event()
    ts_prueba = time.time()

    def on_flota(c, userdata, msg):
        try:
            d = json.loads(msg.payload)
            if d.get("ts") == ts_prueba:
                estado_recibido.set()
        except Exception:
            pass

    desp.on_message = on_flota
    desp.subscribe(topics.flota_estado("ambulancia", unidad), qos=1)
    time.sleep(0.5)

    cid = f"publico-{uuid.uuid4().hex}"
    cli_tramposo, err = cliente_conectado(cid, "publico", clave("publico"), host, port, ca)
    if not cli_tramposo:
        desp.loop_stop()
        desp.disconnect()
        return False, "ERR_CONEXION", err

    cli_unidad, err = cliente_conectado(unidad, unidad, clave(unidad), host, port, ca)
    if not cli_unidad:
        cli_tramposo.loop_stop()
        cli_tramposo.disconnect()
        desp.loop_stop()
        desp.disconnect()
        return False, "ERR_CONEXION", err

    mensajes_maliciosos = {
        "entero gigante (400 dígitos)": b'{"v":1,"id":"x","ts":1' + b"0" * 400 + b',"lat":-45.86,"lon":-67.48}',
        "JSON anidado (200k niveles)": b"[" * 200000,
        "bytes corruptos no-JSON": b"\xff\xfe no es json valido",
    }

    try:
        topic_pedido = topics.pedido(cid, "bomberos")
        for desc, payload_raw in mensajes_maliciosos.items():
            intentar_publicacion(cli_tramposo, topic_pedido, payload_raw, qos=1)

        time.sleep(1.0)

        # Ahora enviar una posición legítima con la unidad para comprobar que el servicio sigue vivo y procesa
        pos_valida = payloads.posicion(lat=-45.8647, lon=-67.4812, ts=ts_prueba)
        intentar_publicacion(cli_unidad, topics.posicion("ambulancia", unidad), json.dumps(pos_valida), qos=0)

        sigue_vivo = estado_recibido.wait(4.0)
        if sigue_vivo:
            return True, "RESISTIDO_Y_VIVO", "El servicio descartó los mensajes hostiles sin caerse y procesó con éxito el reporte posterior de la ambulancia."
        else:
            return False, "SERVICIO_NO_RESPONDE", "El servicio no actualizó flota/# tras los mensajes hostiles (se colgó o cayó)."
    finally:
        cli_tramposo.loop_stop()
        cli_tramposo.disconnect()
        cli_unidad.loop_stop()
        cli_unidad.disconnect()
        desp.loop_stop()
        desp.disconnect()


# =============================================================================
# EJECUCIÓN Y FORMATEO DE RESULTADOS
# =============================================================================

def ejecutar_caso(caso_info, host, port, ca, verbose=True):
    cid = caso_info["id"]
    nombre = caso_info["nombre"]
    cat = caso_info["categoria"]

    if verbose:
        print("\n" + "=" * 76)
        print(f" {negrita(f'[{cid}]')} {cyan(nombre)} {tenue(f'(Categoría {cat})')}")
        print("=" * 76)
        print(f" {negrita('Contexto:')}   {caso_info['descripcion']}")
        print(f" {negrita('Intento:')}    {amarillo(caso_info['accion'])}")
        print(f" {negrita('Esperado:')}   {caso_info['defensa']}")
        print(f" {negrita('Ver en:')}     {azul(caso_info['verificacion'])}")
        print("-" * 76)

    try:
        bloqueado, cod, det = caso_info["handler"](host, port, ca)
    except Exception as e:
        bloqueado, cod, det = False, "EXCEPCION_INESPERADA", str(e)

    es_error_red = cod in ("ERR_CONEXION", "ERROR_RED", "ERROR_SOCKET", "TIMEOUT")

    if verbose:
        if es_error_red:
            print(f" {negrita('Resultado:')}  {amarillo('⚠ ERROR DE CONEXIÓN AL BROKER')}")
            print(f" {negrita('Detalle:')}    {amarillo(str(det))}")
            print(f" {negrita('Nota:')}       {tenue(f'El broker no respondió en {host}:{port}. Verificá que el contenedor esté corriendo.')}")
        elif bloqueado:
            print(f" {negrita('Resultado:')}  {verde('✔ BLOQUEADO / DEFENDIDO EXITOSAMENTE')}")
            print(f" {negrita('Detalle:')}    {verde(str(det))}")
        else:
            print(f" {negrita('Resultado:')}  {rojo('✖ FALLO / VULNERABILIDAD DETECTADA')}")
            print(f" {negrita('Detalle:')}    {rojo(f'El sistema permitió la acción: {det}')}")

    return {
        "id": cid,
        "nombre": nombre,
        "categoria": cat,
        "exito": bloqueado and not es_error_red,
        "error_red": es_error_red,
        "codigo": cod,
        "detalle": det,
    }


def ejecutar_todos(host, port, ca, filtro_cat=None):
    print("\n" + negrita(azul("=" * 76)))
    print(negrita(azul(" EJECUTANDO SUITE COMPLETA DE CLIENTE TRAMPOSO")))
    print(negrita(azul("=" * 76)))
    print(f" Destino Broker: {cyan(f'{host}:{port}')} | CA: {cyan(ca)}")

    casos_a_ejecutar = [c for c in CASOS.values() if filtro_cat is None or c["categoria"] == filtro_cat]
    resultados = []

    for caso in casos_a_ejecutar:
        res = ejecutar_caso(caso, host, port, ca, verbose=True)
        resultados.append(res)
        time.sleep(0.3)

    # Tabla Resumen Final
    print("\n" + "=" * 76)
    print(negrita(" RESUMEN FINAL DE LA SUITE DE TRAMPAS"))
    print("=" * 76)
    print(f" {'ID':<22} | {'CATEGORÍA':<10} | {'ESTADO':<15} | {'CÓDIGO'}")
    print("-" * 76)

    pasados = 0
    errores_red = 0
    for r in resultados:
        if r.get("error_red"):
            estado_str = amarillo("ERROR CONEXIÓN")
            errores_red += 1
        elif r["exito"]:
            estado_str = verde("DEFENDIDO")
            pasados += 1
        else:
            estado_str = rojo("VULNERABLE")
        print(f" {r['id']:<22} | Cat. {r['categoria']:<5} | {estado_str:<24} | {r['codigo']}")

    print("-" * 76)
    fallidos = len(resultados) - pasados - errores_red
    if errores_red > 0:
        print(amarillo(f" Total de pruebas: {len(resultados)} | Exitosas: {pasados} | Vulnerables: {fallidos} | Errores de conexión: {errores_red}"))
        print(amarillo(f" Atención: {errores_red} prueba(s) fallaron por falta de conexión al broker en {host}:{port}."))
    else:
        resumen_color = verde if pasados == len(resultados) else rojo
        print(resumen_color(f" Total de pruebas: {len(resultados)} | Exitosas: {pasados} | Vulnerables: {fallidos}"))
    print("=" * 76)
    return pasados == len(resultados) and errores_red == 0


def menu_interactivo(host, port, ca):
    while True:
        print("\n" + negrita(azul("╔════════════════════════════════════════════════════════════════════════════╗")))
        print(negrita(azul("║               SUITE CLIENTE TRAMPOSO - SISTEMAS DISTRIBUIDOS               ║")))
        print(negrita(azul("║               Demostración en Vivo de Seguridad                            ║")))
        print(negrita(azul("╚════════════════════════════════════════════════════════════════════════════╝")))
        print(f" Conectado a: {cyan(f'{host}:{port}')}\n")

        print(negrita(" Categorías de Demostración:"))
        print(f"  {negrita('1.')} Conexión & Robo de Sesión / Client ID {tenue('(Rechazos en CONNECT por cinfo)')}")
        print(f"  {negrita('2.')} Espionaje & Suscripciones Prohibidas {tenue('(Rechazos SUBACK 135 por ACL)')}")
        print(f"  {negrita('3.')} Suplantación de Identidad & Bad Topics {tenue('(PUBACK 135 & Webhook Receptor)')}")
        print(f"  {negrita('4.')} Mentiras de Estado & Ataques al Servicio {tenue('(Validador geo/ts & payloads)')}")
        print(f"  {negrita('T.')} {verde('Ejecutar TODOS los 20 casos en secuencia')}")
        print(f"  {negrita('L.')} {amarillo('Listar todos los casos con sus IDs')}")
        print(f"  {negrita('0.')} Salir\n")

        opcion = input(negrita(" Seleccioná una opción o ingresá el ID de un caso: ")).strip().lower()

        if opcion in ("0", "salir", "exit", "q"):
            print("Chau!")
            break
        elif opcion in ("t", "todos"):
            ejecutar_todos(host, port, ca)
        elif opcion in ("l", "listar"):
            listar_casos()
        elif opcion in ("1", "2", "3", "4"):
            cat_num = int(opcion)
            print(f"\n--- Ejecutando pruebas de la Categoría {cat_num} ---")
            ejecutar_todos(host, port, ca, filtro_cat=cat_num)
        elif opcion in CASOS:
            ejecutar_caso(CASOS[opcion], host, port, ca, verbose=True)
        else:
            print(rojo(f"Opción desconocida: '{opcion}'. Escribí un número, un ID o 'todos'."))


def listar_casos():
    print("\n" + negrita("LISTADO COMPLETO DE CASOS DE CLIENTE TRAMPOSO:"))
    print("-" * 76)
    for cat in (1, 2, 3, 4):
        nombres_cat = {
            1: "1. Autenticación y Robo de Client ID (cinfo)",
            2: "2. Espionaje y Suscripciones Prohibidas (ACL)",
            3: "3. Suplantación de Identidad y Bad Topics (ACL)",
            4: "4. Mentiras de Estado y Ataques al Servicio Central",
        }
        print(f"\n{negrita(amarillo(nombres_cat[cat]))}")
        for cid, info in CASOS.items():
            if info["categoria"] == cat:
                print(f"  {cyan(f'--caso {cid:<22}')} {info['nombre']}")
    print("-" * 76)


# =============================================================================
# ENTRYPOINT PRINCIPAL
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Cliente Tramposo para demostración de seguridad y robustez.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--host", default=HOST_DEFAULT, help="IP o hostname del broker EMQX (def: localhost o MQTT_HOST)")
    parser.add_argument("--port", type=int, default=PORT_DEFAULT, help="Puerto TLS del broker (def: 8883 o MQTT_PORT)")
    parser.add_argument("--ca-cert", default=CA_CERT_DEFAULT, help="Ruta al certificado CA propio (def: broker/certs/ca.crt)")
    parser.add_argument("--todos", action="store_true", help="Ejecuta toda la suite completa secuencialmente")
    parser.add_argument("--categoria", "--cat", type=int, choices=[1, 2, 3, 4], help="Ejecuta todos los casos de una categoría")
    parser.add_argument("--caso", help="Ejecuta un caso puntual por su ID (ej: --caso espiar-flota)")
    parser.add_argument("--listar", action="store_true", help="Muestra todos los casos disponibles y sus IDs")

    # Flags abreviados directos
    for cid in CASOS:
        parser.add_argument(f"--{cid}", action="store_true", help=f"Ejecuta caso '{cid}'")

    args = parser.parse_args()

    # Si se pide listar
    if args.listar:
        listar_casos()
        return

    # Verificación de disponibilidad del broker antes de ejecutar cualquier prueba
    broker_ok, err_broker = verificar_broker(args.host, args.port)
    if not broker_ok:
        print("\n" + rojo("=" * 76))
        print(negrita(rojo(" ERROR: NO SE PUDO CONECTAR CON EL BROKER EMQX ")))
        print(rojo("=" * 76))
        print(f" Destino probado: {cyan(f'{args.host}:{args.port}')}")
        print(f" Causa de fallo:  {amarillo(err_broker)}\n")
        print(negrita(" Posibles causas y soluciones:"))
        print(f"  1. {negrita('Si probás en local:')} El contenedor de EMQX no está levantado.")
        print(f"     Levantalo con:   {verde('cd broker && docker compose up -d')}")
        print(f"  2. {negrita('Si probás en Tailscale / Remoto:')} El script por defecto busca en 'localhost'.")
        print(f"     Pasá la IP con:  {verde(f'python pruebas/tramposo.py --host <ip-broker>')}")
        print(f"     O exportá:       {verde(f'export MQTT_HOST=<ip-broker>')}")
        print(rojo("=" * 76) + "\n")
        sys.exit(1)

    # Si se pasó un caso específico por flag directo (--espiar-flota, etc.)
    for cid in CASOS:
        if getattr(args, cid.replace("-", "_"), False):
            res = ejecutar_caso(CASOS[cid], args.host, args.port, args.ca_cert, verbose=True)
            sys.exit(0 if res["exito"] else 1)

    # Si se pasó --caso <nombre>
    if args.caso:
        if args.caso not in CASOS:
            print(rojo(f"Error: Caso '{args.caso}' no existe. Usá --listar para ver opciones."))
            sys.exit(1)
        res = ejecutar_caso(CASOS[args.caso], args.host, args.port, args.ca_cert, verbose=True)
        sys.exit(0 if res["exito"] else 1)

    # Si se pasó --categoria
    if args.categoria:
        ok = ejecutar_todos(args.host, args.port, args.ca_cert, filtro_cat=args.categoria)
        sys.exit(0 if ok else 1)

    # Si se pasó --todos
    if args.todos:
        ok = ejecutar_todos(args.host, args.port, args.ca_cert)
        sys.exit(0 if ok else 1)

    # Si no se pasaron argumentos, lanzar Menú Interactivo
    menu_interactivo(args.host, args.port, args.ca_cert)


if __name__ == "__main__":
    main()
