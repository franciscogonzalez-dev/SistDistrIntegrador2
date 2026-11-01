"""
Despachador Básico de Emergencias (Comodoro Rivadavia)
Proyecto 2 - Sistemas Distribuidos (UNPSJB)

Este módulo es un visor visual mínimo:
1. Abre una ventana de mapa (ArcGIS World Street Map) centrada en Comodoro Rivadavia.
2. Se conecta al broker MQTT como desp-01 (sesión persistente de 1 h) y se suscribe a
   'flota/#': el estado oficial de cada unidad, que publica el servicio después de validar.
   El despachador nunca lee 'reportes/' (posiciones sin validar), y la ACL no se lo permite.
   Mientras no corre el servicio central, el mapa queda vacío.
3. Cada vez que llega un estado, crea o mueve el marcador de esa unidad en vivo.

La clave se lee de clientes/credenciales.json (ver credenciales.example.json).
"""

import sys
import queue
import argparse
from pathlib import Path
import tkinter as tk
import tkintermapview

# Asegurar que la raíz del proyecto esté en el path para importar 'comun'
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from comun.publisher import MQTTPub, config_entorno
from comun import topics, payloads
from credenciales import clave

CA_CERTS_DEFAULT = str(ROOT_DIR / "broker" / "certs" / "ca.crt")
USUARIO_DEFAULT = "desp-01"

# Sesión persistente de 1 h: guarda alertas y respuestas si se cierra la app (docs/topics.md)
EXPIRACION_SESION_SEG = 3600

# Íconos para distinguir cada fuerza en el mapa
ICONOS = {
    "ambulancia": "🚑",
    "policia": "🚓",
    "bomberos": "🚒",
}

class DespachadorBasico:
    """
    Ventana básica de monitoreo en tiempo real vía MQTT con soporte TLS.
    """

    def __init__(
        self,
        ip_broker="127.0.0.1",
        puerto=8883,
        client_id=USUARIO_DEFAULT,
        usuario=USUARIO_DEFAULT,
        password=None,
        ca_certs=CA_CERTS_DEFAULT,
    ):
        self.ip_broker = ip_broker
        self.puerto = puerto

        # Diccionario para guardar los marcadores activos en el mapa: { "amb-01": marker, ... }
        self.marcadores = {}

        # Los callbacks de paho corren en otro hilo y tkinter solo se puede tocar desde el hilo
        # de la ventana: paho deja los mensajes en esta cola y la ventana los lee con root.after()
        self.mensajes = queue.Queue()

        # 1. Configurar la ventana principal de Tkinter
        self.root = tk.Tk()
        self.root.title("Despachador Básico - Monitoreo de Flota (Comodoro Rivadavia)")
        self.root.geometry("1050x700")

        # 2. Crear el mapa interactivo
        self.mapa = tkintermapview.TkinterMapView(self.root, width=1050, height=700)
        self.mapa.pack(fill="both", expand=True)
        self.mapa.set_tile_server("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", max_zoom=20)
        self.mapa.set_position(-45.8642, -67.4850)  # Centro de Comodoro Rivadavia
        self.mapa.set_zoom(14)

        # 3. Cliente MQTT. Sin TLS solo si se pide explícitamente (--no-tls)
        ca_file = None
        if ca_certs:
            if not Path(ca_certs).exists():
                raise SystemExit(f"No existe el certificado de la CA: {ca_certs} (para probar sin TLS: --no-tls)")
            ca_file = str(ca_certs)

        self.cliente = MQTTPub(
            ip_broker,
            puerto,
            client_id,
            usuario=usuario,
            password=password if password else clave(usuario),
            ca_certs=ca_file,
            sesion_persistente=True,
            expiracion_sesion=EXPIRACION_SESION_SEG,
        )

        # flota/# con QoS 0: se publica con cada posición aceptada y con QoS 1 llenaría la cola
        # de la sesión mientras la app está cerrada. Al ser retained, al reconectar llega la foto actual
        self.cliente.subscribe(topics.SUB_FLOTA, callback=self._on_flota, qos=0)

        print(f"[*] Conectando despachador a MQTT ({ip_broker}:{puerto}) [ID={client_id}]...")
        self.cliente.start()

        # Al cerrar la ventana, detener cliente MQTT
        self.root.protocol("WM_DELETE_WINDOW", self.cerrar)
        self.root.after(100, self._procesar_mensajes)

    def _on_flota(self, client, userdata, msg):
        """Corre en el hilo de paho: valida el estado y lo deja en la cola para la ventana."""
        try:
            tipo, id_unidad = topics.parse_flota_estado(msg.topic)
            estado = payloads.validar(payloads.FLOTA_ESTADO, msg.payload)
        except ValueError as e:
            print(f"[Aviso] Mensaje descartado en {msg.topic}: {e}")
            return
        self.mensajes.put((tipo, id_unidad, estado))

    def _procesar_mensajes(self):
        """Corre en el hilo de la ventana: aplica al mapa todo lo que llegó desde la última vez."""
        while True:
            try:
                tipo, id_unidad, estado = self.mensajes.get_nowait()
            except queue.Empty:
                break
            self.actualizar_unidad(id_unidad, tipo, estado)
        self.root.after(100, self._procesar_mensajes)

    def actualizar_unidad(self, id_unidad, tipo, estado):
        """Crea o mueve el marcador de la unidad en la pantalla."""
        if "lat" not in estado or "lon" not in estado:
            return  # todavía no tiene una posición válida

        lat, lon = estado["lat"], estado["lon"]
        icono = ICONOS.get(tipo, "🚗")
        etiqueta = f"{icono} {id_unidad} ({estado['disponibilidad']}, {estado['conexion']})"

        if id_unidad in self.marcadores:
            # Si el marcador ya existe, solo movemos su posición y texto
            marcador = self.marcadores[id_unidad]
            marcador.set_position(lat, lon)
            marcador.set_text(etiqueta)
        else:
            # Si es la primera vez que aparece, creamos el marcador en el mapa
            print(f"[Nuevo Móvil] Registrado en mapa: {id_unidad} ({tipo}) en ({lat}, {lon})")
            nuevo_marcador = self.mapa.set_marker(lat, lon, text=etiqueta)
            self.marcadores[id_unidad] = nuevo_marcador

    def iniciar(self):
        """Inicia la ventana gráfica interactiva."""
        self.root.mainloop()

    def cerrar(self):
        """Cierre ordenado al cerrar la ventana."""
        print("[*] Cerrando despachador...")
        self.cliente.stop()
        self.root.destroy()


