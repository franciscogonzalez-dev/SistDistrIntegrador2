"""
Despachador Básico de Emergencias (Comodoro Rivadavia)
Proyecto 2 - Sistemas Distribuidos (UNPSJB)

Este módulo es la interfaz visual del despachador:
1. Abre una ventana con mapa interactivo (ArcGIS World Street Map) centrada en Comodoro Rivadavia.
2. Se conecta al broker MQTT como desp-01 (sesión persistente de 1 h) y se suscribe a:
   - 'flota/#': estado oficial de cada unidad (publicado por el servicio tras validar).
     QoS 0 para no saturar la cola persistente; retained garantiza recibir la foto completa al conectar.
     El despachador nunca lee 'reportes/' (posiciones sin validar, la ACL lo prohíbe).
   - 'servicio/estado': estado de vida del servicio central (online / offline via Last Will con retained).
3. Cada móvil se representa con:
   - Ícono según el tipo de fuerza (ambulancia 🚑, policía 🚓, bomberos 🚒).
   - Color del marcador según el estado (verde: libre, rojo: ocupada, gris: sin conexión).
   - Rumbo de navegación con flecha cardinal orientada y valor angular.
4. Muestra un indicador prominente en la barra superior ante 'servicio caído'.
5. Comunicación segura entre el hilo de red paho-mqtt y la interfaz Tkinter mediante queue.Queue.

La clave se lee de clientes/credenciales.json (ver credenciales.example.json).
"""

import sys
import queue
import argparse
from pathlib import Path
import tkinter as tk
import tkintermapview

# Asegurar que la raíz del proyecto y 'clientes' estén en el path para importar 'comun' y 'credenciales'
ROOT_DIR = Path(__file__).resolve().parent.parent
CLIENTES_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(CLIENTES_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTES_DIR))

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

# Colores según el estado operativo de la unidad: (color_circulo, color_borde)
COLORES_ESTADO = {
    "libre": ("#2ecc71", "#27ae60"),          # Verde: disponible para despachar
    "ocupada": ("#e74c3c", "#c0392b"),        # Rojo: asignada a una emergencia
    "sin_conexion": ("#95a5a6", "#7f8c8d"),   # Gris: desconectada (LWT offline)
}

# 8 cuadrantes cardinales para orientar la flecha de rumbo según el azimut
FLECHAS_RUMBO = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]


