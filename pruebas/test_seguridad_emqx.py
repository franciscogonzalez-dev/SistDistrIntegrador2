#!/usr/bin/env python3
"""
pruebas/test_seguridad_emqx.py
Suite de validación automatizada de Seguridad y Control de Acceso (Issue #6 / Requerimiento 5).

Verifica exhaustivamente:
1. Sin anónimos: intentos sin usuario/clave son rechazados.
2. Autenticador cinfo primero en la cadena:
   - Atar client ID al usuario: 'publico' exige 'publico-<32 hex>'.
   - El resto exige client_id == username (impide robo de sesión y spoofing).
3. Autenticación con clave: claves incorrectas son rechazadas por la base interna.
4. Matriz ACL (docs/acl.md):
   - Expansión de ${clientid} en tópicos de pedidos del público.
   - Aislamiento total del público (solo lee servicio/estado).
   - Segregación por fuerza en unidades móviles (amb-, pol-, bom-).
   - Firma de comandos del despachador (comandos/${username}/+/+).
   - Monopolio de estado del servicio central.
   - Denegación explícita final ({deny, all}).
"""

import os
import ssl
import sys
import time
from pathlib import Path
import paho.mqtt.client as mqtt

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from clientes.credenciales import clave

CA_CERT = str(ROOT_DIR / "broker" / "certs" / "ca.crt")
HOST = os.getenv("MQTT_HOST", "localhost")
PORT = int(os.getenv("MQTT_PORT", "8883"))

# Contadores de aserciones
tests_ejecutados = 0
tests_fallidos = 0


def registrar_resultado(descripcion, exito, detalle=""):
    global tests_ejecutados, tests_fallidos
    tests_ejecutados += 1
    if exito:
        print(f"  [PASS] {descripcion}")
    else:
        tests_fallidos += 1
        print(f"  [FAIL] {descripcion} -> {detalle}")


def probar_conexion(client_id, username=None, password=None):
    """Intenta conectar con TLS y devuelve (conectado: bool, reason_code: str/int)"""
    resultado = {"conectado": False, "codigo": None}

    def on_connect(c, userdata, flags, rc, properties=None):
        resultado["codigo"] = getattr(rc, "value", rc)
        if resultado["codigo"] == 0:
            resultado["conectado"] = True

    c = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTProtocolVersion.MQTTv5,
    )
    c.tls_set(ca_certs=CA_CERT, cert_reqs=ssl.CERT_REQUIRED)
    c.tls_insecure_set(True)
    if username is not None:
        c.username_pw_set(username, password)
    c.on_connect = on_connect

    try:
        c.connect(HOST, PORT, keepalive=5)
        c.loop_start()
        t0 = time.time()
        while resultado["codigo"] is None and time.time() - t0 < 3:
            time.sleep(0.05)
        c.loop_stop()
        c.disconnect()
    except Exception as e:
        return False, str(e)

    return resultado["conectado"], resultado["codigo"]


def crear_cliente_autenticado(client_id, username, password):
    """Crea un cliente conectado y en bucle para pruebas de ACL"""
    conexion = {"conectado": False, "codigo": None}

    def on_connect(c, userdata, flags, rc, properties=None):
        conexion["codigo"] = getattr(rc, "value", rc)
        if conexion["codigo"] == 0:
            conexion["conectado"] = True

    c = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTProtocolVersion.MQTTv5,
    )
    c.tls_set(ca_certs=CA_CERT, cert_reqs=ssl.CERT_REQUIRED)
    c.tls_insecure_set(True)
    c.username_pw_set(username, password)
    c.on_connect = on_connect

    c.connect(HOST, PORT, keepalive=10)
    c.loop_start()
    t0 = time.time()
    while not conexion["conectado"] and time.time() - t0 < 3:
        time.sleep(0.05)

    if not conexion["conectado"]:
        raise RuntimeError(f"No se pudo conectar {username}: {conexion['codigo']}")
    return c


def probar_subscripcion(cliente, topic, qos=1):
    """Prueba si el broker acepta la suscripción (devuelve True si granted QoS 0/1/2, False si 135)"""
    resp = {"codigo": None}

    def on_subscribe(c, userdata, mid, reason_codes, properties=None):
        cod = reason_codes[0]
        resp["codigo"] = getattr(cod, "value", cod)

    cliente.on_subscribe = on_subscribe
    cliente.subscribe(topic, qos=qos)
    t0 = time.time()
    while resp["codigo"] is None and time.time() - t0 < 2:
        time.sleep(0.05)

    # 128 o superior indica error en MQTT v5 (135 = Not authorized)
    if resp["codigo"] is not None and resp["codigo"] < 128:
        return True, resp["codigo"]
    return False, resp["codigo"]


