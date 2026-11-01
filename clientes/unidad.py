"""
Este módulo implementa el Cliente Móvil (ambulancia, patrullero o bomberos)
que carga sus datos y coordenadas directamente desde las tablas JSON en 'clientes/rutas/'
(ambulancias.json, bomberos.json, policias.json) según el ID elegido.

Hereda de MQTTPub (comun/publisher.py), reutilizando su gestión
de cliente MQTT v5, autenticación, Last Will y publicaciones.
Utiliza comun.topics y comun.payloads para cumplir con el contrato oficial del proyecto.

EJEMPLOS DE EJECUCIÓN:
  python clientes/unidad.py --id amb-01
  python clientes/unidad.py --id pol-01
  python clientes/unidad.py --id bom-02
  python clientes/unidad.py pol-03
  python clientes/unidad.py pol-01 --invalidas 10   (cada 10 reportes manda una posición inválida, para la demo)

La clave de cada unidad se lee de clientes/credenciales.json (ver credenciales.example.json).
Host, puerto y CA se pueden pasar por argumento o por las variables MQTT_HOST, MQTT_PORT y MQTT_CA_CERTS.

Dentro de flota-demo.py estan simuladas las unidades del 1..5 (Ambulancias, Bomberos, Policias)

"""

import sys
import json
import math
import time
import argparse
from pathlib import Path

# Asegurar que la raíz del proyecto esté en el path para importar 'comun'
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from comun.publisher import MQTTPub, config_entorno
from comun import topics, payloads
from credenciales import clave

CA_CERTS_DEFAULT = str(ROOT_DIR / "broker" / "certs" / "ca.crt")

VELOCIDAD_PATRULLA_MS = 10.0   # 10 metros/segundo = 36 km/h para patrulleros
INTERVALO_REPORTE_SEG = 1.0    # 1 publicación cada 1 segundo

# Keepalive corto: si se corta la red (sin cerrar el socket), el broker publica el Last Will
# a los ~1.5 keepalive. Con 60 s tardaba hasta 90 s en enterarse; con 15 s, unos 22 s
KEEPALIVE_SEG = 15

# Modo inválido (--invalidas): posiciones que el servicio tiene que descartar (#9, #19)
PUNTO_EN_EL_MAR = (-45.8700, -67.3000)   # Golfo San Jorge, ~13 km al este de la costa
SALTO_IMPOSIBLE_GRADOS = 0.01            # ~1.1 km de latitud en un solo reporte

PREFIJOS = {"amb": "ambulancia", "pol": "policia", "bom": "bomberos"}
ARCHIVOS_RUTAS = {"ambulancia": "ambulancias.json", "policia": "policias.json", "bomberos": "bomberos.json"}

def cargar_coordenadas(id_unidad, tipo=None):
    """
    Carga las coordenadas desde el JSON correspondiente usando el ID de la unidad.
    """
    if not tipo:
        tipo = PREFIJOS.get(id_unidad.split("-")[0])
        if not tipo:
            raise SystemExit(f"No se puede deducir el tipo de '{id_unidad}': usar amb-, pol- o bom-, o pasar --tipo")

    archivo = Path(__file__).resolve().parent / "rutas" / ARCHIVOS_RUTAS[tipo]
    with open(archivo, "r", encoding="utf-8") as f:
        datos = json.load(f)

    if id_unidad not in datos:
        raise SystemExit(f"'{id_unidad}' no tiene ruta en rutas/{archivo.name}")
    return datos[id_unidad]["coordenadas"], tipo

