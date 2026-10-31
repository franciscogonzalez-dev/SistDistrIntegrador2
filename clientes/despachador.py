"""
Despachador Básico de Emergencias (Comodoro Rivadavia)
Proyecto 2 - Sistemas Distribuidos (UNPSJB)

Este módulo es un visor visual mínimo:
1. Abre una ventana de mapa (Google Maps) centrada en Comodoro Rivadavia.
2. Se conecta al broker MQTT y se suscribe a los reportes de todas las unidades:
   - 'reportes/+/+/posicion' -> Telemetría de movimiento
   - 'reportes/+/+/presencia' -> Conexión online/offline (Last Will)
3. Cada vez que llega un mensaje, crea o mueve el marcador de esa unidad en vivo.
"""

import os
import sys
import json
import tkinter as tk
import tkintermapview
import paho.mqtt.client as mqtt
from paho.mqtt.enums import MQTTProtocolVersion

# Parámetros por defecto
IP_BROKER_DEFAULT = "127.0.0.1"
PUERTO_BROKER_DEFAULT = 1883

# Íconos para distinguir cada fuerza en el mapa
ICONOS = {
    "ambulancia": "🚑",
    "policia": "🚓",
    "bomberos": "🚒",
}

class DespachadorBasico:
    """
    Ventana básica de monitoreo en tiempo real vía MQTT.
    """

    def __init__(self, ip_broker=IP_BROKER_DEFAULT, puerto=PUERTO_BROKER_DEFAULT):
        self.ip_broker = ip_broker
        self.puerto = puerto

        # Diccionario para guardar los marcadores activos en el mapa: { "amb-01": marker, ... }
        self.marcadores = {}

        # 1. Configurar la ventana principal de Tkinter
        self.root = tk.Tk()
        self.root.title("Despachador Básico - Monitoreo de Flota (Comodoro Rivadavia)")
        self.root.geometry("1050x700")

        # 2. Crear el mapa interactivo con Google Maps (libre y rápido)
        self.mapa = tkintermapview.TkinterMapView(self.root, width=1050, height=700)
        self.mapa.pack(fill="both", expand=True)
        self.mapa.set_tile_server("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", max_zoom=20)
        self.mapa.set_position(-45.8642, -67.4850)  # Centro de Comodoro Rivadavia
        self.mapa.set_zoom(14)

        # 3. Configurar el cliente MQTT receptor
        self.cliente_mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="despachador_visor",
            protocol=MQTTProtocolVersion.MQTTv5,
        )

        self.cliente_mqtt.on_connect = self._on_connect
        self.cliente_mqtt.on_message = self._on_message

        # Conectar al broker e iniciar el hilo de red en segundo plano
        try:
            print(f"[*] Conectando despachador a MQTT ({self.ip_broker}:{self.puerto})...")
            self.cliente_mqtt.connect(self.ip_broker, self.puerto, keepalive=60)
            self.cliente_mqtt.loop_start()
        except Exception as e:
            print(f"[Error] No se pudo conectar al broker MQTT: {e}")
            print("        Asegúrese de que el broker esté corriendo.")

        # Al cerrar la ventana, detener cliente MQTT
        self.root.protocol("WM_DELETE_WINDOW", self.cerrar)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """Callback al conectarse: se suscribe a los topics de telemetría y presencia."""
        if reason_code.is_failure:
            print(f"[Error MQTT] Conexión rechazada: {reason_code}")
            return

        print(f"[OK] Despachador conectado a MQTT. Escuchando unidades en vivo...")

        # Suscribirse a la posición de todas las unidades (+ es wildcard de 1 nivel)
        # Topics acordados: reportes/{tipo}/{id_unidad}/posicion
        self.cliente_mqtt.subscribe("reportes/+/+/posicion", qos=0)

        # Suscribirse a la presencia (online / offline Last Will)
        self.cliente_mqtt.subscribe("reportes/+/+/presencia", qos=1)

    def _on_message(self, client, userdata, msg):
        """Callback cuando llega un mensaje desde cualquier móvil."""
        try:
            topic_partes = msg.topic.split("/")
            # Formato esperado: reportes/{tipo}/{id_unidad}/{canal}
            if len(topic_partes) < 4:
                return

            tipo = topic_partes[1]        # ambulancia, policia, bomberos
            id_unidad = topic_partes[2]   # amb-01, pol-01, etc.
            canal = topic_partes[3]       # posicion o presencia

            payload = json.loads(msg.payload.decode("utf-8"))

            if canal == "posicion":
                lat = payload.get("lat")
                lon = payload.get("lon")
                vel = payload.get("velocidad_kmh", 0.0)
                if lat is not None and lon is not None:
                    # Actualizar el mapa de forma segura en el hilo de la interfaz
                    self.root.after_idle(self.actualizar_posicion, id_unidad, tipo, lat, lon, vel)

            elif canal == "presencia":
                estado = payload.get("estado")
                print(f"[Presencia] {id_unidad} pasó a estado: {estado}")

        except Exception as e:
            print(f"[Error procesando mensaje]: {e}")

    def actualizar_posicion(self, id_unidad, tipo, lat, lon, velocidad):
        """Crea o mueve el marcador de la unidad en la pantalla."""
        icono = ICONOS.get(tipo, "🚗")
        etiqueta = f"{icono} {id_unidad} ({velocidad} km/h)"

        if id_unidad in self.marcadores:
            # Si el marcador ya existe, solo movemos su posición y texto
            marcador = self.marcadores[id_unidad]
            marcador.set_position(lat, lon)
            marcador.set_text(etiqueta)
        else:
            # Si es la primera vez que emite, creamos el marcador en el mapa
            print(f"[Nuevo Móvil] Registrado en mapa: {id_unidad} ({tipo}) en ({lat}, {lon})")
            nuevo_marcador = self.mapa.set_marker(lat, lon, text=etiqueta)
            self.marcadores[id_unidad] = nuevo_marcador

    def iniciar(self):
        """Inicia la ventana gráfica interactiva."""
        self.root.mainloop()

    def cerrar(self):
        """Cierre ordenado al cerrar la ventana."""
        print("[*] Cerrando despachador...")
        try:
            self.cliente_mqtt.loop_stop()
            self.cliente_mqtt.disconnect()
        except Exception:
            pass
        self.root.destroy()


# ==============================================================================
# ENTRADA PRINCIPAL
# ==============================================================================

if __name__ == "__main__":
    broker = sys.argv[1] if len(sys.argv) > 1 else IP_BROKER_DEFAULT
    puerto = int(sys.argv[2]) if len(sys.argv) > 2 else PUERTO_BROKER_DEFAULT

    app = DespachadorBasico(ip_broker=broker, puerto=puerto)
    app.iniciar()