def formato_rumbo(rumbo):
    """Convierte el rumbo en grados (0° - 359.9°) en una flecha cardinal orientada con grados."""
    if rumbo is None:
        return ""
    idx = int((rumbo + 22.5) % 360 // 45)
    return f"{FLECHAS_RUMBO[idx]} {int(round(rumbo)) % 360}°"


class DespachadorBasico:
    """
    Ventana interactiva de monitoreo en tiempo real de la flota con soporte TLS e indicador de servicio.
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
        # Diccionario de estado local de la flota para el contador: { "amb-01": {"tipo": ..., "estado": ...} }
        self.flota_info = {}

        # Estado del servicio central: 'desconocido', 'online' u 'offline'
        self.estado_servicio = "desconocido"

        # Los callbacks de paho corren en otro hilo y tkinter solo se puede tocar desde el hilo
        # de la ventana: paho deja los mensajes en esta cola y la ventana los lee con root.after()
        self.mensajes = queue.Queue()

        # 1. Configurar la ventana principal de Tkinter
        self.root = tk.Tk()
        self.root.title("Despachador Básico - Monitoreo de Flota Oficial (Comodoro Rivadavia)")
        self.root.geometry("1100x750")
        self.root.minsize(800, 500)

        # 2. Barra superior: Título, resumen de flota e indicador de estado del servicio
        self.barra_superior = tk.Frame(self.root, bg="#1e272e", padx=12, pady=8)
        self.barra_superior.pack(side="top", fill="x")

        self.lbl_titulo = tk.Label(
            self.barra_superior,
            text="🚨 Despachador Oficial - Comodoro Rivadavia",
            bg="#1e272e",
            fg="#f5f6fa",
            font=("Helvetica", 11, "bold"),
        )
        self.lbl_titulo.pack(side="left")

        # Indicador de "servicio caído" leyendo servicio/estado
        self.lbl_servicio = tk.Label(
            self.barra_superior,
            text="🟡 Conectando con Servicio Central...",
            bg="#f39c12",
            fg="#ffffff",
            font=("Helvetica", 10, "bold"),
            padx=12,
            pady=4,
            relief="groove",
        )
        self.lbl_servicio.pack(side="right")

        # Resumen en vivo de la flota
        self.lbl_resumen_flota = tk.Label(
            self.barra_superior,
            text="Flota: 0 unidades",
            bg="#1e272e",
            fg="#dcdde1",
            font=("Helvetica", 9),
            padx=15,
        )
        self.lbl_resumen_flota.pack(side="right")

        # 3. Crear el mapa interactivo
        self.mapa = tkintermapview.TkinterMapView(self.root, width=1100, height=700)
        self.mapa.pack(fill="both", expand=True)
        self.mapa.set_tile_server(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
            max_zoom=20,
        )
        self.mapa.set_position(-45.8642, -67.4850)  # Centro de Comodoro Rivadavia
        self.mapa.set_zoom(14)

        # 4. Cliente MQTT. Sin TLS solo si se pide explícitamente (--no-tls)
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
        # de la sesión persistente mientras la app está cerrada. Al ser retained, al reconectar llega la foto actual
        self.cliente.subscribe(topics.SUB_FLOTA, callback=self._on_flota, qos=0)

        # servicio/estado con QoS 1 (retained): indica si el servicio central está operativo o caído (Last Will)
        self.cliente.subscribe(topics.SERVICIO_ESTADO, callback=self._on_servicio_estado, qos=1)

        print(f"[*] Conectando despachador a MQTT ({ip_broker}:{puerto}) [ID={client_id}]...")
        self.cliente.start()

        # Al cerrar la ventana, detener cliente MQTT
        self.root.protocol("WM_DELETE_WINDOW", self.cerrar)
        self.root.after(100, self._procesar_mensajes)

    def _on_flota(self, client, userdata, msg):
        """Corre en el hilo de paho: valida el estado de la unidad y lo encola para la ventana."""
        try:
            tipo, id_unidad = topics.parse_flota_estado(msg.topic)
            estado = payloads.validar(payloads.FLOTA_ESTADO, msg.payload)
        except ValueError as e:
            print(f"[Aviso] Mensaje descartado en {msg.topic}: {e}")
            return
        self.mensajes.put(("flota", tipo, id_unidad, estado))

    def _on_servicio_estado(self, client, userdata, msg):
        """Corre en el hilo de paho: valida el estado de vida del servicio central (online / offline)."""
        try:
            datos = payloads.validar(payloads.SERVICIO_ESTADO, msg.payload)
        except ValueError as e:
            print(f"[Aviso] Mensaje descartado en {msg.topic}: {e}")
            return
        self.mensajes.put(("servicio", datos["estado"]))

    def _procesar_mensajes(self):
        """Corre en el hilo de la ventana: aplica al mapa y a la UI todo lo que llegó por la cola."""
        while True:
            try:
                mensaje = self.mensajes.get_nowait()
            except queue.Empty:
                break

            tipo_msg = mensaje[0]
            if tipo_msg == "flota":
                _, tipo, id_unidad, estado = mensaje
                self.actualizar_unidad(id_unidad, tipo, estado)
            elif tipo_msg == "servicio":
                _, estado_servicio = mensaje
                self.actualizar_estado_servicio(estado_servicio)

        self.root.after(100, self._procesar_mensajes)

    def actualizar_estado_servicio(self, estado):
        """Actualiza el indicador visual de 'servicio/estado' en la barra superior."""
        self.estado_servicio = estado
        if estado == "online":
            self.lbl_servicio.config(
                text="🟢 Servicio Central: Online",
                bg="#27ae60",
                fg="#ffffff",
            )
        elif estado == "offline":
            self.lbl_servicio.config(
                text="🔴 SERVICIO CENTRAL CAÍDO",
                bg="#c0392b",
                fg="#ffffff",
            )
        else:
            self.lbl_servicio.config(
                text=f"🟡 Servicio Central: {estado}",
                bg="#f39c12",
                fg="#ffffff",
            )

    def _actualizar_color_marcador(self, marcador, color_circulo, color_borde):
        """Actualiza los colores de un marcador existente en el mapa de forma dinámica."""
        marcador.marker_color_circle = color_circulo
        marcador.marker_color_outside = color_borde
        if marcador.polygon:
            self.mapa.canvas.itemconfig(marcador.polygon, fill=color_borde, outline=color_borde)
        if marcador.big_circle:
            self.mapa.canvas.itemconfig(marcador.big_circle, fill=color_circulo, outline=color_borde)

    def _actualizar_resumen_flota(self):
        """Actualiza el contador de unidades según sus estados operativos en la barra superior."""
        total = len(self.flota_info)
        libres = sum(1 for info in self.flota_info.values() if info["estado"] == "libre")
        ocupadas = sum(1 for info in self.flota_info.values() if info["estado"] == "ocupada")
        desconectadas = sum(1 for info in self.flota_info.values() if info["estado"] == "sin_conexion")
        self.lbl_resumen_flota.config(
            text=f"Flota: {total} | 🟢 {libres} libres | 🔴 {ocupadas} ocupadas | ⚫ {desconectadas} sin conexión"
        )

    def actualizar_unidad(self, id_unidad, tipo, estado):
        """
        Crea o actualiza el marcador de la unidad:
        - Color según estado: verde (libre), rojo (ocupada), gris (sin conexión).
        - Ícono según tipo: 🚑 ambulancia, 🚓 policía, 🚒 bomberos.
        - Orientación con rumbo: flecha cardinal orientada y grados.
        """
        conexion = estado.get("conexion", "offline")
        disponibilidad = estado.get("disponibilidad", "libre")
        lat = estado.get("lat")
        lon = estado.get("lon")
        rumbo = estado.get("rumbo")
        eid = estado.get("eid")

        # 1. Determinar estado operativo y color
        if conexion == "offline":
            estado_clave = "sin_conexion"
            estado_desc = "sin conexión"
        elif disponibilidad == "ocupada":
            estado_clave = "ocupada"
            estado_desc = f"ocupada ({eid})" if eid else "ocupada"
        else:
            estado_clave = "libre"
            estado_desc = "libre"

        color_circulo, color_borde = COLORES_ESTADO[estado_clave]
        icono = ICONOS.get(tipo, "🚗")

        # 2. Formatear la etiqueta con tipo, ID, estado y orientación de rumbo
        rumbo_str = formato_rumbo(rumbo) if conexion != "offline" else ""
        partes_etiqueta = [f"{icono} {id_unidad}", f"[{estado_desc}]"]
        if rumbo_str:
            partes_etiqueta.append(rumbo_str)
        etiqueta = " ".join(partes_etiqueta)

        # 3. Actualizar o crear marcador en el mapa
        if id_unidad in self.marcadores:
            marcador = self.marcadores[id_unidad]
            if lat is not None and lon is not None:
                marcador.set_position(lat, lon)
            marcador.set_text(etiqueta)
            self._actualizar_color_marcador(marcador, color_circulo, color_borde)
        else:
            if lat is not None and lon is not None:
                print(f"[Nuevo Móvil] Registrado en mapa: {id_unidad} ({tipo}) en ({lat}, {lon}) [{estado_desc}]")
                nuevo_marcador = self.mapa.set_marker(
                    lat,
                    lon,
                    text=etiqueta,
                    text_color="#1a1a1a",
                    font=("Helvetica", 9, "bold"),
                    marker_color_circle=color_circulo,
                    marker_color_outside=color_borde,
                )
                self.marcadores[id_unidad] = nuevo_marcador

        # 4. Actualizar estado local y métricas
        self.flota_info[id_unidad] = {"tipo": tipo, "estado": estado_clave}
        self._actualizar_resumen_flota()

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
