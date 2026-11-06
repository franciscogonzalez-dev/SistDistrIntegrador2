"""
Despachador Básico y Avanzado de Emergencias (Comodoro Rivadavia)
Proyecto 2 - Sistemas Distribuidos (UNPSJB)

Este módulo es la interfaz visual y operativa del despachador general (monitorea todas las fuerzas):
1. Abre una ventana con mapa interactivo (ArcGIS World Street Map) centrada en Comodoro Rivadavia
   y panel lateral de operaciones.
2. Se conecta al broker MQTT como desp-01 (sesión persistente de 1 h) y se suscribe a:
   - 'flota/#': estado oficial de cada unidad (QoS 0, retained).
   - 'servicio/estado': estado de vida del servicio central (online / offline via Last Will con retained, QoS 1).
   - 'emergencias/#': incidentes oficiales validados (QoS 1, retained; vacío al cerrar).
   - 'alertas/#': avisos del sistema, como caídas de unidades en servicio (QoS 1).
   - 'respuestas/desp-01': confirmaciones y rechazos de comandos enviados por este despachador (QoS 1).
3. Cada móvil y emergencia se representa en el mapa con marcadores interactivos:
   - Móviles: Ícono de fuerza (🚑, 🚓, 🚒), color (verde: libre, rojo: ocupada, gris: sin conexión) y rumbo.
   - Emergencias: Ícono de aviso (⚠️ sin asignar, 🚨 asignada) y código de incidente.
4. Funciones de despacho:
   - Asignar una unidad libre a una emergencia: publica en 'comandos/desp-01/{tipo}/{id}' con eid y
     un ID único idempotente (reutilizado si se reintenta la misma acción).
   - Marcar libre una unidad ocupada que finalizó: publica comando 'liberar', lo que cierra la emergencia.
   - Alerta visible ante caída de unidad ocupada con botón directo para reasignar su emergencia a otro móvil.
   - Notificación visual de comandos rechazados que llegan por 'respuestas/desp-01'.
5. Comunicación segura entre el hilo de red paho-mqtt y la interfaz Tkinter mediante queue.Queue.

La clave se lee de clientes/credenciales.json (ver credenciales.example.json).
"""

import sys
import time
import json
import queue
import argparse
from datetime import datetime
from collections import deque
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import tkintermapview

# Asegurar que la raíz del proyecto y 'clientes' estén en el path para importar 'comun' y 'credenciales'
ROOT_DIR = Path(__file__).resolve().parent.parent
CLIENTES_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(CLIENTES_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTES_DIR))

from comun.publisher import MQTTPub, config_entorno
from comun import topics, payloads, jwt_auth
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

# Colores según el estado de la emergencia: (color_circulo, color_borde)
COLORES_EMERGENCIA = {
    "sin_asignar": ("#e67e22", "#d35400"),    # Naranja / Ámbar: requiere despacho urgente
    "asignada": ("#2980b9", "#1f618d"),       # Azul: unidad en camino o atendiendo
}

# 8 cuadrantes cardinales para orientar la flecha de rumbo según el azimut
FLECHAS_RUMBO = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]