def probar_publicacion(cliente, topic, payload="{}", qos=1, retain=False):
    """
    Prueba si el broker acepta publicar (devuelve True si recibe PUBACK de éxito,
    False si no es autorizado o falla con reason_code >= 128).
    """
    resp = {"codigo": None, "publicado": False}

    def on_publish(c, userdata, mid, reason_code=None, properties=None):
        cod = getattr(reason_code, "value", reason_code) if reason_code is not None else 0
        resp["codigo"] = cod
        if cod < 128:
            resp["publicado"] = True

    cliente.on_publish = on_publish
    msg_info = cliente.publish(topic, payload, qos=qos, retain=retain)
    try:
        msg_info.wait_for_publish(timeout=2)
        # Si wait_for_publish no lanzó excepción y el reason_code es menor a 128
        cod = getattr(msg_info.rc, "value", msg_info.rc)
        if cod == 0 and resp["codigo"] is not None:
            return (resp["codigo"] < 128), resp["codigo"]
        return (cod == 0), cod
    except Exception as e:
        return False, str(e)


def esperar_broker(timeout=15):
    """Espera hasta que el puerto SSL del broker esté listo para aceptar conexiones."""
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection((HOST, PORT), timeout=1):
                time.sleep(1) # Pequeño margen para asegurar que el listener TLS completó el handshake inicial
                return True
        except (socket.error, ConnectionRefusedError):
            time.sleep(0.5)
    return False


