"""App del público: pedir ayuda y aviso de servicio caído (Issue #16).

Simula la app de emergencias del ciudadano civil. Tres botones grandes
(Policía, Ambulancia, Bomberos) para pedir auxilio inmediato.

El público NO ve unidades ni emergencias (por ACL solo puede leer
servicio/estado y publicar en pedidos/{clientid}/{tipo}).

Hereda de MQTTPub (comun/publisher.py), que maneja TLS, autenticación,
reconexión automática y los problemas de hilos de paho.

Dos modos de ejecución:
  python clientes/publico.py                    → modo civil (ubicación fija, transparente)
  python clientes/publico.py --prueba           → modo demo (selector de 10 puntos de Comodoro)

Cada instancia abierta a la vez necesita su propio Client ID: python clientes/publico.py --perfil 2

La clave se lee de clientes/credenciales.json (ver credenciales.example.json).
Host, puerto y CA se pueden pasar por argumento o por las variables MQTT_HOST, MQTT_PORT y MQTT_CA_CERTS.
"""

import sys
import re
import uuid
import time
import queue
import argparse
import logging
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk

# Asegurar que la raíz del proyecto y 'clientes' estén en el path
ROOT_DIR = Path(__file__).resolve().parent.parent
CLIENTES_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(CLIENTES_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTES_DIR))

from comun.publisher import MQTTPub, config_entorno
from comun import topics, payloads
from credenciales import clave

logger = logging.getLogger("publico")

CA_CERTS_DEFAULT = str(ROOT_DIR / "broker" / "certs" / "ca.crt")
ARCHIVO_CLIENT_ID = CLIENTES_DIR / ".client_id_publico"

# Ubicación por defecto: Centro de Comodoro Rivadavia (Plaza San Martín)
LAT_DEFAULT = -45.8641
LON_DEFAULT = -67.4850

# Tocar otra vez el mismo pedido sin confirmar, o hasta este tiempo después de confirmado,
# no es una emergencia nueva: un doble clic no tiene que mandar dos unidades
VENTANA_REPETICION_SEG = 30.0
# Si tras conectar no llega servicio/estado (sin retained: el servicio nunca arrancó), se avisa igual
ESPERA_ESTADO_MS = 5000
# PUBACK "No matching subscribers": no es un error de MQTT, pero el pedido no le llegó a nadie.
# Pasa si el servicio nunca arrancó en este broker, porque todavía no tiene sesión persistente
RC_SIN_SUSCRIPTORES = 16

NOMBRES_TIPO = {"policia": "Policía", "ambulancia": "Ambulancia", "bomberos": "Bomberos"}
TELEFONOS = "📞 107 (Salud)  📞 101 (Policía)  📞 100 (Bomberos)"

# Puntos de prueba para la demo (--prueba): lugares reales de Comodoro + casos borde
PUNTOS_PRUEBA = [
    ("📍 Centro (San Martín y Rivadavia)",        -45.8641, -67.4850),
    ("📍 Hospital Regional (Av. Yrigoyen)",       -45.8684, -67.4875),
    ("📍 Clínica del Valle (Av. Alsina)",         -45.8639, -67.4944),
    ("📍 Cuartel Central Bomberos (Huergo)",      -45.8587, -67.4890),
    ("📍 Av. Kennedy (Pueyrredón)",               -45.8628, -67.5155),
    ("📍 Km 3 – Hospital Alvear",                 -45.8344, -67.4619),
    ("📍 Km 5 – Barrio Presidente Ortiz",         -45.8200, -67.4700),
    ("📍 Km 8 – Don Bosco / Malvinas",            -45.7980, -67.4680),
    ("🌊 En el Mar (Golfo San Jorge)",            -45.8641, -67.4200),
    ("🚫 Fuera de Comodoro (Ruta 3 al norte)",    -45.7000, -67.5000),
]


# ─────────────────────────────────────────────────────────────────────
# Persistencia del Client ID (simula el id del celular)
# ─────────────────────────────────────────────────────────────────────