def formato_rumbo(rumbo):
    """Convierte el rumbo en grados (0° - 359.9°) en una flecha cardinal orientada con grados."""
    if rumbo is None:
        return ""
    idx = int((rumbo + 22.5) % 360 // 45)
    return f"{FLECHAS_RUMBO[idx]} {int(round(rumbo)) % 360}°"


class DialogoLogin(tk.Toplevel):
    """Ventana modal de autenticación para el operador de despacho con emisión de JWT."""

    def __init__(self, parent, usuario_default="desp-01"):
        super().__init__(parent)
        self.title("🔐 Acceso de Despachador - Autenticación JWT")
        self.geometry("450x340")
        self.resizable(False, False)
        self.configure(bg="#1e272e")
        self.resultado = None

        self.transient(parent)
        self.grab_set()

        # Título y encabezado
        lbl_head = tk.Label(
            self,
            text="🚨 Centro de Despacho y Monitoreo",
            font=("Helvetica", 12, "bold"),
            bg="#1e272e",
            fg="#f5f6fa",
            pady=8,
        )
        lbl_head.pack()

        lbl_sub = tk.Label(
            self,
            text="Autenticación de Operador con emisión de Token JWT (HS256)",
            font=("Helvetica", 9),
            bg="#1e272e",
            fg="#a4b0be",
        )
        lbl_sub.pack(pady=(0, 12))

        frame_form = tk.Frame(self, bg="#2f3640", padx=16, pady=16, relief="ridge", bd=1)
        frame_form.pack(fill="x", padx=25)

        tk.Label(
            frame_form,
            text="Usuario / Operador:",
            bg="#2f3640",
            fg="#f5f6fa",
            font=("Helvetica", 9, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=6)

        self.ent_usuario = tk.Entry(frame_form, font=("Helvetica", 10), width=22)
        self.ent_usuario.insert(0, usuario_default)
        self.ent_usuario.grid(row=0, column=1, pady=6, padx=8)

        tk.Label(
            frame_form,
            text="Contraseña:",
            bg="#2f3640",
            fg="#f5f6fa",
            font=("Helvetica", 9, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=6)

        self.ent_clave = tk.Entry(frame_form, show="•", font=("Helvetica", 10), width=22)
        # Pre-cargar sugerencia si existe en credenciales.json para facilitar la demo/defensa
        try:
            self.ent_clave.insert(0, clave(usuario_default))
        except Exception:
            pass
        self.ent_clave.grid(row=1, column=1, pady=6, padx=8)

        lbl_hint = tk.Label(
            self,
            text="💡 Tip: Operador 'desp-01' (clave en clientes/credenciales.json)",
            font=("Helvetica", 8, "italic"),
            bg="#1e272e",
            fg="#7bed9f",
        )
        lbl_hint.pack(pady=6)

        frame_botones = tk.Frame(self, bg="#1e272e")
        frame_botones.pack(pady=10)

        btn_cancelar = tk.Button(
            frame_botones,
            text="Cancelar",
            bg="#57606f",
            fg="#ffffff",
            font=("Helvetica", 9),
            padx=12,
            pady=4,
            command=self._cancelar,
        )
        btn_cancelar.pack(side="left", padx=8)

        btn_login = tk.Button(
            frame_botones,
            text="Iniciar Sesión ➔",
            bg="#2ed573",
            fg="#1e272e",
            font=("Helvetica", 9, "bold"),
            padx=14,
            pady=4,
            cursor="hand2",
            command=self._ingresar,
        )
        btn_login.pack(side="left", padx=8)

        self.bind("<Return>", lambda e: self._ingresar())
        self.protocol("WM_DELETE_WINDOW", self._cancelar)
        self.ent_clave.focus_set()

    def _ingresar(self):
        usuario = self.ent_usuario.get().strip()
        pwd = self.ent_clave.get().strip()
        if not usuario or not pwd:
            messagebox.showwarning("Atención", "Por favor ingrese usuario y contraseña.", parent=self)
            return

        try:
            token, claims = jwt_auth.autenticar_despachador(usuario, pwd)
            self.resultado = (usuario, pwd, token, claims)
            self.destroy()
        except Exception as e:
            messagebox.showerror("Error de Autenticación", f"Credenciales incorrectas: {e}", parent=self)

    def _cancelar(self):
        self.resultado = None
        self.destroy()


class DespachadorBasico:
    """
    Ventana interactiva de monitoreo y despacho en tiempo real con soporte TLS,
    gestión de incidentes, asignación/liberación de flota y respuesta a eventos.
    """

    def __init__(
        self,
        ip_broker="127.0.0.1",
        puerto=8883,
        client_id=USUARIO_DEFAULT,
        usuario=USUARIO_DEFAULT,
        password=None,
        ca_certs=CA_CERTS_DEFAULT,
        token_jwt=None,
        pedir_login=False,
    ):
        self.ip_broker = ip_broker
        self.puerto = puerto
        self.client_id = client_id
        self.usuario = usuario
        self.password = password
        self.ca_certs = ca_certs
        self.token_jwt = token_jwt
        self.claims_jwt = None

        # Marcadores activos en el mapa:
        # Flota: { "amb-01": marker, ... }
        self.marcadores = {}
        # Emergencias: { "E-01": marker, ... }
        self.marcadores_emergencias = {}

        # Estado local de la flota:
        # { "amb-01": {"tipo": "ambulancia", "estado_clave": "libre", "datos": {...}} }
        self.flota_info = {}

        # Estado local de emergencias activas:
        # { "E-01": {"tipo": "ambulancia", "lat": -45.86, "lon": -67.48, "estado": "sin_asignar", "unidad": None} }
        self.emergencias = {}

        # Mapeo de idempotencia de comandos sin respuesta todavía (se borra al responder el servicio):
        # { "asignar:amb-01:E-01": "uuid-...", "liberar:amb-01": "uuid-..." }
        self.comandos_ids = {}

        # Historial de alertas y respuestas para la pestaña de registro
        self.historial_eventos = deque(maxlen=100)

        # Selección activa en la interfaz
        self.emergencia_seleccionada_id = None
        self.unidad_seleccionada_id = None

        # Estado del servicio central: 'desconocido', 'online' u 'offline'
        self.estado_servicio = "desconocido"

        # Los callbacks de paho corren en otro hilo y tkinter solo se puede tocar desde el hilo
        # de la ventana: paho deja los mensajes en esta cola y la ventana los lee con root.after()
        self.mensajes = queue.Queue()

        # ----------------------------------------------------------------------
        # 1. Configurar la ventana principal de Tkinter
        # ----------------------------------------------------------------------
        self.root = tk.Tk()
        self.root.title("Centro de Despacho Oficial - Comodoro Rivadavia")
        self.root.geometry("1280x820")
        self.root.minsize(1050, 650)
        self.root.configure(bg="#1e272e")

        # Flujo de autenticación con Login interactivo si se solicitó
        if pedir_login:
            dialogo = DialogoLogin(self.root, usuario_default=self.usuario)
            self.root.wait_window(dialogo)
            if not dialogo.resultado:
                self.root.destroy()
                sys.exit(0)
            self.usuario, self.password, self.token_jwt, self.claims_jwt = dialogo.resultado
            self.client_id = self.usuario
            self.root.deiconify()
        else:
            if not self.password:
                try:
                    self.password = clave(self.usuario)
                except Exception:
                    self.password = None
            if not self.token_jwt:
                self.token_jwt = jwt_auth.generar_token(usuario=self.usuario, rol="despachador")
            try:
                self.claims_jwt = jwt_auth.verificar_token(self.token_jwt)
            except Exception:
                pass

        # ----------------------------------------------------------------------
        # 2. Barra superior: Título, resúmenes e indicador del servicio central
        # ----------------------------------------------------------------------
        self.barra_superior = tk.Frame(self.root, bg="#1e272e", padx=12, pady=8)
        self.barra_superior.pack(side="top", fill="x")

        self.lbl_titulo = tk.Label(
            self.barra_superior,
            text="🚨 Centro de Despacho General",
            bg="#1e272e",
            fg="#f5f6fa",
            font=("Helvetica", 12, "bold"),
        )
        self.lbl_titulo.pack(side="left")

        # Botón / Badge de sesión activa con Token JWT
        self.btn_sesion_jwt = tk.Button(
            self.barra_superior,
            text=f"🔑 {self.usuario} (JWT Activo 🟢)",
            bg="#27ae60",
            fg="#ffffff",
            font=("Helvetica", 9, "bold"),
            padx=8,
            pady=2,
            relief="groove",
            cursor="hand2",
            command=self.mostrar_info_jwt,
        )
        self.btn_sesion_jwt.pack(side="left", padx=12)

        # Indicador de "servicio caído" leyendo servicio/estado
        self.lbl_servicio = tk.Label(
            self.barra_superior,
            text="🟡 Conectando con Servicio Central...",
            bg="#f39c12",
            fg="#ffffff",
            font=("Helvetica", 9, "bold"),
            padx=10,
            pady=3,
            relief="groove",
        )
        self.lbl_servicio.pack(side="right")

        # Resumen en vivo de emergencias
        self.lbl_resumen_emergencias = tk.Label(
            self.barra_superior,
            text="Emergencias: 0 | ⚠️ 0 sin asignar | 🚨 0 asignadas",
            bg="#1e272e",
            fg="#f39c12",
            font=("Helvetica", 9, "bold"),
            padx=12,
        )
        self.lbl_resumen_emergencias.pack(side="right")

        # Resumen en vivo de la flota
        self.lbl_resumen_flota = tk.Label(
            self.barra_superior,
            text="Flota: 0 | 🟢 0 | 🔴 0 | ⚫ 0",
            bg="#1e272e",
            fg="#dcdde1",
            font=("Helvetica", 9),
            padx=10,
        )
        self.lbl_resumen_flota.pack(side="right")

        # ----------------------------------------------------------------------
        # 3. Barra de alerta destacada (aparece ante incidentes críticos o rechazos)
        # ----------------------------------------------------------------------
        self.frame_alerta = tk.Frame(self.root, bg="#c0392b", padx=12, pady=6)
        self.lbl_alerta_msg = tk.Label(
            self.frame_alerta,
            text="",
            bg="#c0392b",
            fg="#ffffff",
            font=("Helvetica", 10, "bold"),
        )
        self.lbl_alerta_msg.pack(side="left", padx=5)

        self.btn_alerta_accion = tk.Button(
            self.frame_alerta,
            text="🔁 Reasignar Emergencia",
            bg="#f39c12",
            fg="#1e272e",
            font=("Helvetica", 9, "bold"),
            cursor="hand2",
            padx=8,
            pady=2,
            relief="raised",
        )
        # Se empaca dinámicamente según la alerta

        self.btn_alerta_cerrar = tk.Button(
            self.frame_alerta,
            text="✕",
            bg="#962d22",
            fg="#ffffff",
            font=("Helvetica", 9, "bold"),
            relief="flat",
            cursor="hand2",
            command=self.ocultar_alerta_banner,
        )
        self.btn_alerta_cerrar.pack(side="right", padx=5)

        # ----------------------------------------------------------------------
        # 4. Contenedor principal dividido: Mapa (izquierda) y Panel de Control (derecha)
        # ----------------------------------------------------------------------
        self.paned_main = tk.PanedWindow(self.root, orient="horizontal", bg="#2f3640", sashrelief="raised", sashwidth=4)
        self.paned_main.pack(fill="both", expand=True)

        # Mapa interactivo (ArcGIS World Street Map)
        self.mapa = tkintermapview.TkinterMapView(self.paned_main, width=780, height=700)
        self.mapa.set_tile_server(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
            max_zoom=20,
        )
        self.mapa.set_position(-45.8642, -67.4850)  # Centro de Comodoro Rivadavia
        self.mapa.set_zoom(14)
        self.paned_main.add(self.mapa, stretch="always")

        # Panel lateral de control del despachador
        self.panel_control = tk.Frame(self.paned_main, width=460, bg="#2c3e50")
        self.paned_main.add(self.panel_control, stretch="never")

        self._crear_interfaz_panel_control()

        # ----------------------------------------------------------------------
        # 5. Cliente MQTT y suscripciones
        # ----------------------------------------------------------------------
        ca_file = None
        if ca_certs:
            if not Path(ca_certs).exists():
                raise SystemExit(f"No existe el certificado de la CA: {ca_certs} (para probar sin TLS: --no-tls)")
            ca_file = str(ca_certs)

        self.cliente = MQTTPub(
            self.ip_broker,
            self.puerto,
            self.client_id,
            usuario=self.usuario,
            password=self.password if self.password else clave(self.usuario),
            ca_certs=ca_file,
            sesion_persistente=True,
            expiracion_sesion=EXPIRACION_SESION_SEG,
        )

        # flota/# con QoS 0 (retained): foto actual de los móviles
        self.cliente.subscribe(topics.SUB_FLOTA, callback=self._on_flota, qos=0)

        # servicio/estado con QoS 1 (retained): vida del servicio central
        self.cliente.subscribe(topics.SERVICIO_ESTADO, callback=self._on_servicio_estado, qos=1)

        # emergencias/# con QoS 1 (retained): incidentes activos validados
        self.cliente.subscribe(topics.SUB_EMERGENCIAS, callback=self._on_emergencia, qos=1)

        # alertas/# con QoS 1: avisos puntuales (móviles ocupados desconectados, etc.)
        self.cliente.subscribe(topics.SUB_ALERTAS, callback=self._on_alerta, qos=1)

        # respuestas/{despachador} con QoS 1: rechazos y confirmaciones de órdenes
        self.cliente.subscribe(topics.respuestas(self.usuario), callback=self._on_respuesta, qos=1)

        print(f"[*] Conectando despachador a MQTT ({ip_broker}:{puerto}) [Usuario={usuario}, ID={client_id}]...")
        self.cliente.start()

        # Al cerrar la ventana, detener cliente MQTT
        self.root.protocol("WM_DELETE_WINDOW", self.cerrar)
        self.root.after(100, self._procesar_mensajes)

    # ==========================================================================
    # CONSTRUCCIÓN DE LA INTERFAZ DEL PANEL DE CONTROL
    # ==========================================================================

    def _crear_interfaz_panel_control(self):
        """Construye las pestañas de Emergencias, Flota y Registro de Alertas en el panel derecho."""
        estilo = ttk.Style(master=self.root)
        estilo.theme_use("clam")
        estilo.configure("TNotebook", background="#2c3e50", borderwidth=0)
        estilo.configure("TNotebook.Tab", background="#34495e", foreground="#ecf0f1", padding=[10, 4], font=("Helvetica", 9, "bold"))
        estilo.map("TNotebook.Tab", background=[("selected", "#1abc9c")], foreground=[("selected", "#ffffff")])
        estilo.configure("Treeview", background="#ffffff", foreground="#2c3e50", rowheight=24, font=("Helvetica", 9))
        estilo.configure("Treeview.Heading", font=("Helvetica", 9, "bold"), background="#bdc3c7", foreground="#2c3e50")

        self.notebook = ttk.Notebook(self.panel_control)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=6)

        # Pestaña 1: Emergencias
        self.tab_emergencias = tk.Frame(self.notebook, bg="#2c3e50")
        self.notebook.add(self.tab_emergencias, text="🚨 Emergencias")
        self._construir_tab_emergencias()

        # Pestaña 2: Flota de Móviles
        self.tab_flota = tk.Frame(self.notebook, bg="#2c3e50")
        self.notebook.add(self.tab_flota, text="🚓 Flota Móvil")
        self._construir_tab_flota()

        # Pestaña 3: Alertas y Respuestas
        self.tab_alertas = tk.Frame(self.notebook, bg="#2c3e50")
        self.notebook.add(self.tab_alertas, text="📋 Registro y Alertas")
        self._construir_tab_alertas()

    def _construir_tab_emergencias(self):
        """Controles y tabla para asignar, monitorear y reasignar emergencias."""
        # 1. Tabla de emergencias
        frame_tabla = tk.LabelFrame(self.tab_emergencias, text=" Incidentes Reportados (emergencias/#) ", bg="#2c3e50", fg="#ecf0f1", font=("Helvetica", 9, "bold"))
        frame_tabla.pack(fill="both", expand=True, padx=6, pady=6)

        columnas = ("eid", "tipo", "estado", "unidad")
        self.tree_emergencias = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=8, selectmode="browse")
        self.tree_emergencias.heading("eid", text="EID")
        self.tree_emergencias.heading("tipo", text="Fuerza")
        self.tree_emergencias.heading("estado", text="Estado")
        self.tree_emergencias.heading("unidad", text="Móvil Asignado")

        self.tree_emergencias.column("eid", width=70, anchor="center")
        self.tree_emergencias.column("tipo", width=85, anchor="center")
        self.tree_emergencias.column("estado", width=95, anchor="center")
        self.tree_emergencias.column("unidad", width=105, anchor="center")

        scroll_em = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree_emergencias.yview)
        self.tree_emergencias.configure(yscrollcommand=scroll_em.set)
        self.tree_emergencias.pack(side="left", fill="both", expand=True)
        scroll_em.pack(side="right", fill="y")

        self.tree_emergencias.bind("<<TreeviewSelect>>", self._on_tree_emergencia_select)

        # 2. Tarjeta de acción de despacho / asignación
        self.frame_accion_em = tk.LabelFrame(
            self.tab_emergencias,
            text=" Despachar Móvil a Emergencia Seleccionada ",
            bg="#2c3e50",
            fg="#f39c12",
            font=("Helvetica", 9, "bold"),
            padx=10,
            pady=10,
        )
        self.frame_accion_em.pack(fill="x", padx=6, pady=6)

        self.lbl_em_detalle = tk.Label(
            self.frame_accion_em,
            text="Seleccioná una emergencia en la lista o en el mapa.",
            bg="#2c3e50",
            fg="#ecf0f1",
            font=("Helvetica", 9),
            justify="left",
            wraplength=380,
        )
        self.lbl_em_detalle.pack(anchor="w", pady=(0, 6))

        # Selector de móvil libre para asignar
        frame_combo = tk.Frame(self.frame_accion_em, bg="#2c3e50")
        frame_combo.pack(fill="x", pady=4)

        tk.Label(frame_combo, text="Móvil Disponible:", bg="#2c3e50", fg="#bdc3c7", font=("Helvetica", 9)).pack(side="left")
        self.cbo_unidades_libres = ttk.Combobox(frame_combo, state="readonly", width=22)
        self.cbo_unidades_libres.pack(side="left", padx=8)

        # Botones de acción
        frame_botones_em = tk.Frame(self.frame_accion_em, bg="#2c3e50")
        frame_botones_em.pack(fill="x", pady=(6, 0))

        self.btn_asignar = tk.Button(
            frame_botones_em,
            text="🚀 Asignar Móvil",
            bg="#27ae60",
            fg="#ffffff",
            font=("Helvetica", 9, "bold"),
            cursor="hand2",
            command=self._on_click_asignar,
        )
        self.btn_asignar.pack(side="left", padx=(0, 6), expand=True, fill="x")

        self.btn_centrar_em = tk.Button(
            frame_botones_em,
            text="🎯 Ver en Mapa",
            bg="#34495e",
            fg="#ffffff",
            font=("Helvetica", 9),
            cursor="hand2",
            command=self._on_click_centrar_emergencia,
        )
        self.btn_centrar_em.pack(side="left", expand=True, fill="x")

    def _construir_tab_flota(self):
        """Controles y tabla para visualizar la flota completa y marcar unidades libres."""
        frame_tabla = tk.LabelFrame(self.tab_flota, text=" Estado Oficial de Móviles (flota/#) ", bg="#2c3e50", fg="#ecf0f1", font=("Helvetica", 9, "bold"))
        frame_tabla.pack(fill="both", expand=True, padx=6, pady=6)

        columnas = ("id", "tipo", "conexion", "disponibilidad", "eid")
        self.tree_flota = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=8, selectmode="browse")
        self.tree_flota.heading("id", text="Móvil")
        self.tree_flota.heading("tipo", text="Fuerza")
        self.tree_flota.heading("conexion", text="Conexión")
        self.tree_flota.heading("disponibilidad", text="Disponibilidad")
        self.tree_flota.heading("eid", text="Incidente")

        self.tree_flota.column("id", width=65, anchor="center")
        self.tree_flota.column("tipo", width=80, anchor="center")
        self.tree_flota.column("conexion", width=75, anchor="center")
        self.tree_flota.column("disponibilidad", width=85, anchor="center")
        self.tree_flota.column("eid", width=75, anchor="center")

        scroll_flota = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree_flota.yview)
        self.tree_flota.configure(yscrollcommand=scroll_flota.set)
        self.tree_flota.pack(side="left", fill="both", expand=True)
        scroll_flota.pack(side="right", fill="y")

        self.tree_flota.bind("<<TreeviewSelect>>", self._on_tree_flota_select)

        # Tarjeta de acción para liberar móvil
        self.frame_accion_flota = tk.LabelFrame(
            self.tab_flota,
            text=" Gestión Operativa de Móvil ",
            bg="#2c3e50",
            fg="#ecf0f1",
            font=("Helvetica", 9, "bold"),
            padx=10,
            pady=10,
        )
        self.frame_accion_flota.pack(fill="x", padx=6, pady=6)

        self.lbl_flota_detalle = tk.Label(
            self.frame_accion_flota,
            text="Seleccioná un móvil en la lista o en el mapa.",
            bg="#2c3e50",
            fg="#ecf0f1",
            font=("Helvetica", 9),
            justify="left",
            wraplength=380,
        )
        self.lbl_flota_detalle.pack(anchor="w", pady=(0, 6))

        frame_botones_flota = tk.Frame(self.frame_accion_flota, bg="#2c3e50")
        frame_botones_flota.pack(fill="x", pady=(4, 0))

        self.btn_marcar_libre = tk.Button(
            frame_botones_flota,
            text="🔓 Marcar Libre (Finalizar Emergencia)",
            bg="#e67e22",
            fg="#ffffff",
            font=("Helvetica", 9, "bold"),
            cursor="hand2",
            command=self._on_click_marcar_libre,
        )
        self.btn_marcar_libre.pack(side="left", padx=(0, 6), expand=True, fill="x")

        self.btn_centrar_flota = tk.Button(
            frame_botones_flota,
            text="🎯 Ver Móvil",
            bg="#34495e",
            fg="#ffffff",
            font=("Helvetica", 9),
            cursor="hand2",
            command=self._on_click_centrar_flota,
        )
        self.btn_centrar_flota.pack(side="left", expand=True, fill="x")

    def _construir_tab_alertas(self):
        """Registro cronológico de avisos, comandos rechazados y respuestas del servicio."""
        frame_registro = tk.Frame(self.tab_alertas, bg="#2c3e50")
        frame_registro.pack(fill="both", expand=True, padx=6, pady=6)

        self.txt_registro = tk.Text(
            frame_registro,
            bg="#1e272e",
            fg="#dcdde1",
            font=("Consolas", 9),
            wrap="word",
            padx=6,
            pady=6,
        )
        scroll_reg = ttk.Scrollbar(frame_registro, orient="vertical", command=self.txt_registro.yview)
        self.txt_registro.configure(yscrollcommand=scroll_reg.set)
        self.txt_registro.pack(side="left", fill="both", expand=True)
        scroll_reg.pack(side="right", fill="y")

        # Configurar colores para tipos de eventos
        self.txt_registro.tag_config("alerta", foreground="#e74c3c", font=("Consolas", 9, "bold"))
        self.txt_registro.tag_config("rechazo", foreground="#e67e22", font=("Consolas", 9, "bold"))
        self.txt_registro.tag_config("exito", foreground="#2ecc71")
        self.txt_registro.tag_config("info", foreground="#3498db")

        btn_limpiar = tk.Button(
            self.tab_alertas,
            text="🗑 Limpiar Registro",
            bg="#34495e",
            fg="#ffffff",
            font=("Helvetica", 8),
            cursor="hand2",
            command=self._limpiar_registro,
        )
        btn_limpiar.pack(fill="x", padx=6, pady=(0, 6))

    def _limpiar_registro(self):
        self.txt_registro.delete("1.0", tk.END)

    def registrar_evento(self, texto, tag=None):
        """Agrega un mensaje con timestamp al registro de la tercera pestaña."""
        hora = datetime.now().strftime("%H:%M:%S")
        linea = f"[{hora}] {texto}\n"
        self.txt_registro.insert(tk.END, linea, tag)
        self.txt_registro.see(tk.END)

    # ==========================================================================
    # MANEJO DEL BANNER DE ALERTAS DESTACADAS
    # ==========================================================================

    def mostrar_alerta_banner(self, texto, accion_texto=None, callback_accion=None, es_critico=True):
        """Muestra una alerta visible de alto contraste en la parte superior."""
        bg_color = "#c0392b" if es_critico else "#d35400"
        self.frame_alerta.config(bg=bg_color)
        self.lbl_alerta_msg.config(text=texto, bg=bg_color)

        if accion_texto and callback_accion:
            self.btn_alerta_accion.config(text=accion_texto, command=callback_accion)
            self.btn_alerta_accion.pack(side="right", padx=8)
        else:
            self.btn_alerta_accion.pack_forget()

        # Posicionarlo justo debajo de la barra superior
        self.frame_alerta.pack(side="top", fill="x", before=self.paned_main)

    def ocultar_alerta_banner(self):
        """Oculta la barra de alerta destacada."""
        self.frame_alerta.pack_forget()

    # ==========================================================================
    # CALLBACKS DE RED (HILO PAHO-MQTT)
    # ==========================================================================

    def _on_flota(self, client, userdata, msg):
        """Valida el estado de la unidad y lo encola para la ventana gráfica."""
        try:
            tipo, id_unidad = topics.parse_flota_estado(msg.topic)
            estado = payloads.validar(payloads.FLOTA_ESTADO, msg.payload)
        except ValueError as e:
            print(f"[Aviso] Mensaje descartado en {msg.topic}: {e}")
            return
        self.mensajes.put(("flota", tipo, id_unidad, estado))

    def _on_servicio_estado(self, client, userdata, msg):
        """Valida el estado de vida del servicio central (online / offline)."""
        try:
            datos = payloads.validar(payloads.SERVICIO_ESTADO, msg.payload)
        except ValueError as e:
            print(f"[Aviso] Mensaje descartado en {msg.topic}: {e}")
            return
        self.mensajes.put(("servicio", datos["estado"]))

    def _on_emergencia(self, client, userdata, msg):
        """Valida incidentes en emergencias/#. Si el payload está vacío con retained, es cierre."""
        try:
            tipo, eid = topics.parse_emergencia(msg.topic)
            if payloads.es_cierre(msg.payload):
                self.mensajes.put(("emergencia_cierre", tipo, eid))
                return
            datos = payloads.validar(payloads.EMERGENCIA, msg.payload)
        except ValueError as e:
            print(f"[Aviso] Mensaje descartado en {msg.topic}: {e}")
            return
        self.mensajes.put(("emergencia", tipo, eid, datos))

    def _on_alerta(self, client, userdata, msg):
        """Valida alertas operativas recibidas en alertas/#."""
        try:
            tipo = topics.parse_alertas(msg.topic)
            datos = payloads.validar(payloads.ALERTA, msg.payload)
        except ValueError as e:
            print(f"[Aviso] Mensaje descartado en {msg.topic}: {e}")
            return
        self.mensajes.put(("alerta", tipo, datos))

    def _on_respuesta(self, client, userdata, msg):
        """Valida las respuestas a comandos recibidas en respuestas/{despachador}."""
        try:
            datos = payloads.validar(payloads.RESPUESTA, msg.payload)
        except ValueError as e:
            print(f"[Aviso] Mensaje descartado en {msg.topic}: {e}")
            return
        self.mensajes.put(("respuesta", datos))

    # ==========================================================================
    # PROCESAMIENTO PRINCIPAL EN EL HILO DE TKINTER
    # ==========================================================================

    def _procesar_mensajes(self):
        """Lee la cola concurrente y actualiza mapa y elementos gráficos de forma segura."""
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
            elif tipo_msg == "emergencia":
                _, tipo, eid, datos = mensaje
                self.actualizar_emergencia(tipo, eid, datos)
            elif tipo_msg == "emergencia_cierre":
                _, tipo, eid = mensaje
                self.cerrar_emergencia(tipo, eid)
            elif tipo_msg == "alerta":
                _, tipo, datos = mensaje
                self.procesar_alerta(tipo, datos)
            elif tipo_msg == "respuesta":
                _, datos = mensaje
                self.procesar_respuesta(datos)

        self.root.after(100, self._procesar_mensajes)

    # ==========================================================================
    # GESTIÓN DE EMERGENCIAS (TAREAS 1, 2 Y 4)
    # ==========================================================================

    def actualizar_emergencia(self, tipo, eid, datos):
        """
        Crea o actualiza el marcador de la emergencia en el mapa y la lista:
        - Si está 'sin_asignar': color ámbar/naranja ⚠️.
        - Si está 'asignada': color azul 🚨 con la unidad encargada.
        """
        lat = datos["lat"]
        lon = datos["lon"]
        estado = datos["estado"]
        unidad = datos.get("unidad")

        self.emergencias[eid] = {
            "tipo": tipo,
            "lat": lat,
            "lon": lon,
            "estado": estado,
            "unidad": unidad,
            "ts": datos.get("ts"),
        }

        color_circulo, color_borde = COLORES_EMERGENCIA.get(estado, ("#e67e22", "#d35400"))
        icono_fuerza = ICONOS.get(tipo, "⚠️")

        if estado == "sin_asignar":
            etiqueta = f"⚠️ {eid} [{tipo.upper()}] (SIN ASIGNAR)"
        else:
            etiqueta = f"🚨 {eid} [{tipo.upper()}] ➔ {unidad or 'Asignada'}"

        if eid in self.marcadores_emergencias:
            marcador = self.marcadores_emergencias[eid]
            marcador.set_position(lat, lon)
            marcador.set_text(etiqueta)
            self._actualizar_color_marcador(marcador, color_circulo, color_borde)
            marcador.data = {"tipo_objeto": "emergencia", "eid": eid, "tipo": tipo}
        else:
            print(f"[Emergencia] Registrada en mapa: {eid} ({tipo}) en ({lat}, {lon}) [{estado}]")
            nuevo_marcador = self.mapa.set_marker(
                lat,
                lon,
                text=etiqueta,
                text_color="#ffffff",
                font=("Helvetica", 9, "bold"),
                marker_color_circle=color_circulo,
                marker_color_outside=color_borde,
                command=self._on_marcador_click,
                data={"tipo_objeto": "emergencia", "eid": eid, "tipo": tipo},
            )
            self.marcadores_emergencias[eid] = nuevo_marcador

        self._actualizar_tabla_emergencias()
        self._actualizar_resumen_emergencias()

        # Si esta emergencia está seleccionada en los controles, refrescar detalles
        if self.emergencia_seleccionada_id == eid:
            self._mostrar_detalle_emergencia(eid)

    def cerrar_emergencia(self, tipo, eid):
        """Elimina el marcador de la emergencia del mapa y la remueve de la lista activa."""
        if eid in self.marcadores_emergencias:
            self.marcadores_emergencias[eid].delete()
            del self.marcadores_emergencias[eid]

        if eid in self.emergencias:
            del self.emergencias[eid]

        print(f"[Emergencia] Cerrada y removida del mapa: {eid} ({tipo})")
        self.registrar_evento(f"🏁 Emergencia {eid} ({tipo}) finalizada y cerrada.", tag="info")

        if self.emergencia_seleccionada_id == eid:
            self.emergencia_seleccionada_id = None
            self.lbl_em_detalle.config(text="Seleccioná una emergencia en la lista o en el mapa.")
            self.cbo_unidades_libres["values"] = []
            self.cbo_unidades_libres.set("")

        self._actualizar_tabla_emergencias()
        self._actualizar_resumen_emergencias()

    def _actualizar_tabla_emergencias(self):
        """Refresca las filas del Treeview de emergencias manteniendo la selección."""
        seleccion = self.tree_emergencias.selection()
        item_sel_eid = self.tree_emergencias.item(seleccion[0])["values"][0] if seleccion else None

        self.tree_emergencias.delete(*self.tree_emergencias.get_children())
        for eid, info in sorted(self.emergencias.items()):
            unidad_str = info["unidad"] if info["unidad"] else "-"
            item_id = self.tree_emergencias.insert(
                "",
                tk.END,
                values=(eid, info["tipo"], info["estado"], unidad_str),
            )
            if item_sel_eid == eid or self.emergencia_seleccionada_id == eid:
                self.tree_emergencias.selection_set(item_id)

    def _actualizar_resumen_emergencias(self):
        """Actualiza el contador de emergencias en la barra superior."""
        total = len(self.emergencias)
        sin_asignar = sum(1 for e in self.emergencias.values() if e["estado"] == "sin_asignar")
        asignadas = sum(1 for e in self.emergencias.values() if e["estado"] == "asignada")
        self.lbl_resumen_emergencias.config(
            text=f"Emergencias: {total} | ⚠️ {sin_asignar} sin asignar | 🚨 {asignadas} asignadas"
        )

    # ==========================================================================
    # GESTIÓN DE FLOTA (TAREAS 2 Y 3)
    # ==========================================================================

    def actualizar_unidad(self, id_unidad, tipo, estado):
        """
        Crea o actualiza el marcador del móvil en el mapa:
        - Color según disponibilidad y conexión: verde (libre), rojo (ocupada), gris (sin conexión).
        - Orientación con rumbo.
        """
        conexion = estado.get("conexion", "offline")
        disponibilidad = estado.get("disponibilidad", "libre")
        lat = estado.get("lat")
        lon = estado.get("lon")
        rumbo = estado.get("rumbo")
        eid = estado.get("eid")

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

        rumbo_str = formato_rumbo(rumbo) if conexion != "offline" else ""
        partes_etiqueta = [f"{icono} {id_unidad}", f"[{estado_desc}]"]
        if rumbo_str:
            partes_etiqueta.append(rumbo_str)
        etiqueta = " ".join(partes_etiqueta)

        if id_unidad in self.marcadores:
            marcador = self.marcadores[id_unidad]
            if lat is not None and lon is not None:
                marcador.set_position(lat, lon)
            marcador.set_text(etiqueta)
            self._actualizar_color_marcador(marcador, color_circulo, color_borde)
            marcador.data = {"tipo_objeto": "unidad", "id": id_unidad, "tipo": tipo}
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
                    command=self._on_marcador_click,
                    data={"tipo_objeto": "unidad", "id": id_unidad, "tipo": tipo},
                )
                self.marcadores[id_unidad] = nuevo_marcador

        # Guardar estado completo
        self.flota_info[id_unidad] = {
            "tipo": tipo,
            "estado": estado_clave,
            "datos": estado,
        }

        self._actualizar_tabla_flota()
        self._actualizar_resumen_flota()

        # Si hay una emergencia seleccionada, refrescar los móviles disponibles
        if self.emergencia_seleccionada_id in self.emergencias:
            self._actualizar_combo_unidades_libres(self.emergencias[self.emergencia_seleccionada_id]["tipo"])

        # Si esta unidad está seleccionada en el detalle de flota, refrescar tarjeta
        if self.unidad_seleccionada_id == id_unidad:
            self._mostrar_detalle_flota(id_unidad)

    def _actualizar_tabla_flota(self):
        """Refresca la tabla de flota de la segunda pestaña."""
        seleccion = self.tree_flota.selection()
        item_sel_id = self.tree_flota.item(seleccion[0])["values"][0] if seleccion else None

        self.tree_flota.delete(*self.tree_flota.get_children())
        for uid, info in sorted(self.flota_info.items()):
            datos = info["datos"]
            eid_str = datos.get("eid") or "-"
            item_id = self.tree_flota.insert(
                "",
                tk.END,
                values=(uid, info["tipo"], datos.get("conexion", "-"), datos.get("disponibilidad", "-"), eid_str),
            )
            if item_sel_id == uid or self.unidad_seleccionada_id == uid:
                self.tree_flota.selection_set(item_id)

    def _actualizar_resumen_flota(self):
        """Actualiza el contador de unidades según sus estados operativos en la barra superior."""
        total = len(self.flota_info)
        libres = sum(1 for info in self.flota_info.values() if info["estado"] == "libre")
        ocupadas = sum(1 for info in self.flota_info.values() if info["estado"] == "ocupada")
        desconectadas = sum(1 for info in self.flota_info.values() if info["estado"] == "sin_conexion")
        self.lbl_resumen_flota.config(
            text=f"Flota: {total} | 🟢 {libres} libres | 🔴 {ocupadas} ocupadas | ⚫ {desconectadas} sin conexión"
        )

    # ==========================================================================
    # INTERACCIÓN CON MARCADORES DEL MAPA
    # ==========================================================================

    def _on_marcador_click(self, marcador):
        """Callback cuando el usuario hace clic sobre un marcador del mapa."""
        if not hasattr(marcador, "data") or not marcador.data:
            return

        tipo_objeto = marcador.data.get("tipo_objeto")
        if tipo_objeto == "emergencia":
            eid = marcador.data.get("eid")
            self.notebook.select(self.tab_emergencias)
            self.seleccionar_emergencia_por_id(eid)
        elif tipo_objeto == "unidad":
            uid = marcador.data.get("id")
            self.notebook.select(self.tab_flota)
            self.seleccionar_unidad_por_id(uid)

    def _actualizar_color_marcador(self, marcador, color_circulo, color_borde):
        """Actualiza los colores de un marcador existente en el mapa de forma dinámica."""
        marcador.marker_color_circle = color_circulo
        marcador.marker_color_outside = color_borde
        if marcador.polygon:
            self.mapa.canvas.itemconfig(marcador.polygon, fill=color_borde, outline=color_borde)
        if marcador.big_circle:
            self.mapa.canvas.itemconfig(marcador.big_circle, fill=color_circulo, outline=color_borde)

    # ==========================================================================
    # GESTIÓN Y VALIDACIÓN DE SESIÓN JWT (RFC 7519)
    # ==========================================================================

    def _verificar_autorizacion_jwt(self, operacion="operar"):
        """Verifica que el despachador disponga de un token JWT válido y vigente."""
        if not self.token_jwt:
            messagebox.showerror(
                "Acceso Denegado (JWT)",
                f"No hay una sesión activa con token JWT para {operacion}.\nDebe iniciar sesión.",
            )
            return False

        try:
            self.claims_jwt = jwt_auth.verificar_token(self.token_jwt)
            return True
        except jwt_auth.TokenExpirado:
            messagebox.showwarning(
                "Sesión Expirada (JWT)",
                "Su token JWT ha expirado.\nPor seguridad inicie sesión nuevamente.",
            )
            return False
        except jwt_auth.ErrorJWT as e:
            messagebox.showerror(
                "Token Inválido (JWT)",
                f"Fallo de verificación de integridad o firma criptográfica JWT:\n{e}",
            )
            return False

    def mostrar_info_jwt(self):
        """Muestra una ventana modal detallada con los claims y el token JWT de la sesión activa."""
        if not self.token_jwt:
            messagebox.showinfo("JWT", "No hay una sesión JWT activa actualmente.")
            return

        try:
            header, payload = jwt_auth.decodificar_sin_verificar(self.token_jwt)
            jwt_auth.verificar_token(self.token_jwt)
            msg_validez = "VÁLIDO 🟢"
        except jwt_auth.TokenExpirado:
            msg_validez = "EXPIRADO 🔴"
        except Exception as e:
            header, payload = {}, {}
            msg_validez = f"INVÁLIDO 🔴 ({e})"

        exp = payload.get("exp", 0)
        ahora = int(time.time())
        restante_min = max(0, int((exp - ahora) / 60))

        win = tk.Toplevel(self.root)
        win.title("🔐 Sesión Activa - Token JWT (RFC 7519)")
        win.geometry("680x620")
        win.minsize(580, 520)
        win.configure(bg="#1e272e")
        win.transient(self.root)

        tk.Label(
            win,
            text="Detalles de la Sesión y Token JWT",
            font=("Helvetica", 12, "bold"),
            bg="#1e272e",
            fg="#f5f6fa",
            pady=10,
        ).pack()

        frame_resumen = tk.Frame(win, bg="#2f3640", padx=12, pady=8)
        frame_resumen.pack(fill="x", padx=15, pady=5)

        tk.Label(
            frame_resumen,
            text=f"Operador (sub): {payload.get('sub', self.usuario)}   |   Rol: {payload.get('rol', 'despachador')}   |   Algoritmo: {header.get('alg', 'HS256')}\nEstado: {msg_validez}   |   Expira en: {restante_min} minutos (exp: {exp})",
            font=("Helvetica", 9),
            bg="#2f3640",
            fg="#ecf0f1",
            justify="left",
        ).pack(anchor="w")

        lbl_raw = tk.Label(win, text="Token Compacto (Header.Payload.Signature):", font=("Helvetica", 9, "bold"), bg="#1e272e", fg="#a4b0be")
        lbl_raw.pack(anchor="w", padx=15, pady=(8, 2))

        txt_token = tk.Text(win, height=4, font=("Consolas", 8), bg="#000000", fg="#2ecc71", wrap="char")
        txt_token.insert("1.0", self.token_jwt)
        txt_token.configure(state="disabled")
        txt_token.pack(fill="x", padx=15)

        def copiar():
            self.root.clipboard_clear()
            self.root.clipboard_append(self.token_jwt)
            btn_copiar.config(text="¡Token Copiado al Portapapeles! ✓", bg="#27ae60")

        btn_copiar = tk.Button(
            win,
            text="📋 Copiar Token para jwt.io",
            bg="#34495e",
            fg="#ffffff",
            font=("Helvetica", 8, "bold"),
            cursor="hand2",
            command=copiar,
            pady=3,
        )
        btn_copiar.pack(anchor="e", padx=15, pady=4)

        frame_detalles = tk.Frame(win, bg="#1e272e")
        frame_detalles.pack(fill="both", expand=True, padx=15, pady=5)

        frame_hdr = tk.LabelFrame(frame_detalles, text=" Header (JSON) ", bg="#2c3e50", fg="#ecf0f1", font=("Helvetica", 9, "bold"))
        frame_hdr.pack(fill="x", pady=4)
        txt_hdr = tk.Text(frame_hdr, height=3, font=("Consolas", 9), bg="#1e272e", fg="#3498db")
        txt_hdr.insert("1.0", json.dumps(header, indent=2))
        txt_hdr.configure(state="disabled")
        txt_hdr.pack(fill="x", padx=5, pady=5)

        frame_pld = tk.LabelFrame(frame_detalles, text=" Claims / Payload (JSON) ", bg="#2c3e50", fg="#ecf0f1", font=("Helvetica", 9, "bold"))
        frame_pld.pack(fill="both", expand=True, pady=4)
        txt_pld = tk.Text(frame_pld, height=8, font=("Consolas", 9), bg="#1e272e", fg="#e67e22")
        txt_pld.insert("1.0", json.dumps(payload, indent=2))
        txt_pld.configure(state="disabled")
        txt_pld.pack(fill="both", expand=True, padx=5, pady=5)

        btn_cerrar = tk.Button(
            win,
            text="Cerrar",
            bg="#7f8c8d",
            fg="#ffffff",
            font=("Helvetica", 9, "bold"),
            command=win.destroy,
            pady=4,
            padx=12,
        )
        btn_cerrar.pack(pady=8)

    # ==========================================================================
    # COMANDOS DE DESPACHO (TAREAS 2 Y 3: ASIGNAR Y LIBERAR)
    # ==========================================================================

    def asignar_unidad(self, id_unidad, tipo, eid):
        """
        Publica orden en comandos/desp-01/{tipo}/{id} con el eid y un id único de comando.
        Idempotencia: Si se reintenta la misma acción (mismo móvil y mismo eid), se reutiliza el mismo ID.
        """
        if not self._verificar_autorizacion_jwt("asignar una unidad"):
            return

        if not id_unidad or not eid:
            messagebox.showwarning("Atención", "Debe seleccionar un incidente y un móvil para asignar.")
            return

        # Generar o recuperar el ID único de esta acción para garantizar idempotencia en reintentos
        clave_accion = f"asignar:{id_unidad}:{eid}"
        if clave_accion not in self.comandos_ids:
            self.comandos_ids[clave_accion] = payloads.nuevo_id()
        cmd_id = self.comandos_ids[clave_accion]

        payload = payloads.comando(accion="asignar", eid=eid, id=cmd_id, jwt=self.token_jwt)
        topico = topics.comando(self.usuario, tipo, id_unidad)

        ok = self.cliente.publish(payload, topic=topico, qos=1, retain=False)
        if ok:
            msg = f"🚀 Comando 'asignar' enviado: {id_unidad} -> {eid} (ID: {cmd_id[:8]}...)"
            print(f"[*] [Comando Asignar] {id_unidad} -> {eid} (ID: {cmd_id[:8]}...) en {topico}")
            self.registrar_evento(msg, tag="info")
        else:
            self.registrar_evento(f"⚠️ Error al publicar asignación de {id_unidad}", tag="rechazo")

    def liberar_unidad(self, id_unidad, tipo):
        """
        Publica orden 'liberar' en comandos/desp-01/{tipo}/{id}.
        El servicio central marca la unidad como libre y cierra la emergencia.
        Idempotencia: Si se reintenta la liberación de la unidad, se reutiliza el mismo ID.
        """
        if not self._verificar_autorizacion_jwt("liberar una unidad"):
            return

        if not id_unidad:
            messagebox.showwarning("Atención", "Debe seleccionar un móvil ocupado para liberar.")
            return

        clave_accion = f"liberar:{id_unidad}"
        if clave_accion not in self.comandos_ids:
            self.comandos_ids[clave_accion] = payloads.nuevo_id()
        cmd_id = self.comandos_ids[clave_accion]

        payload = payloads.comando(accion="liberar", id=cmd_id, jwt=self.token_jwt)
        topico = topics.comando(self.usuario, tipo, id_unidad)

        ok = self.cliente.publish(payload, topic=topico, qos=1, retain=False)
        if ok:
            msg = f"🔓 Comando 'liberar' enviado para {id_unidad} (ID: {cmd_id[:8]}...)"
            print(f"[*] [Comando Liberar] {id_unidad} (ID: {cmd_id[:8]}...) en {topico}")
            self.registrar_evento(msg, tag="info")
        else:
            self.registrar_evento(f"⚠️ Error al publicar liberación de {id_unidad}", tag="rechazo")

    # ==========================================================================
    # ALERTAS Y REASIGNACIÓN (TAREA 4)
    # ==========================================================================

    def procesar_alerta(self, tipo, datos):
        """
        Procesa alertas de alertas/#:
        - Si una unidad ocupada se cae, muestra una alerta visible y habilita reasignación directa.
        """
        motivo = datos.get("motivo")
        mensaje = datos.get("mensaje", "")
        unidad = datos.get("unidad")
        eid = datos.get("eid")

        desc = f"⚠️ ALERTA ({tipo}): {mensaje}"
        if unidad:
            desc += f" | Móvil: {unidad}"
        if eid:
            desc += f" | Incidente: {eid}"

        self.registrar_evento(desc, tag="alerta")

        # Alerta visible cuando se cae una unidad ocupada
        if motivo == "unidad_desconectada" and eid:
            texto_alerta = f"⚠️ ¡MÓVIL OCUPADO DESCONECTADO! {unidad} se cayó en emergencia {eid}."
            self.mostrar_alerta_banner(
                texto=texto_alerta,
                accion_texto=f"🔁 Reasignar {eid}",
                callback_accion=lambda e=eid, t=tipo: self.preparar_reasignacion(e, t),
                es_critico=True,
            )
        elif motivo == "unidad_desconectada":
            self.mostrar_alerta_banner(
                texto=f"⚠️ Móvil {unidad} perdió conexión.",
                es_critico=False,
            )

    def preparar_reasignacion(self, eid, tipo):
        """Focaliza la emergencia caída para que el despachador seleccione una nueva unidad con 1 clic."""
        self.ocultar_alerta_banner()
        # Pudo cerrarse entre que llegó la alerta y el clic en el banner
        if eid not in self.emergencias:
            self.registrar_evento(f"La emergencia {eid} ya no está activa.", tag="info")
            return
        self.notebook.select(self.tab_emergencias)
        self.seleccionar_emergencia_por_id(eid)
        em = self.emergencias[eid]
        self.mapa.set_position(em["lat"], em["lon"])
        self.mapa.set_zoom(15)

    # ==========================================================================
    # RESPUESTAS Y COMANDOS RECHAZADOS (TAREA 5)
    # ==========================================================================

    def procesar_respuesta(self, datos):
        """
        Procesa respuestas/{despachador}:
        - Muestra visiblemente los comandos rechazados con su motivo.
        """
        cmd_id = datos.get("id", "desconocido")
        ok = datos.get("ok", False)
        motivo = datos.get("motivo", "Sin detalle especificado")

        # La acción ya tuvo respuesta: repetirla es una acción nueva y lleva otro id.
        # Con el mismo id el servicio la tomaría como duplicado y no la aplicaría (docs/topics.md, Idempotencia)
        self.comandos_ids = {accion: id_cmd for accion, id_cmd in self.comandos_ids.items() if id_cmd != cmd_id}

        if not ok:
            texto_rechazo = f"❌ COMANDO RECHAZADO: {motivo} (ID: {cmd_id[:8]}...)"
            self.registrar_evento(texto_rechazo, tag="rechazo")
            self.mostrar_alerta_banner(
                texto=f"❌ Comando rechazado: {motivo}",
                es_critico=True,
            )
        else:
            self.registrar_evento(f"✅ Comando confirmado por el servicio central (ID: {cmd_id[:8]}...)", tag="exito")

    # ==========================================================================
    # ESTADO DEL SERVICIO CENTRAL
    # ==========================================================================

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
            self.mostrar_alerta_banner("🔴 El Servicio Central está desconectado. Los comandos se procesarán al volver.", es_critico=True)
        else:
            self.lbl_servicio.config(
                text=f"🟡 Servicio Central: {estado}",
                bg="#f39c12",
                fg="#ffffff",
            )

    # ==========================================================================
    # MÉTODOS DE SOPORTE PARA SELECCIÓN EN INTERFAZ
    # ==========================================================================

    def seleccionar_emergencia_por_id(self, eid):
        """Selecciona una emergencia en el Treeview y muestra sus detalles."""
        self.emergencia_seleccionada_id = eid
        for item in self.tree_emergencias.get_children():
            if self.tree_emergencias.item(item)["values"][0] == eid:
                self.tree_emergencias.selection_set(item)
                self.tree_emergencias.see(item)
                break
        self._mostrar_detalle_emergencia(eid)

    def _mostrar_detalle_emergencia(self, eid):
        """Muestra los datos de la emergencia en la tarjeta y actualiza los móviles libres disponibles."""
        if eid not in self.emergencias:
            return
        em = self.emergencias[eid]
        fuerza = em["tipo"]
        estado = em["estado"]
        unidad = em["unidad"] or "Ninguna"

        txt = f"Incidente: {eid} | Fuerza: {fuerza.upper()}\nEstado: {estado.upper()} | Móvil: {unidad}"
        self.lbl_em_detalle.config(text=txt)

        self._actualizar_combo_unidades_libres(fuerza)

    def _actualizar_combo_unidades_libres(self, tipo_fuerza):
        """Llena el combobox con unidades conectadas y libres de la fuerza requerida."""
        libres = [
            uid for uid, info in self.flota_info.items()
            if info["tipo"] == tipo_fuerza
            and info["datos"].get("conexion") == "online"
            and info["datos"].get("disponibilidad") == "libre"
        ]
        self.cbo_unidades_libres["values"] = sorted(libres)
        # Se llama con cada mensaje de flota/: si el móvil elegido sigue libre, no se le cambia la selección
        if self.cbo_unidades_libres.get() in libres:
            return
        if libres:
            self.cbo_unidades_libres.current(0)
        else:
            self.cbo_unidades_libres.set("(Sin móviles libres)")

    def _on_tree_emergencia_select(self, event):
        seleccion = self.tree_emergencias.selection()
        if not seleccion:
            return
        eid = self.tree_emergencias.item(seleccion[0])["values"][0]
        self.emergencia_seleccionada_id = eid
        self._mostrar_detalle_emergencia(eid)

    def _on_click_asignar(self):
        if not self.emergencia_seleccionada_id or self.emergencia_seleccionada_id not in self.emergencias:
            messagebox.showwarning("Atención", "Seleccioná primero una emergencia en la lista o en el mapa.")
            return

        em = self.emergencias[self.emergencia_seleccionada_id]
        unidad_elegida = self.cbo_unidades_libres.get()
        if not unidad_elegida or unidad_elegida.startswith("("):
            messagebox.showwarning("Atención", f"No hay móviles libres de fuerza '{em['tipo']}' disponibles.")
            return

        self.asignar_unidad(unidad_elegida, em["tipo"], self.emergencia_seleccionada_id)

    def _on_click_centrar_emergencia(self):
        if not self.emergencia_seleccionada_id or self.emergencia_seleccionada_id not in self.emergencias:
            return
        em = self.emergencias[self.emergencia_seleccionada_id]
        self.mapa.set_position(em["lat"], em["lon"])
        self.mapa.set_zoom(16)

    def seleccionar_unidad_por_id(self, uid):
        """Selecciona una unidad en el Treeview de flota y muestra sus detalles."""
        self.unidad_seleccionada_id = uid
        for item in self.tree_flota.get_children():
            if self.tree_flota.item(item)["values"][0] == uid:
                self.tree_flota.selection_set(item)
                self.tree_flota.see(item)
                break
        self._mostrar_detalle_flota(uid)

    def _mostrar_detalle_flota(self, uid):
        """Muestra los detalles del móvil en la tarjeta y habilita 'Marcar Libre' si está ocupada."""
        if uid not in self.flota_info:
            return
        info = self.flota_info[uid]
        datos = info["datos"]
        fuerza = info["tipo"]
        conexion = datos.get("conexion", "-")
        disponibilidad = datos.get("disponibilidad", "-")
        eid = datos.get("eid") or "-"

        txt = f"Móvil: {uid} ({fuerza}) | Conexión: {conexion}\nEstado: {disponibilidad} | Incidente: {eid}"
        self.lbl_flota_detalle.config(text=txt)

        if disponibilidad == "ocupada":
            self.btn_marcar_libre.config(state="normal", bg="#e67e22")
        else:
            self.btn_marcar_libre.config(state="disabled", bg="#7f8c8d")

    def _on_tree_flota_select(self, event):
        seleccion = self.tree_flota.selection()
        if not seleccion:
            return
        uid = self.tree_flota.item(seleccion[0])["values"][0]
        self.unidad_seleccionada_id = uid
        self._mostrar_detalle_flota(uid)

    def _on_click_marcar_libre(self):
        if not self.unidad_seleccionada_id or self.unidad_seleccionada_id not in self.flota_info:
            messagebox.showwarning("Atención", "Seleccioná primero un móvil en la lista o en el mapa.")
            return

        info = self.flota_info[self.unidad_seleccionada_id]
        if info["datos"].get("disponibilidad") != "ocupada":
            messagebox.showinfo("Información", f"La unidad {self.unidad_seleccionada_id} ya se encuentra libre.")
            return

        self.liberar_unidad(self.unidad_seleccionada_id, info["tipo"])

    def _on_click_centrar_flota(self):
        if not self.unidad_seleccionada_id or self.unidad_seleccionada_id not in self.flota_info:
            return
        datos = self.flota_info[self.unidad_seleccionada_id]["datos"]
        lat = datos.get("lat")
        lon = datos.get("lon")
        if lat is not None and lon is not None:
            self.mapa.set_position(lat, lon)
            self.mapa.set_zoom(16)

    def iniciar(self):
        """Inicia el bucle de eventos de la ventana gráfica."""
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
    entorno = config_entorno()

    parser = argparse.ArgumentParser(description="Despachador Oficial de Emergencias (Comodoro Rivadavia)")
    parser.add_argument("pos_broker", nargs="?", default=None, help="IP o host del broker (posicional)")
    parser.add_argument("pos_puerto", nargs="?", type=int, default=None, help="Puerto del broker (posicional)")
    parser.add_argument("--broker", type=str, default=None, help="IP del broker MQTT")
    parser.add_argument("--puerto", type=int, default=None, help="Puerto del broker MQTT (8883 para TLS, 1883 estándar)")
    parser.add_argument("--id", type=str, default=USUARIO_DEFAULT, help="Client ID del despachador")
    parser.add_argument("--usuario", type=str, default=USUARIO_DEFAULT, help="Usuario MQTT")
    parser.add_argument("--password", type=str, default=None, help="Contraseña MQTT")
    parser.add_argument("--ca-certs", type=str, default=entorno["ca_certs"] or CA_CERTS_DEFAULT, help="Ruta al archivo CA para TLS")
    parser.add_argument("--no-tls", action="store_true", help="Desactivar TLS y conectar en texto plano (puerto 1883)")
    parser.add_argument("--login", dest="login", action="store_true", default=True, help="Mostrar ventana de inicio de sesión con JWT al iniciar (por defecto)")
    parser.add_argument("--no-login", dest="login", action="store_false", help="Omitir pantalla de login e ingresar directamente")

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
        pedir_login=args.login,
    )
    app.iniciar()