# ==============================================================================
# ENTRADA PRINCIPAL
# ==============================================================================

if __name__ == "__main__":
    # Valores por defecto de las variables MQTT_HOST, MQTT_PORT y MQTT_CA_CERTS (si no están, localhost:8883)
    entorno = config_entorno()

    parser = argparse.ArgumentParser(description="Despachador Básico de Emergencias (Comodoro Rivadavia)")
    parser.add_argument("pos_broker", nargs="?", default=None, help="IP o host del broker (posicional)")
    parser.add_argument("pos_puerto", nargs="?", type=int, default=None, help="Puerto del broker (posicional)")
    parser.add_argument("--broker", type=str, default=None, help="IP del broker MQTT")
    parser.add_argument("--puerto", type=int, default=None, help="Puerto del broker MQTT (8883 para TLS, 1883 estándar)")
    parser.add_argument("--id", type=str, default=USUARIO_DEFAULT, help="Client ID del despachador")
    parser.add_argument("--usuario", type=str, default=USUARIO_DEFAULT, help="Usuario MQTT")
    parser.add_argument("--password", type=str, default=None, help="Contraseña MQTT (por defecto, la de clientes/credenciales.json)")
    parser.add_argument("--ca-certs", type=str, default=entorno["ca_certs"] or CA_CERTS_DEFAULT, help="Ruta al archivo CA para TLS")
    parser.add_argument("--no-tls", action="store_true", help="Desactivar TLS y conectar en texto plano (puerto 1883)")

    args = parser.parse_args()

    broker = args.broker or args.pos_broker or entorno["ip_broker"]
    if args.no_tls:
        puerto = args.puerto or args.pos_puerto or 1883
        ca_certs = None
    else:
        puerto = args.puerto or args.pos_puerto or entorno["port_broker"]
        ca_certs = args.ca_certs

    app = DespachadorBasico(
        ip_broker=broker,
        puerto=puerto,
        client_id=args.id,
        usuario=args.usuario,
        password=args.password,
        ca_certs=ca_certs,
    )
    app.iniciar()