def obtener_client_id(perfil=None):
    """Lee el client_id guardado o genera uno nuevo y lo persiste.

    Formato: publico-<32 hex> (requerido por el autenticador cinfo de EMQX).
    Cada perfil simula otro celular: dos instancias con el mismo Client ID
    se echarían entre sí del broker.
    """
    archivo = ARCHIVO_CLIENT_ID
    if perfil is not None:
        archivo = archivo.with_name(f"{archivo.name}_{perfil}")

    if archivo.exists():
        guardado = archivo.read_text().strip()
        if re.fullmatch(r"publico-[0-9a-fA-F]{32}", guardado):
            return guardado

    nuevo = f"publico-{uuid.uuid4().hex}"
    archivo.write_text(nuevo)
    logger.info("Client ID generado: %s", nuevo)
    return nuevo


# ─────────────────────────────────────────────────────────────────────
# Paleta de colores y constantes de diseño
# ─────────────────────────────────────────────────────────────────────

# Fondo general oscuro (simula app de celular)
COLOR_FONDO        = "#1a1a2e"
COLOR_FONDO_CLARO  = "#16213e"
COLOR_TEXTO        = "#eaeaea"
COLOR_TEXTO_TENUE  = "#8d99ae"

# Botones de emergencia
COLOR_POLICIA      = "#2563eb"   # azul
COLOR_POLICIA_H    = "#1d4ed8"   # hover
COLOR_AMBULANCIA   = "#dc2626"   # rojo
COLOR_AMBULANCIA_H = "#b91c1c"
COLOR_BOMBEROS     = "#ea580c"   # naranja
COLOR_BOMBEROS_H   = "#c2410c"

# Banner de estado
COLOR_ONLINE       = "#059669"   # verde esmeralda
COLOR_PENDIENTE    = "#d97706"   # ámbar / amarillo
COLOR_OFFLINE      = "#dc2626"   # rojo

# Confirmación de envío
COLOR_EXITO        = "#10b981"

FUENTE_TITULO      = ("Segoe UI", 16, "bold")
FUENTE_NORMAL      = ("Segoe UI", 11)
FUENTE_BOTON       = ("Segoe UI", 15, "bold")
FUENTE_CHICA       = ("Segoe UI", 9)
FUENTE_ESTADO      = ("Segoe UI", 10, "bold")


# ─────────────────────────────────────────────────────────────────────
# Cliente del público: hereda de MQTTPub y maneja la GUI
# ─────────────────────────────────────────────────────────────────────

