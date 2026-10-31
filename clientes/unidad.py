"""
Simulador de Unidad Móvil de Emergencia (Comodoro Rivadavia)
Proyecto 2 - Sistemas Distribuidos (UNPSJB)

Este módulo implementa el Cliente Móvil (ambulancia, patrullero o bomberos)
que carga sus datos y coordenadas directamente desde las tablas JSON en 'clientes/rutas/'
(ambulancias.json, bomberos.json, policias.json) según el ID elegido.

Hereda de MQTTPub (clientes/publisher.py), reutilizando su gestión
de cliente MQTT v5, autenticación, Last Will y publicaciones.

EJEMPLOS DE EJECUCIÓN INDIVIDUAL:
  # Ejecutar una ambulancia (queda estacionada en base fija emitiendo presencia y posición):
  python clientes/unidad.py --id amb-01

  # Ejecutar un patrullero (inicia patrullaje vial continuo con odómetro a 36 km/h):
  python clientes/unidad.py --id pol-01

  # Ejecutar una autobomba de bomberos:
  python clientes/unidad.py --id bom-02

  # Ejecutar indicando IP y puerto del broker explícitamente:
  python clientes/unidad.py --id pol-02 --broker 127.0.0.1 --puerto 1883

  # Ejecución corta posicional (sin el flag --id):
  python clientes/unidad.py pol-03
"""

import os
import sys
import json
import math
import time
import argparse
from publisher import MQTTPub

IP_BROKER_DEFAULT = "127.0.0.1"
PUERTO_BROKER_DEFAULT = 1883
PASSWORD_DEFAULT = "clave_unidad"

VELOCIDAD_PATRULLA_MS = 10.0   # 10 metros/segundo = 36 km/h para patrulleros
INTERVALO_REPORTE_SEG = 1   # 1 publicación cada 1 segundo

# Carpeta donde están las 3 tablas JSON
CARPETA_RUTAS = "clientes/rutas"

def cargar_coordenadas(id_unidad, tipo=None):
    """
    Carga las coordenadas desde el JSON correspondiente usando un if/else simple.
    """
    # 1. Si no pasaron el tipo, lo deducimos con un if por el ID
    if not tipo:
        if id_unidad.startswith("pol"):
            tipo = "policia"
        elif id_unidad.startswith("bom"):
            tipo = "bomberos"
        else:
            tipo = "ambulancia"

    # 2. Elegimos el archivo con if/else directo
    if tipo == "ambulancia":
        archivo = "clientes/rutas/ambulancias.json"
    elif tipo == "bomberos":
        archivo = "clientes/rutas/bomberos.json"
    else:
        archivo = "clientes/rutas/policias.json"

    # 3. Leemos el archivo y devolvemos las coordenadas de esa unidad
    with open(archivo, "r", encoding="utf-8") as f:
        datos = json.load(f)

    return datos[id_unidad]["coordenadas"], tipo


def odometro_simple(ruta, paso_metros=VELOCIDAD_PATRULLA_MS):
    """
    Odómetro virtual:
    Convierte las esquinas de la calle en pasos regulares de 'paso_metros' (10m = 36 km/h).
    Cada elemento resultante equivale exactamente a 1 segundo de viaje.
    """
    if not ruta or len(ruta) < 2:
        return ruta

    pasos = [ruta[0]]
    for i in range(len(ruta) - 1):
        p1, p2 = ruta[i], ruta[i + 1]

        # Distancia plana aproximada en metros
        dx = (p2[1] - p1[1]) * math.cos(math.radians(p1[0])) * 111320.0
        dy = (p2[0] - p1[0]) * 110540.0
        dist = math.hypot(dx, dy)

        if dist < 0.001:
            continue

        cant_pasos = max(1, int(dist / paso_metros))
        for j in range(1, cant_pasos + 1):
            fraccion = j / cant_pasos
            lat = p1[0] + (p2[0] - p1[0]) * fraccion
            lon = p1[1] + (p2[1] - p1[1]) * fraccion
            pasos.append((round(lat, 6), round(lon, 6)))

    return pasos


