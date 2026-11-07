#!/usr/bin/env python3
"""
pruebas/tramposo_mensaje_hostil.py
Cliente tramposo: el público manda mensajes hechos para romper al servicio (Issue #45, para la demo de #19).

La ACL los deja pasar, porque el topic es el suyo: el que tiene que aguantarlos es el servicio. Para comprobarlo
se reporta una posición válida después de los mensajes y se espera que aparezca en flota/, que solo publica el servicio.

Necesita el broker y el servicio levantados. Usa los client ID de amb-05 y desp-01, así que
desconecta a esos clientes si están corriendo: amb-05 tiene que estar parada y el mapa cerrado.

    MQTT_HOST=<ip-broker> python pruebas/tramposo_mensaje_hostil.py
"""

import json
import os
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from clientes.credenciales import clave
from comun import payloads, topics

CA_CERT = os.getenv("MQTT_CA_CERTS", str(ROOT_DIR / "broker" / "certs" / "ca.crt"))
HOST = os.getenv("MQTT_HOST", "localhost")
PORT = int(os.getenv("MQTT_PORT", "8883"))

UNIDAD = "amb-05"
TIPO = "ambulancia"
ESPERA_SEG = 10

MENSAJES_HOSTILES = {
    "entero de 400 dígitos": b'{"v":1,"id":"x","ts":1' + b"0" * 400 + b',"lat":-45.86,"lon":-67.48}',
    "JSON anidado 200.000 niveles": b"[" * 200000,
    "no es JSON": b"\xff\xfe no es json",
}


def conectar(client_id, usuario):
    conectado = threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTProtocolVersion.MQTTv5)
    c.tls_set(ca_certs=CA_CERT, cert_reqs=ssl.CERT_REQUIRED)
    c.username_pw_set(usuario, clave(usuario))
    c.on_connect = lambda *_: conectado.set()
    return c, conectado


def arrancar(c, conectado):
    c.connect(HOST, PORT, keepalive=30)
    c.loop_start()
    if not conectado.wait(5):
        raise SystemExit(f"No se pudo conectar al broker en {HOST}:{PORT}")


def main():
    rutas = json.loads((ROOT_DIR / "clientes" / "rutas" / "ambulancias.json").read_text(encoding="utf-8"))
    lat, lon = rutas[UNIDAD]["coordenadas"][0]

    posicion = payloads.posicion(lat=lat, lon=lon)
    recibida = threading.Event()

    def on_flota(client, userdata, msg):
        #el retained viejo llega primero: se espera el estado con el ts de esta prueba
        if json.loads(msg.payload).get("ts") == posicion["ts"]:
            recibida.set()

    desp, conectado = conectar("desp-01", "desp-01")
    desp.on_message = on_flota
    arrancar(desp, conectado)
    desp.subscribe(topics.flota_estado(TIPO, UNIDAD), qos=1)

    client_id = "publico-" + uuid.uuid4().hex
    tramposo, conectado = conectar(client_id, "publico")
    arrancar(tramposo, conectado)
    topic = topics.pedido(client_id, "bomberos")
    for descripcion, payload in MENSAJES_HOSTILES.items():
        info = tramposo.publish(topic, payload, qos=1)
        info.wait_for_publish(5)
        print(f"  Publicado como público: {descripcion}")

    time.sleep(2)  # que el servicio llegue a procesarlos antes de la posición

    unidad, conectado = conectar(UNIDAD, UNIDAD)
    arrancar(unidad, conectado)
    unidad.publish(topics.posicion(TIPO, UNIDAD), json.dumps(posicion), qos=0)

    sigue_vivo = recibida.wait(ESPERA_SEG)
    for c in (unidad, tramposo, desp):
        c.loop_stop()
        c.disconnect()

    if sigue_vivo:
        print("[PASS] El servicio sigue procesando después de los mensajes hostiles")
        return
    print(f"[FAIL] La posición de {UNIDAD} no apareció en flota/ en {ESPERA_SEG} s: el servicio no está atendiendo")
    sys.exit(1)


if __name__ == "__main__":
    main()
