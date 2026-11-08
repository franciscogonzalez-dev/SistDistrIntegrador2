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
import threading
import argparse
import requests
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
ARCHIVO_DESPACHOS = Path(__file__).resolve().parent / "despachos.txt"

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


def consultar_osrm_ruta(origen, destino, timeout=5):
    """
    Consulta la API pública de OSRM para obtener el trazado vial real entre dos puntos.
    origen:  (lat, lon)
    destino: (lat, lon)
    Retorna: lista de coordenadas [(lat, lon), ...] siguiendo las calles.
    Fallback: Si OSRM no responde o no hay internet, devuelve [origen, destino] en línea recta.
    """
    coords_str = f"{origen[1]},{origen[0]};{destino[1]},{destino[0]}"
    url = f"http://router.project-osrm.org/route/v1/driving/{coords_str}?overview=full&geometries=geojson"
    try:
        r = requests.get(url, headers={"User-Agent": "SistDistrComodoro/1.0"}, timeout=timeout)
        data = r.json()
        if data.get("code") == "Ok":
            coords = data["routes"][0]["geometry"]["coordinates"]
            return [(round(lat, 6), round(lon, 6)) for lon, lat in coords]
    except Exception as e:
        print(f"   [Aviso OSRM: {e}] Usando interpolación en línea recta.")
    return [origen, destino]


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

        # Lock para cambiar la ruta en caliente de forma segura entre hilos
        self._lock_ruta = threading.Lock()

        # Guardar la rutina original (base fija en hospital/cuartel o circuito de patrullaje)
        self.es_patrullero = len(coords) > 1
        self.pos_base = tuple(coords[0])
        if self.es_patrullero:
            self.ruta_original = odometro_simple(coords, paso_metros=VELOCIDAD_PATRULLA_MS)
            self.modo_actual = "patrulla"
            print(f"[{self.id_unidad}] Patrulla en movimiento: {len(self.ruta_original)} segundos ({round(len(self.ruta_original)/60, 1)} min) de circuito continuo.")
        else:
            self.ruta_original = [self.pos_base]
            self.modo_actual = "base"
            print(f"[{self.id_unidad}] En guardia estacionado en base fija: {self.pos_base}")

        self.ruta_activa = list(self.ruta_original)
        self.indice_activo = 0
        self.pos_anterior = self.ruta_activa[0]
        self.eid_actual = None
        self._id_solicitud = 0
        self._calculando_ruta = False

        # Seguimiento del archivo local despachos.txt (sin usar tópicos extras)
        self.mtime_despachos = 0
        if ARCHIVO_DESPACHOS.exists():
            try:
                self.mtime_despachos = ARCHIVO_DESPACHOS.stat().st_mtime
            except Exception:
                pass

    # ==============================================================================
    # NAVEGACIÓN MODULAR VIAL (OSRM + Odómetro)
    # ==============================================================================

    def cambiar_destino(self, destino, modo="hacia_emergencia", velocidad_ms=15.0, eid=None):
        """
        Método modular con hilos seguros y token de versión:
        - Actualiza modo_actual inmediatamente para que ninguna orden posterior se confunda.
        - Activa _calculando_ruta para que siguiente_paso no asuma prematuramente fin de ruta.
        - Incrementa _id_solicitud para invalidar cualquier hilo previo que esté esperando OSRM.
        - Consulta OSRM y aplica la ruta de forma atómica solo si la solicitud sigue vigente.
        """
        with self._lock_ruta:
            self._id_solicitud += 1
            mi_solicitud = self._id_solicitud
            self._calculando_ruta = True
            self.modo_actual = modo
            self.eid_actual = eid
            origen = self.pos_anterior

        def _hilo_calcular():
            print(f"[{self.id_unidad}] Cortando ruta previa. Trazando ruta OSRM: {origen} -> {destino} ({modo})...")
            try:
                calles = consultar_osrm_ruta(origen, destino)
                pasos = odometro_simple(calles, paso_metros=velocidad_ms)
            except Exception as e:
                print(f"[{self.id_unidad}] Error en OSRM: {e}. Usando línea directa.")
                pasos = odometro_simple([origen, destino], paso_metros=velocidad_ms)

            with self._lock_ruta:
                if mi_solicitud != self._id_solicitud:
                    print(f"[{self.id_unidad}] Solicitud OSRM obsoleta descartada.")
                    return
                self.ruta_activa = pasos
                self.indice_activo = 0
                self._calculando_ruta = False

            print(f"[{self.id_unidad}] Nueva ruta activa: {len(pasos)} segundos estimados hacia {destino} [{modo}].")

        threading.Thread(target=_hilo_calcular, daemon=True).start()

    def asignar_emergencia(self, lat_emergencia, lon_emergencia, eid):
        """Corta la ruta actual (estática o patrullaje) y despacha la unidad hacia la emergencia."""
        print(f"[{self.id_unidad}] ¡Emergencia {eid} asignada! Desplazándose hacia ({lat_emergencia}, {lon_emergencia}).")
        self.cambiar_destino(
            destino=(lat_emergencia, lon_emergencia),
            modo="hacia_emergencia",
            velocidad_ms=15.0,  # 54 km/h (< 75 m/s límite del validador)
            eid=eid,
        )

    def regresar_a_rutina(self):
        """
        Corta la emergencia y regresa a su rutina:
        - Si es ambulancia/bombero: calcula ruta de vuelta a su hospital/base.
        - Si es patrullero: calcula ruta de vuelta al inicio de su circuito de patrullaje.
        """
        if not self.es_patrullero:
            print(f"[{self.id_unidad}] Emergencia concluida. Retornando a base hospitalaria...")
            self.cambiar_destino(
                destino=self.pos_base,
                modo="regresando_a_base",
                velocidad_ms=10.0,
                eid=None,
            )
        else:
            print(f"[{self.id_unidad}] Emergencia concluida. Retornando al inicio de patrullaje...")
            self.cambiar_destino(
                destino=self.pos_base,
                modo="reenganche_patrulla",
                velocidad_ms=10.0,
                eid=None,
            )

    def verificar_despachos_archivo(self):
        """
        Monitorea el archivo clientes/despachos.txt.
        Si fue modificado, lee órdenes para esta unidad sin requerir tópicos MQTT:
          - '<id_unidad> <lat> <lon> [eid]' -> corta la base/patrulla y sale hacia allá vía OSRM.
          - '<id_unidad> volver'            -> regresa a su base o patrulla vía OSRM.
        """
        if not ARCHIVO_DESPACHOS.exists():
            return
        try:
            mtime = ARCHIVO_DESPACHOS.stat().st_mtime
            if mtime <= self.mtime_despachos:
                return
            self.mtime_despachos = mtime

            with open(ARCHIVO_DESPACHOS, "r", encoding="utf-8") as f:
                for linea in f:
                    partes = linea.strip().split()
                    if not partes or partes[0] != self.id_unidad:
                        continue

                    accion = partes[1].lower()
                    if accion in ("volver", "base", "liberar"):
                        if self.modo_actual in ("hacia_emergencia", "en_emergencia"):
                            self.regresar_a_rutina()
                    else:
                        try:
                            lat = float(partes[1])
                            lon = float(partes[2])
                            eid = partes[3] if len(partes) > 3 else "E-MANUAL"
                            if self.eid_actual != eid or self.modo_actual not in ("hacia_emergencia", "en_emergencia"):
                                self.asignar_emergencia(lat, lon, eid)
                        except (ValueError, IndexError):
                            pass
        except Exception as e:
            print(f"[{self.id_unidad}] Aviso leyendo {ARCHIVO_DESPACHOS.name}: {e}")

    def siguiente_paso(self):
        """
        Retorna la telemetría validada según el modo actual de la unidad:
        - Si está en base: emite rumbo 0.0 en sus coordenadas fijas.
        - Si llegó a la emergencia: permanece en el lugar asistiendo.
        - Si es patrulla continua: avanza 1 paso en el circuito vial.
        - Si está en trayecto OSRM (hacia emergencia o regresando):
          avanza paso a paso por las calles calculadas.
        """
        self.verificar_despachos_archivo()

        with self._lock_ruta:
            if self._calculando_ruta:
                # Mientras OSRM calcula el nuevo trazado, permanece reportando su posición actual
                return payloads.posicion(lat=self.pos_anterior[0], lon=self.pos_anterior[1], rumbo=0.0)

            if self.modo_actual == "base":
                pos_actual = self.pos_base
                self.pos_anterior = pos_actual
                return payloads.posicion(
                    lat=pos_actual[0],
                    lon=pos_actual[1],
                    rumbo=0.0,
                )

            elif self.modo_actual == "en_emergencia":
                pos_actual = self.pos_anterior
                return payloads.posicion(
                    lat=pos_actual[0],
                    lon=pos_actual[1],
                    rumbo=0.0,
                )

            elif self.modo_actual == "patrulla":
                pos_actual = self.ruta_activa[self.indice_activo]
                rumbo = calcular_rumbo(self.pos_anterior, pos_actual) if pos_actual != self.pos_anterior else 0.0
                self.pos_anterior = pos_actual
                self.indice_activo = (self.indice_activo + 1) % len(self.ruta_activa)
                return payloads.posicion(
                    lat=pos_actual[0],
                    lon=pos_actual[1],
                    rumbo=rumbo,
                )

            elif self.modo_actual in ("hacia_emergencia", "regresando_a_base", "reenganche_patrulla"):
                pos_actual = self.ruta_activa[self.indice_activo]
                rumbo = calcular_rumbo(self.pos_anterior, pos_actual) if pos_actual != self.pos_anterior else 0.0
                self.pos_anterior = pos_actual

                if self.indice_activo < len(self.ruta_activa) - 1:
                    self.indice_activo += 1
                else:
                    # Llegó al destino de la ruta OSRM
                    if self.modo_actual == "hacia_emergencia":
                        self.modo_actual = "en_emergencia"
                        print(f"[{self.id_unidad}] Arribó al lugar de la emergencia {self.eid_actual or ''}. Asistiendo en el lugar.")
                    elif self.modo_actual == "regresando_a_base":
                        self.modo_actual = "base"
                        self.ruta_activa = [self.pos_base]
                        self.indice_activo = 0
                        print(f"[{self.id_unidad}] Regreso completado a base hospitalaria. En guardia y disponible.")
                    elif self.modo_actual == "reenganche_patrulla":
                        self.modo_actual = "patrulla"
                        self.ruta_activa = list(self.ruta_original)
                        self.indice_activo = 0
                        print(f"[{self.id_unidad}] Llegó al inicio de circuito. Reanudando patrullaje continuo.")

                return payloads.posicion(
                    lat=pos_actual[0],
                    lon=pos_actual[1],
                    rumbo=rumbo,
                )

            return payloads.posicion(lat=self.pos_anterior[0], lon=self.pos_anterior[1], rumbo=0.0)

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