class AppPublico(MQTTPub):
    """App del ciudadano civil. Hereda de MQTTPub para la conexión MQTT
    y maneja la interfaz gráfica Tkinter para el pedido de emergencias.

    Similar a como Unidad hereda de MQTTPub en clientes/unidad.py.
    """

    def __init__(self, client_id, ip_broker, puerto, password,
                 ca_certs=None, modo_prueba=False):
        self.client_id_publico = client_id
        self.modo_prueba = modo_prueba

        # Cola thread-safe y banderas de ciclo de vida (se definen antes de MQTTPub)
        self._cola_ui = queue.Queue()
        self._cerrando = False
        self._after_id = None
        self._after_estado = None
        self.root = None

        # Estado interno
        self.estado_servicio = "desconocido"
        self.estado_red = "conectando"
        self.ultimo_pedido = None
        self._pedidos_recientes = {}   # (tipo, lat, lon) -> pedido, para reconocer toques repetidos
        self._pendientes = {}          # mid -> pedido publicado todavía sin PUBACK
        self.lat_actual = LAT_DEFAULT
        self.lon_actual = LON_DEFAULT

        # Inicializar MQTTPub (sin sesión persistente, sin Last Will)
        # El público no necesita Last Will: no es un componente crítico del sistema,
        # a nadie le importa si se desconecta (docs/topics.md)
        super().__init__(
            ip_broker=ip_broker,
            port_broker=puerto,
            client_id=client_id,
            usuario="publico",
            password=password,
            sesion_persistente=False,
            ca_certs=ca_certs,
        )

        self._client.on_connect_fail = self._on_connect_fail

        # Suscripción a servicio/estado (único topic que el público puede leer)
        self.subscribe(topics.SERVICIO_ESTADO, callback=self._on_servicio_estado, qos=1)

    # ─────────────────── Callbacks MQTT (sobrescriben MQTTPub) ───────────────────

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        """Conexión aceptada: encola actualización para la UI y deja que MQTTPub resubscriba."""
        super()._on_connect(client, userdata, connect_flags, reason_code, properties)

        if not self._cerrando:
            if not reason_code.is_failure:
                self._cola_ui.put(("red", "conectado"))
            else:
                self._cola_ui.put(("red", "sin_red"))
                self._cola_ui.put(("banner", "offline"))

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """Desconexión: marca la red como caída y el servicio como inalcanzable."""
        super()._on_disconnect(client, userdata, disconnect_flags, reason_code, properties)

        if not self._cerrando:
            self._cola_ui.put(("red", "sin_red"))
            # Si el broker se cae, el servicio también está inalcanzable
            self._cola_ui.put(("banner", "offline"))

    def _on_connect_fail(self, client, userdata):
        """paho avisa por acá si no llega a conectar (broker caído, red, TLS): no pasa por _on_connect."""
        if not self._cerrando:
            self._cola_ui.put(("red", "sin_red"))
            self._cola_ui.put(("banner", "offline"))

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        """PUBACK del broker: recién acá se sabe que el pedido llegó (hilo MQTT → cola → hilo UI)."""
        super()._on_publish(client, userdata, mid, reason_code, properties)
        if not self._cerrando:
            perdido = reason_code.is_failure or reason_code.value == RC_SIN_SUSCRIPTORES
            self._cola_ui.put(("puback", (mid, perdido)))

    def _on_servicio_estado(self, _client, _userdata, msg):
        """Callback de servicio/estado (hilo MQTT → cola → hilo UI)."""
        if self._cerrando:
            return
        try:
            datos = payloads.validar(payloads.SERVICIO_ESTADO, msg.payload)
            estado = datos["estado"]
            self._cola_ui.put(("banner", estado))
        except payloads.PayloadInvalido as e:
            logger.warning("Payload inválido en servicio/estado: %s", e)

    # ─────────────────── Ventana ───────────────────

    def _configurar_ventana(self):
        self.root.title("911 Emergencias – Comodoro Rivadavia")
        self.root.configure(bg=COLOR_FONDO)
        self.root.resizable(False, False)

        ancho = 420
        alto = 680 if self.modo_prueba else 580
        self.root.geometry(f"{ancho}x{alto}")

        # Centrar en pantalla
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - ancho) // 2
        y = (self.root.winfo_screenheight() - alto) // 2
        self.root.geometry(f"+{x}+{y}")

    # ─────────────────── Construcción de la GUI ───────────────────

    def _construir_gui(self):
        main = tk.Frame(self.root, bg=COLOR_FONDO, padx=20, pady=12)
        main.pack(fill="both", expand=True)

        self._crear_encabezado(main)
        self._crear_banner(main)

        if self.modo_prueba:
            self._crear_selector_ubicacion(main)

        self._crear_botones_emergencia(main)
        self._crear_panel_confirmacion(main)

    def _crear_encabezado(self, parent):
        """Título de la app y estado de conexión."""
        enc = tk.Frame(parent, bg=COLOR_FONDO)
        enc.pack(fill="x", pady=(0, 8))

        tk.Label(
            enc, text="🆘  911 Emergencias", font=FUENTE_TITULO,
            bg=COLOR_FONDO, fg=COLOR_TEXTO, anchor="w",
        ).pack(side="left")

        self.lbl_red = tk.Label(
            enc, text="● Conectando…", font=FUENTE_CHICA,
            bg=COLOR_FONDO, fg=COLOR_TEXTO_TENUE, anchor="e",
        )
        self.lbl_red.pack(side="right")

        tk.Frame(parent, bg=COLOR_TEXTO_TENUE, height=1).pack(fill="x", pady=(0, 8))

    def _crear_banner(self, parent):
        """Cartel dinámico que muestra el estado del servicio central."""
        self.frame_banner = tk.Frame(parent, bg=COLOR_PENDIENTE, padx=2, pady=2)
        self.frame_banner.pack(fill="x", pady=(0, 10))

        self.banner_interior = tk.Frame(self.frame_banner, bg=COLOR_FONDO_CLARO, padx=12, pady=8)
        self.banner_interior.pack(fill="x")

        self.lbl_banner_icono = tk.Label(
            self.banner_interior, text="🟡", font=("Segoe UI", 14),
            bg=COLOR_FONDO_CLARO, fg=COLOR_TEXTO,
        )
        self.lbl_banner_icono.pack(side="left", padx=(0, 8))

        self.lbl_banner_texto = tk.Label(
            self.banner_interior, text="Conectando con el sistema central…",
            font=FUENTE_ESTADO, bg=COLOR_FONDO_CLARO, fg=COLOR_PENDIENTE,
            anchor="w", wraplength=320, justify="left",
        )
        self.lbl_banner_texto.pack(side="left", fill="x", expand=True)

    def _crear_selector_ubicacion(self, parent):
        """Combo para elegir la ubicación de prueba (solo con --prueba)."""
        frame_sel = tk.Frame(parent, bg=COLOR_FONDO_CLARO, padx=12, pady=10)
        frame_sel.pack(fill="x", pady=(0, 10))

        tk.Label(
            frame_sel, text="📍 Ubicación simulada (modo demo):",
            font=FUENTE_NORMAL, bg=COLOR_FONDO_CLARO, fg=COLOR_TEXTO, anchor="w",
        ).pack(fill="x", pady=(0, 6))

        opciones = [p[0] for p in PUNTOS_PRUEBA]
        self.combo_ubicacion = ttk.Combobox(
            frame_sel, values=opciones, state="readonly", font=FUENTE_CHICA,
        )
        self.combo_ubicacion.current(0)
        self.combo_ubicacion.pack(fill="x")
        self.combo_ubicacion.bind("<<ComboboxSelected>>", self._on_cambio_ubicacion)

        self.lbl_coords = tk.Label(
            frame_sel,
            text=self._texto_coords(PUNTOS_PRUEBA[0][1], PUNTOS_PRUEBA[0][2]),
            font=FUENTE_CHICA, bg=COLOR_FONDO_CLARO, fg=COLOR_TEXTO_TENUE, anchor="w",
        )
        self.lbl_coords.pack(fill="x", pady=(4, 0))

    def _crear_botones_emergencia(self, parent):
        """Los tres botones principales de auxilio (nunca se deshabilitan)."""
        tk.Label(
            parent, text="Seleccioná tu emergencia:", font=FUENTE_NORMAL,
            bg=COLOR_FONDO, fg=COLOR_TEXTO_TENUE, anchor="w",
        ).pack(fill="x", pady=(4, 8))

        botones = [
            ("🚓   POLICÍA",    "policia",    COLOR_POLICIA,    COLOR_POLICIA_H),
            ("🚑   AMBULANCIA", "ambulancia", COLOR_AMBULANCIA, COLOR_AMBULANCIA_H),
            ("🚒   BOMBEROS",   "bomberos",   COLOR_BOMBEROS,   COLOR_BOMBEROS_H),
        ]

        for texto, tipo, color, color_hover in botones:
            btn = tk.Button(
                parent, text=texto, font=FUENTE_BOTON,
                bg=color, fg="white", activebackground=color_hover, activeforeground="white",
                relief="flat", cursor="hand2", height=2,
                command=lambda t=tipo: self._enviar_pedido(t),
            )
            btn.pack(fill="x", pady=4, ipady=2)

            btn.bind("<Enter>", lambda e, b=btn, c=color_hover: b.configure(bg=c))
            btn.bind("<Leave>", lambda e, b=btn, c=color: b.configure(bg=c))

    def _crear_panel_confirmacion(self, parent):
        """Muestra el resultado del último pedido y el botón de reenvío."""
        tk.Frame(parent, bg=COLOR_TEXTO_TENUE, height=1).pack(fill="x", pady=(12, 8))

        self.frame_confirm = tk.Frame(parent, bg=COLOR_FONDO)
        self.frame_confirm.pack(fill="x")

        self.lbl_confirm = tk.Label(
            self.frame_confirm,
            text="Presioná un botón de emergencia para pedir auxilio.",
            font=FUENTE_NORMAL, bg=COLOR_FONDO, fg=COLOR_TEXTO_TENUE,
            wraplength=370, justify="left", anchor="w",
        )
        self.lbl_confirm.pack(fill="x")

        self.btn_reenviar = tk.Button(
            self.frame_confirm, text="🔄  Reenviar último aviso",
            font=FUENTE_NORMAL, bg=COLOR_FONDO_CLARO, fg=COLOR_TEXTO,
            activebackground="#2d3a5e", activeforeground=COLOR_TEXTO,
            relief="flat", cursor="hand2",
            command=self._reenviar_pedido,
        )
        # No se muestra hasta el primer pedido

    # ─────────────────── Acciones ───────────────────

    def _on_cambio_ubicacion(self, _event):
        """Callback del combo de ubicaciones (modo --prueba)."""
        idx = self.combo_ubicacion.current()
        _, lat, lon = PUNTOS_PRUEBA[idx]
        self.lat_actual = lat
        self.lon_actual = lon
        self.lbl_coords.configure(text=self._texto_coords(lat, lon))

    def _enviar_pedido(self, tipo):
        """Publica un pedido de auxilio, salvo que sea un toque repetido del anterior."""
        clave_pedido = (tipo, self.lat_actual, self.lon_actual)
        anterior = self._pedidos_recientes.get(clave_pedido)
        if anterior is not None and self._es_repeticion(anterior):
            logger.info("Toque repetido ignorado: tipo=%s, id=%s", tipo, anterior["dato"]["id"])
            self.ultimo_pedido = anterior
            self._mostrar_estado_pedido()
            return

        pedido = {
            "tipo": tipo,
            "dato": payloads.pedido(lat=self.lat_actual, lon=self.lon_actual, id=payloads.nuevo_id()),
            "estado": "pendiente",
            "hora": datetime.now().strftime("%H:%M:%S"),
            "confirmado_en": None,
        }
        self._pedidos_recientes[clave_pedido] = pedido
        self._publicar_pedido(pedido)

    def _es_repeticion(self, pedido):
        if pedido["estado"] == "fallido":
            return False
        if pedido["estado"] == "pendiente":
            return True
        return time.monotonic() - pedido["confirmado_en"] < VENTANA_REPETICION_SEG

    def _reenviar_pedido(self):
        """Reenvía el último pedido tal cual se armó: mismo id, ubicación y hora (idempotencia)."""
        if self.ultimo_pedido:
            self._publicar_pedido(self.ultimo_pedido)

    def _publicar_pedido(self, pedido):
        """Publica en pedidos/{clientid}/{tipo} con QoS 1 y sigue su PUBACK."""
        topic = topics.pedido(self.client_id_publico, pedido["tipo"])
        self.ultimo_pedido = pedido

        if self.publish(pedido["dato"], topic, qos=1, retain=False):
            pedido["estado"] = "pendiente"
            self._pendientes[self.ultimo_mid] = pedido
            logger.info("Pedido publicado: tipo=%s, id=%s, topic=%s", pedido["tipo"], pedido["dato"]["id"], topic)
        else:
            # publish en False significa que paho no lo aceptó: no quedó en ninguna cola
            pedido["estado"] = "fallido"
            logger.error("Pedido perdido: tipo=%s, id=%s", pedido["tipo"], pedido["dato"]["id"])

        self._mostrar_estado_pedido()
        self.btn_reenviar.pack(fill="x", pady=(8, 0), ipady=4)

    def _procesar_puback(self, mid, fallo):
        """Actualiza el pedido según el PUBACK (hilo UI, donde también se registró el mid)."""
        pedido = self._pendientes.pop(mid, None)
        if pedido is None:
            return

        if not fallo:
            pedido["estado"] = "enviado"
            pedido["confirmado_en"] = time.monotonic()
        elif pedido["estado"] == "pendiente":
            pedido["estado"] = "fallido"

        if pedido is self.ultimo_pedido:
            self._mostrar_estado_pedido()

    def _mostrar_estado_pedido(self):
        """Muestra el estado real del último pedido: solo el PUBACK lo da por enviado."""
        pedido = self.ultimo_pedido
        if pedido is None:
            return

        nombre = NOMBRES_TIPO[pedido["tipo"]]
        if pedido["estado"] == "fallido":
            self.lbl_confirm.configure(
                text=f"❌  No se pudo enviar la solicitud de {nombre}.\n"
                     f"Probá de nuevo; si es urgente llamá:\n{TELEFONOS}",
                fg=COLOR_OFFLINE,
            )
        elif pedido["estado"] == "enviado":
            self.lbl_confirm.configure(
                text=f"✅  Solicitud de {nombre} enviada ({pedido['hora']} hs).\n"
                     f"Tu ubicación fue adjuntada automáticamente.",
                fg=COLOR_EXITO,
            )
        elif self.estado_red == "conectado":
            self.lbl_confirm.configure(
                text=f"📤  Enviando solicitud de {nombre} ({pedido['hora']} hs)…",
                fg=COLOR_TEXTO_TENUE,
            )
        else:
            self.lbl_confirm.configure(
                text=f"⏳  Solicitud de {nombre} en cola ({pedido['hora']} hs).\n"
                     f"Se enviará automáticamente cuando se restablezca la conexión.",
                fg=COLOR_TEXTO_TENUE,
            )

    # ─────────────────── Actualización de la UI ───────────────────

    def _actualizar_red(self, estado):
        """Actualiza el indicador de red en el encabezado (hilo UI)."""
        self.estado_red = estado
        if estado == "conectado":
            self.lbl_red.configure(text="● Conectado", fg=COLOR_ONLINE)
            self._esperar_estado_servicio()
        else:
            self.lbl_red.configure(text="● Sin conexión", fg=COLOR_OFFLINE)
        self._mostrar_estado_pedido()

    def _esperar_estado_servicio(self):
        """Si no llega servicio/estado después de conectar, no se deja al ciudadano sin los teléfonos."""
        self.estado_servicio = "desconocido"
        if self._after_estado:
            self.root.after_cancel(self._after_estado)
        self._after_estado = self.root.after(ESPERA_ESTADO_MS, self._sin_estado_servicio)

    def _sin_estado_servicio(self):
        self._after_estado = None
        if self.estado_servicio == "desconocido":
            self._actualizar_banner("offline")

    def _actualizar_banner(self, estado):
        """Actualiza el cartel de estado del servicio central (hilo UI)."""
        self.estado_servicio = estado

        if estado == "online":
            self.frame_banner.configure(bg=COLOR_ONLINE)
            self.lbl_banner_icono.configure(text="✅")
            self.lbl_banner_texto.configure(
                text="Sistema central operativo",
                fg=COLOR_ONLINE,
            )
        else:
            self.frame_banner.configure(bg=COLOR_OFFLINE)
            self.lbl_banner_icono.configure(text="⚠️")
            self.lbl_banner_texto.configure(
                text="El sistema está con problemas: tu pedido se\n"
                     "procesará al reconectar; si es urgente llamá:\n"
                     f"{TELEFONOS}",
                fg=COLOR_OFFLINE,
            )

    def _poll_mqtt(self):
        """Vacía la cola de eventos MQTT desde el hilo seguro de Tkinter."""
        if self._cerrando or not self.root:
            return

        while True:
            try:
                tipo_evento, valor = self._cola_ui.get_nowait()
            except queue.Empty:
                break

            if tipo_evento == "red":
                self._actualizar_red(valor)
            elif tipo_evento == "banner":
                self._actualizar_banner(valor)
            elif tipo_evento == "puback":
                self._procesar_puback(*valor)

        self._after_id = self.root.after(100, self._poll_mqtt)

    # ─────────────────── Ciclo de vida ───────────────────

    def iniciar(self):
        """Crea la ventana primero, luego arranca MQTT y entra al mainloop."""
        self.root = tk.Tk()
        self._configurar_ventana()
        self._construir_gui()

        # Cierre limpio con la X de la ventana
        self.root.protocol("WM_DELETE_WINDOW", self._al_cerrar_ventana)

        # Arrancar el hilo MQTT una vez que la UI y la cola están listas
        self.start()
        self._poll_mqtt()

        print(f"[publico] Client ID: {self.client_id_publico}")
        print(f"[publico] Modo: {'demo (--prueba)' if self.modo_prueba else 'civil'}")

        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.detener()

    def _al_cerrar_ventana(self):
        """Los pedidos sin PUBACK solo están en memoria: cerrar los pierde, así que se avisa antes."""
        if self._pendientes and not messagebox.askyesno(
            "Pedido sin enviar",
            "Tenés un pedido que todavía no llegó al sistema y se pierde si cerrás.\n"
            f"Si es urgente llamá:\n{TELEFONOS}\n\n¿Cerrar igual?",
            icon="warning",
        ):
            return
        self.detener()

    def detener(self):
        """Cierre limpio: cancela el polling, desconecta MQTT y destruye la ventana."""
        if self._cerrando:
            return
        self._cerrando = True

        # 1. Cancelar el timer de Tkinter para que no se reprograme
        if self._after_id and self.root:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self._after_estado and self.root:
            try:
                self.root.after_cancel(self._after_estado)
            except Exception:
                pass
            self._after_estado = None

        # 2. Frenar el hilo MQTT (como _cerrando=True, los callbacks no tocarán nada)
        try:
            self.stop()
        except Exception as e:
            logger.debug("Error al detener MQTT: %s", e)

        # 3. Salir del mainloop y destruir la ventana de forma segura
        if self.root:
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                pass
            self.root = None

    @staticmethod
    def _texto_coords(lat, lon):
        return f"Lat: {lat:.4f}  |  Lon: {lon:.4f}"