def calcular_rumbo(coord_ant, coord_act):
    """
    Calcula el rumbo o azimut en grados (0° a 360°) entre la posición anterior y la actual.
    0° = Norte, 90° = Este, 180° = Sur, 270° = Oeste.
    """
    lat1, lon1 = math.radians(coord_ant[0]), math.radians(coord_ant[1])
    lat2, lon2 = math.radians(coord_act[0]), math.radians(coord_act[1])
    dlon = lon2 - lon1

    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    rumbo_rad = math.atan2(y, x)
    rumbo_deg = math.degrees(rumbo_rad)

    return round((rumbo_deg + 360.0) % 360.0, 1)


class Unidad(MQTTPub):
    """
    Móvil de emergencia que patrulla o permanece en guardia fija según su tabla JSON.
    Hereda de MQTTPub para la gestión de conexión MQTT v5, autenticación y presencia LWT.
    """

    def __init__(
        self,
        id_unidad="amb-01",
        tipo=None,
        ip_broker=IP_BROKER_DEFAULT,
        puerto=PUERTO_BROKER_DEFAULT,
        usuario=None,
        password=PASSWORD_DEFAULT,
    ):
        self.id_unidad = id_unidad
        self.ip_broker = ip_broker
        self.puerto = puerto

        # 1. Cargar coordenadas desde el JSON con if/else
        coords, self.tipo = cargar_coordenadas(id_unidad, tipo)

        # Usuario MQTT por defecto coincide con el ID de la unidad
        usuario_mqtt = usuario if usuario else id_unidad

        # 2. Inicializar la clase padre MQTTPub
        super().__init__(
            ip_broker=self.ip_broker,
            port_broker=self.puerto,
            client_id=self.id_unidad,
            usuario=usuario_mqtt,
            password=password,
            sesion_persistente=False,
        )

        # 3. Tópicos
        self.topic_posicion = f"reportes/{self.tipo}/{self.id_unidad}/posicion"
        self.topic_presencia = f"reportes/{self.tipo}/{self.id_unidad}/presencia"

        # 4. Configurar Last Will and Testament (LWT) antes de conectar
        self.configurar_last_will(
            topic=self.topic_presencia,
            payload={"id_unidad": self.id_unidad, "estado": "offline"},
            qos=1,
            retain=True,
        )

        # 5. Distinguir si es móvil con patrullaje o base fija
        if len(coords) > 1:
            # Patrullero con circuito vial: aplica odómetro para moverse a 36 km/h
            self.ruta_pasos = odometro_simple(coords, paso_metros=VELOCIDAD_PATRULLA_MS)
            self.es_estatico = False
            print(f"[{self.id_unidad}] Patrulla en movimiento: {len(self.ruta_pasos)} segundos ({round(len(self.ruta_pasos)/60, 1)} min) de circuito continuo.")
        else:
            # Ambulancia o Bombero en hospital o cuartel: posición fija estacionada
            self.ruta_pasos = coords
            self.es_estatico = True
            print(f"[{self.id_unidad}] En guardia estacionado en base fija: {coords[0]}")

        self.indice = 0
        self.pos_anterior = self.ruta_pasos[0]

    def siguiente_paso(self):
        """
        Retorna la telemetría del segundo actual:
        - Si es estático (base): emite velocidad 0 y rumbo 0 en sus coordenadas fijas.
        - Si es patrulla: avanza 1 paso en la lista y calcula el rumbo del móvil.
        """
        if self.es_estatico:
            pos_actual = self.ruta_pasos[0]
            return {
                "v": 1,
                "ts": int(time.time()),
                "lat": pos_actual[0],
                "lon": pos_actual[1],
                "rumbo": 0.0,
                "velocidad_kmh": 0.0,
            }

        # Móvil en patrullaje continuo
        pos_actual = self.ruta_pasos[self.indice]
        self.indice = (self.indice + 1) % len(self.ruta_pasos)

        rumbo = calcular_rumbo(self.pos_anterior, pos_actual) if pos_actual != self.pos_anterior else 0.0
        self.pos_anterior = pos_actual

        return {
            "v": 1,
            "ts": int(time.time()),
            "lat": pos_actual[0],
            "lon": pos_actual[1],
            "rumbo": rumbo,
            "velocidad_kmh": round(VELOCIDAD_PATRULLA_MS * 3.6, 1),
        }

    def iniciar(self):
        """
        Conecta a MQTT, publica presencia online e inicia la emisión periódica cada 1 segundo.
        """
        try:
            self.start()
        except OSError:
            print(f"[{self.id_unidad}] Error: No se pudo conectar al broker MQTT en {self.ip_broker}:{self.puerto}")
            return

        # Publicar presencia online inmediata (QoS 1, Retained)
        self.publish(
            {"id_unidad": self.id_unidad, "estado": "online"},
            self.topic_presencia,
            qos=1,
            retain=True,
        )
        print(f"[{self.id_unidad}] Conectado a MQTT ({self.ip_broker}:{self.puerto}). Transmitiendo...")

        try:
            while True:
                inicio_ciclo = time.time()

                telemetria = self.siguiente_paso()

                # Publicar telemetría oficial (QoS 0)
                self.publish(telemetria, self.topic_posicion, qos=0, retain=False)

                print(
                    f"[{self.id_unidad}] Lat={telemetria['lat']:<10} | "
                    f"Lon={telemetria['lon']:<10} | Rumbo={telemetria['rumbo']:>5.1f}° | "
                    f"Vel={telemetria['velocidad_kmh']} km/h"
                )

                tiempo_usado = time.time() - inicio_ciclo
                time.sleep(max(0.0, INTERVALO_REPORTE_SEG - tiempo_usado))

        except KeyboardInterrupt:
            print(f"\n[{self.id_unidad}] Detención solicitada por usuario...")
            self.detener()

    def detener(self):
        """
        Avisa desconexión limpia ('offline') y detiene el cliente MQTT.
        """
        self.publish(
            {"id_unidad": self.id_unidad, "estado": "offline"},
            self.topic_presencia,
            qos=1,
            retain=True,
        )
        self.stop()
        print(f"[{self.id_unidad}] Desconectado y detenido.")