def odometro_simple(ruta, paso_metros=VELOCIDAD_PATRULLA_MS):
    """
    Odómetro virtual: Convierte las esquinas de la calle en pasos regulares de 'paso_metros' (10m = 36 km/h).
    Cada elemento resultante equivale exactamente a 1 segundo de viaje para el MQTT
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

    # el % final evita que 359.96 redondee a 360.0, que el contrato no acepta (0 <= rumbo < 360)
    return round((rumbo_deg + 360.0) % 360.0, 1) % 360.0


class Unidad(MQTTPub):
    """
    Móvil de emergencia que patrulla o permanece en guardia fija según su tabla JSON.
    Hereda de MQTTPub para la gestión de conexión MQTT v5, autenticación y presencia LWT.
    """

    def __init__(
        self,
        id_unidad="amb-01",
        tipo=None,
        ip_broker="127.0.0.1",
        puerto=8883,
        usuario=None,
        password=None,
        ca_certs=CA_CERTS_DEFAULT,
        invalidas=0,
        silencioso=False,
    ):
        self.id_unidad = id_unidad
        self.ip_broker = ip_broker
        self.puerto = puerto
        self.invalidas = invalidas      # cada cuántos reportes manda uno inválido (0 = nunca)
        self.silencioso = silencioso    # no imprimir cada reporte (flota-demo lanza 15 unidades en la misma consola)
        self.reportes = 0
        usuario_mqtt = usuario if usuario else id_unidad

        # Cargar coordenadas y deducir tipo oficial
        coords, self.tipo = cargar_coordenadas(id_unidad, tipo)

        self.topic_posicion = topics.posicion(self.tipo, self.id_unidad)
        self.topic_presencia = topics.presencia(self.tipo, self.id_unidad)

        # Sin TLS solo si se pide explícitamente (ca_certs=None, --no-tls): si falta el certificado
        # se corta acá, en vez de conectarse en texto plano sin avisar
        ca_file = None
        if ca_certs:
            if not Path(ca_certs).exists():
                raise SystemExit(f"[{id_unidad}] No existe el certificado de la CA: {ca_certs} (para probar sin TLS: --no-tls)")
            ca_file = str(ca_certs)

        # Inicializar la clase padre MQTTPub (sin sesión persistente para unidades)
        super().__init__(
            ip_broker=self.ip_broker,
            port_broker=self.puerto,
            client_id=self.id_unidad,
            usuario=usuario_mqtt,
            password=password if password else clave(usuario_mqtt),
            sesion_persistente=False,
            ca_certs=ca_file,
            keepalive=KEEPALIVE_SEG,
        )

        # Configurar Last Will and Testament (LWT) antes de conectar
        self.configurar_last_will(
            topic=self.topic_presencia,
            payload=payloads.presencia("offline"),
            qos=1,
            retain=True,
        )

        # Distinguir si es móvil con patrullaje o base fija
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
        Retorna la telemetría validado según comun.payloads.POSICION:
        - Si es estático (base): emite rumbo 0.0 en sus coordenadas fijas.
        - Si es patrulla: avanza 1 paso en la lista y calcula el rumbo del móvil.
        """
        if self.es_estatico:
            pos_actual = self.ruta_pasos[0]
            return payloads.posicion(
                lat=pos_actual[0],
                lon=pos_actual[1],
                rumbo=0.0,
            )

        # Móvil en patrullaje continuo
        pos_actual = self.ruta_pasos[self.indice]
        self.indice = (self.indice + 1) % len(self.ruta_pasos)

        rumbo = calcular_rumbo(self.pos_anterior, pos_actual) if pos_actual != self.pos_anterior else 0.0
        self.pos_anterior = pos_actual

        return payloads.posicion(
            lat=pos_actual[0],
            lon=pos_actual[1],
            rumbo=rumbo,
        )

    def posicion_invalida(self):
        """
        Posición que el servicio tiene que descartar, alternando entre las dos reglas de validación:
        - fuera de la zona: un punto en el mar
        - salto imposible: ~1.1 km desde la última posición en un solo reporte (el límite es 75 m/s)
        El recorrido no avanza, así que el reporte siguiente vuelve a ser válido.
        """
        if (self.reportes // self.invalidas) % 2:
            lat, lon = PUNTO_EN_EL_MAR
            motivo = "fuera de la zona"
        else:
            lat, lon = self.pos_anterior[0] + SALTO_IMPOSIBLE_GRADOS, self.pos_anterior[1]
            motivo = "salto imposible"
        return payloads.posicion(lat=lat, lon=lon), motivo

    def iniciar(self):
        """
        Conecta a MQTT, publica presencia online e inicia la emisión periódica cada 1 segundo.
        """
        # Presencia online (QoS 1, Retained) en cada conexión, no solo en la primera:
        # si la unidad se cae y reconecta, pisa el Last Will "offline" que publicó el broker
        self.publicar_al_conectar(
            payloads.presencia("online"),
            self.topic_presencia,
            qos=1,
            retain=True,
        )

        # start() no se bloquea: si el broker todavía no responde, reintenta solo
        self.start()
        print(f"[{self.id_unidad}] Conectando a MQTT ({self.ip_broker}:{self.puerto}). Transmitiendo...")

        try:
            while True:
                inicio_ciclo = time.time()
                self.reportes += 1

                if self.invalidas and self.reportes % self.invalidas == 0:
                    telemetria, motivo = self.posicion_invalida()
                    print(f"[{self.id_unidad}] Posición inválida enviada ({motivo}): {telemetria['lat']}, {telemetria['lon']}")
                else:
                    telemetria = self.siguiente_paso()

                # Publicar telemetría oficial (QoS 0) ============================
                # Agregado del QOS=0 y parametros importantes
                self.publish(telemetria, self.topic_posicion, qos=0, retain=False)

                if not self.silencioso:
                    rumbo_str = f"{telemetria['rumbo']:.1f}°" if telemetria.get("rumbo") is not None else "N/A"
                    print(
                        f"[{self.id_unidad}] Lat={telemetria['lat']:<10} | "
                        f"Lon={telemetria['lon']:<10} | Rumbo={rumbo_str:>6} | "
                        f"Topic={self.topic_posicion}"
                    )

                tiempo_usado = time.time() - inicio_ciclo
                time.sleep(max(0.0, INTERVALO_REPORTE_SEG - tiempo_usado))

        except KeyboardInterrupt:
            print(f"\n[{self.id_unidad}] Detención solicitada por usuario...")
            self.detener()

    def detener(self):
        """
        Desconexión limpia que deja la unidad 'offline'.
        En una desconexión limpia el broker no publica el Last Will, salvo que se le pida con el
        reason code 0x04: así publica el mismo 'offline' que en una caída, sin carrera entre
        publicar a mano y desconectar.
        """
        self.stop(con_last_will=True)
        print(f"[{self.id_unidad}] Desconectado y detenido.")

if __name__ == "__main__":
    # Valores por defecto de las variables MQTT_HOST, MQTT_PORT y MQTT_CA_CERTS (si no están, localhost:8883)
    entorno = config_entorno()

    parser = argparse.ArgumentParser(description="Simulador de Unidad Móvil de Emergencia (Comodoro Rivadavia)")
    parser.add_argument("pos_id", nargs="?", default=None, help="ID por posición (ej: amb-01, pol-02, bom-03)")
    parser.add_argument("--id", type=str, default=None, help="ID de la unidad (ej: amb-01, pol-02, bom-03)")
    parser.add_argument("--tipo", type=str, default=None, choices=["ambulancia", "policia", "bomberos"], help="Tipo opcional (se deduce del ID si no se pasa)")
    parser.add_argument("--broker", type=str, default=entorno["ip_broker"], help="IP del broker MQTT")
    parser.add_argument("--puerto", type=int, default=None, help="Puerto del broker MQTT (8883 para TLS, 1883 estándar)")
    parser.add_argument("--usuario", type=str, default=None, help="Usuario MQTT (por defecto, el ID)")
    parser.add_argument("--password", type=str, default=None, help="Contraseña MQTT (por defecto, la de clientes/credenciales.json)")
    parser.add_argument("--ca-certs", type=str, default=entorno["ca_certs"] or CA_CERTS_DEFAULT, help="Ruta al archivo CA para TLS")
    parser.add_argument("--no-tls", action="store_true", help="Desactivar TLS y conectar en texto plano (puerto 1883)")
    parser.add_argument("--invalidas", type=int, default=0, metavar="N", help="Cada N reportes manda una posición inválida (demo de validación)")
    parser.add_argument("--silencioso", action="store_true", help="No imprimir cada reporte")

    args = parser.parse_args()
    id_final = args.id if args.id else (args.pos_id if args.pos_id else "amb-01")

    if args.no_tls:
        puerto_final = args.puerto if args.puerto is not None else 1883
        ca_certs_final = None
    else:
        puerto_final = args.puerto if args.puerto is not None else entorno["port_broker"]
        ca_certs_final = args.ca_certs

    movil = Unidad(
        id_unidad=id_final,
        tipo=args.tipo,
        ip_broker=args.broker,
        puerto=puerto_final,
        usuario=args.usuario,
        password=args.password,
        ca_certs=ca_certs_final,
        invalidas=args.invalidas,
        silencioso=args.silencioso,
    )

    movil.iniciar()