# ─────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

    entorno = config_entorno()

    parser = argparse.ArgumentParser(
        description="App de Emergencias 911 para el ciudadano civil (Comodoro Rivadavia)",
    )
    parser.add_argument(
        "--prueba", "--pruebas", action="store_true", dest="prueba",
        help="Modo demo: habilita un selector con 10 ubicaciones de prueba",
    )
    parser.add_argument(
        "--perfil", type=int, default=None,
        help="Número de celular simulado: cada instancia abierta a la vez necesita uno distinto",
    )
    parser.add_argument("--broker", type=str, default=entorno["ip_broker"], help="IP del broker MQTT")
    parser.add_argument("--puerto", type=int, default=None, help="Puerto del broker MQTT")
    parser.add_argument("--password", type=str, default=None, help="Contraseña MQTT del usuario publico")
    parser.add_argument("--ca-certs", type=str, default=entorno["ca_certs"] or CA_CERTS_DEFAULT, help="Ruta al certificado CA")
    parser.add_argument("--no-tls", action="store_true", help="Conectar sin TLS (puerto 1883)")
    args = parser.parse_args()

    # Resolver puerto y TLS
    if args.no_tls:
        puerto = args.puerto or 1883
        ca = None
    else:
        puerto = args.puerto or entorno["port_broker"]
        ca = args.ca_certs
        if ca and not Path(ca).exists():
            print(f"[publico] No existe el certificado CA: {ca} (para probar sin TLS: --no-tls)")
            sys.exit(1)

    client_id = obtener_client_id(args.perfil)
    password_final = args.password if args.password else clave("publico")

    app = AppPublico(
        client_id=client_id,
        ip_broker=args.broker,
        puerto=puerto,
        password=password_final,
        ca_certs=ca,
        modo_prueba=args.prueba,
    )

    print(f"[publico] Broker: {args.broker}:{puerto} ({'TLS' if ca else 'sin TLS'})")
    app.iniciar()


if __name__ == "__main__":
    main()