# ==============================================================================
# ENTRADA DE LÍNEA DE COMANDOS
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulador de Unidad Móvil de Emergencia (Comodoro Rivadavia)")
    # Permite pasar el ID por parámetro posicional ('python unidad.py pol-01') o con '--id pol-01'
    parser.add_argument("pos_id", nargs="?", default=None, help="ID por posición (ej: amb-01, pol-02, bom-03)")
    parser.add_argument("--id", type=str, default=None, help="ID de la unidad (ej: amb-01, pol-02, bom-03)")
    parser.add_argument("--tipo", type=str, default=None, choices=["ambulancia", "policia", "bomberos"], help="Tipo opcional (se deduce del ID si no se pasa)")
    parser.add_argument("--broker", type=str, default=IP_BROKER_DEFAULT, help="IP del broker MQTT")
    parser.add_argument("--puerto", type=int, default=PUERTO_BROKER_DEFAULT, help="Puerto del broker MQTT")
    parser.add_argument("--usuario", type=str, default=None, help="Usuario MQTT")
    parser.add_argument("--password", type=str, default=PASSWORD_DEFAULT, help="Contraseña MQTT")

    args = parser.parse_args()

    # Resolver ID final: si se pasó --id tiene prioridad, sino el posicional, sino 'amb-01' por defecto
    id_final = args.id if args.id else (args.pos_id if args.pos_id else "amb-01")

    movil = Unidad(
        id_unidad=id_final,
        tipo=args.tipo,
        ip_broker=args.broker,
        puerto=args.puerto,
        usuario=args.usuario,
        password=args.password,
    )

    movil.iniciar()