def ejecutar_pruebas():
    print("=" * 70)
    print("EJECUTANDO SUITE DE PRUEBAS DE SEGURIDAD EMQX (ISSUE #6)")
    print("=" * 70)

    print(f"Verificando disponibilidad de EMQX en {HOST}:{PORT}...")
    if not esperar_broker():
        print(f"Error: EMQX no respondió en {HOST}:{PORT} tras esperar.")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # BLOQUE 1: AUTENTICACIÓN Y CLIENT ID ATADO AL USUARIO (cinfo)
    # -------------------------------------------------------------------------
    print("\n--- 1. Autenticación y Atadura de Client ID (cinfo) ---")

    ok, cod = probar_conexion("anon-123")
    registrar_resultado("Conexión anónima rechazada", not ok, f"conectado={ok}")

    ok, cod = probar_conexion("amb-01", "publico", clave("publico"))
    registrar_resultado("Público con client ID 'amb-01' rechazado (robo identidad)", not ok, f"conectado={ok}")

    ok, cod = probar_conexion("publico-invalido", "publico", clave("publico"))
    registrar_resultado("Público con client ID sin 32 hex rechazado", not ok, f"conectado={ok}")

    cid_pub = "publico-550e8400e29b41d4a716446655440001"
    ok, cod = probar_conexion(cid_pub, "publico", "clave_erronea_123")
    registrar_resultado("Público con clave errónea rechazado por base interna", not ok, f"conectado={ok}")

    ok, cod = probar_conexion(cid_pub, "publico", clave("publico"))
    registrar_resultado("Público con client ID y clave correctos conectado", ok, f"codigo={cod}")

    ok, cod = probar_conexion("servicio", "amb-01", clave("amb-01"))
    registrar_resultado("Unidad amb-01 con client ID 'servicio' rechazada", not ok, f"conectado={ok}")

    ok, cod = probar_conexion("amb-01", "amb-01", "clave_erronea_123")
    registrar_resultado("Unidad amb-01 con clave errónea rechazada", not ok, f"conectado={ok}")

    ok, cod = probar_conexion("amb-01", "amb-01", clave("amb-01"))
    registrar_resultado("Unidad amb-01 con client ID y clave correctos conectada", ok, f"codigo={cod}")

    ok, cod = probar_conexion("desp-01", "desp-01", clave("desp-01"))
    registrar_resultado("Despachador con client ID y clave correctos conectado", ok, f"codigo={cod}")

    ok, cod = probar_conexion("servicio", "servicio", clave("servicio"))
    registrar_resultado("Servicio con client ID y clave correctos conectado", ok, f"codigo={cod}")

    # -------------------------------------------------------------------------
    # BLOQUE 2: CONTROL DE ACCESO ACL - PÚBLICO (docs/acl.md)
    # -------------------------------------------------------------------------
    print("\n--- 2. Reglas ACL: Público y expansión de ${clientid} ---")
    cli_pub = crear_cliente_autenticado(cid_pub, "publico", clave("publico"))

    # Publicación con su propio clientid
    ok, cod = probar_publicacion(cli_pub, f"pedidos/{cid_pub}/bomberos")
    registrar_resultado(f"Público publica en pedidos/{cid_pub}/bomberos (${{clientid}})", ok, f"codigo={cod}")

    # Un pedido retenido se le volvería a entregar al servicio en cada reconexión
    ok, cod = probar_publicacion(cli_pub, f"pedidos/{cid_pub}/bomberos", retain=True)
    registrar_resultado("Público publicando un pedido retenido denegado", not ok, f"permitido={ok}")

    # Intento de publicar haciéndose pasar por otro clientid
    otro_cid = "publico-999e8400e29b41d4a716446655449999"
    ok, cod = probar_publicacion(cli_pub, f"pedidos/{otro_cid}/bomberos")
    registrar_resultado(f"Público publicando en pedidos/{otro_cid}/bomberos denegado", not ok, f"permitido={ok}")

    # Intento de publicar en reportes de ambulancia
    ok, cod = probar_publicacion(cli_pub, "reportes/ambulancia/amb-01/posicion")
    registrar_resultado("Público publicando en reportes/ ambulancia denegado", not ok, f"permitido={ok}")

    # Suscripciones del público
    ok, cod = probar_subscripcion(cli_pub, "servicio/estado")
    registrar_resultado("Público suscrito a 'servicio/estado' concedido", ok, f"codigo={cod}")

    ok, cod = probar_subscripcion(cli_pub, "flota/#")
    registrar_resultado("Público suscrito a 'flota/#' denegado (aislamiento total)", not ok, f"codigo={cod}")

    ok, cod = probar_subscripcion(cli_pub, "reportes/#")
    registrar_resultado("Público suscrito a 'reportes/#' denegado", not ok, f"codigo={cod}")

    cli_pub.loop_stop()
    cli_pub.disconnect()

    # -------------------------------------------------------------------------
    # BLOQUE 3: CONTROL DE ACCESO ACL - UNIDAD MÓVIL (amb-01)
    # -------------------------------------------------------------------------
    print("\n--- 3. Reglas ACL: Unidades Móviles (Segregación por rol) ---")
    cli_amb = crear_cliente_autenticado("amb-01", "amb-01", clave("amb-01"))

    # Publicaciones de amb-01
    ok, cod = probar_publicacion(cli_amb, "reportes/ambulancia/amb-01/posicion")
    registrar_resultado("amb-01 publica su propia posicion concedido", ok, f"codigo={cod}")

    ok, cod = probar_publicacion(cli_amb, "reportes/ambulancia/amb-01/presencia")
    registrar_resultado("amb-01 publica su propia presencia concedido", ok, f"codigo={cod}")

    # La presencia es el único topic de entrada que lleva retained (docs/topics.md)
    ok, cod = probar_publicacion(cli_amb, "reportes/ambulancia/amb-01/presencia", '{"v":1,"estado":"offline"}', retain=True)
    registrar_resultado("amb-01 publica su presencia retenida concedido", ok, f"codigo={cod}")

    ok, cod = probar_publicacion(cli_amb, "reportes/ambulancia/amb-01/posicion", retain=True)
    registrar_resultado("amb-01 publicando una posicion retenida denegado", not ok, f"permitido={ok}")

    # Intento de publicar posición de otra ambulancia
    ok, cod = probar_publicacion(cli_amb, "reportes/ambulancia/amb-02/posicion")
    registrar_resultado("amb-01 publicando posicion de amb-02 denegado", not ok, f"permitido={ok}")

    # Intento de publicar en otra fuerza (policía)
    ok, cod = probar_publicacion(cli_amb, "reportes/policia/amb-01/posicion")
    registrar_resultado("amb-01 publicando en reportes/policia denegado", not ok, f"permitido={ok}")

    # Suscripciones denegadas para unidades (no leen nada)
    ok, cod = probar_subscripcion(cli_amb, "flota/#")
    registrar_resultado("amb-01 suscrito a 'flota/#' denegado", not ok, f"codigo={cod}")

    ok, cod = probar_subscripcion(cli_amb, "comandos/#")
    registrar_resultado("amb-01 suscrito a 'comandos/#' denegado", not ok, f"codigo={cod}")

    cli_amb.loop_stop()
    cli_amb.disconnect()

    # -------------------------------------------------------------------------
    # BLOQUE 4: CONTROL DE ACCESO ACL - DESPACHADOR (desp-01)
    # -------------------------------------------------------------------------
    print("\n--- 4. Reglas ACL: Despachador General (desp-01) ---")
    cli_desp = crear_cliente_autenticado("desp-01", "desp-01", clave("desp-01"))

    # Publicar comandos con su propio usuario
    ok, cod = probar_publicacion(cli_desp, "comandos/desp-01/ambulancia/amb-01")
    registrar_resultado("desp-01 publica comandos/desp-01/ambulancia/amb-01 concedido", ok, f"codigo={cod}")

    ok, cod = probar_publicacion(cli_desp, "comandos/desp-01/ambulancia/amb-01", retain=True)
    registrar_resultado("desp-01 publicando un comando retenido denegado", not ok, f"permitido={ok}")

    # Intento de publicar comandos en nombre de otro despachador
    ok, cod = probar_publicacion(cli_desp, "comandos/desp-02/ambulancia/amb-01")
    registrar_resultado("desp-01 publicando como desp-02 denegado", not ok, f"permitido={ok}")

    # Suscripciones del despachador
    ok, cod = probar_subscripcion(cli_desp, "flota/#")
    registrar_resultado("desp-01 suscrito a 'flota/#' concedido", ok, f"codigo={cod}")

    ok, cod = probar_subscripcion(cli_desp, "emergencias/#")
    registrar_resultado("desp-01 suscrito a 'emergencias/#' concedido", ok, f"codigo={cod}")

    ok, cod = probar_subscripcion(cli_desp, "alertas/#")
    registrar_resultado("desp-01 suscrito a 'alertas/#' concedido", ok, f"codigo={cod}")

    ok, cod = probar_subscripcion(cli_desp, "respuestas/desp-01")
    registrar_resultado("desp-01 suscrito a 'respuestas/desp-01' concedido", ok, f"codigo={cod}")

    # Intento de leer reportes en crudo (el despachador no lee reportes/#)
    ok, cod = probar_subscripcion(cli_desp, "reportes/#")
    registrar_resultado("desp-01 suscrito a 'reportes/#' denegado", not ok, f"codigo={cod}")

    cli_desp.loop_stop()
    cli_desp.disconnect()

    # -------------------------------------------------------------------------
    # BLOQUE 5: CONTROL DE ACCESO ACL - SERVICIO CENTRAL (servicio)
    # -------------------------------------------------------------------------
    print("\n--- 5. Reglas ACL: Servicio Central (servicio) ---")
    cli_srv = crear_cliente_autenticado("servicio", "servicio", clave("servicio"))

    ok, cod = probar_publicacion(cli_srv, "flota/ambulancia/amb-01/estado")
    registrar_resultado("servicio publica en flota/# concedido", ok, f"codigo={cod}")

    ok, cod = probar_publicacion(cli_srv, "emergencias/policia/e-001")
    registrar_resultado("servicio publica en emergencias/# concedido", ok, f"codigo={cod}")

    ok, cod = probar_subscripcion(cli_srv, "reportes/+/+/posicion")
    registrar_resultado("servicio suscrito a reportes/+/+/posicion concedido", ok, f"codigo={cod}")

    ok, cod = probar_subscripcion(cli_srv, "pedidos/+/+")
    registrar_resultado("servicio suscrito a pedidos/+/+ concedido", ok, f"codigo={cod}")

    cli_srv.loop_stop()
    cli_srv.disconnect()

    # -------------------------------------------------------------------------
    # RESUMEN
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"TOTAL TESTS: {tests_ejecutados} | EXITOSOS: {tests_ejecutados - tests_fallidos} | FALLIDOS: {tests_fallidos}")
    print("=" * 70)

    if tests_fallidos > 0:
        sys.exit(1)


if __name__ == "__main__":
    ejecutar_pruebas()
