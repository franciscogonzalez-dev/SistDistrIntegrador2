"""
Este servicio es el ÚNICO componente que consolida el estado oficial del sistema:
1. Se conecta al broker MQTT con Client ID 'servicio', usuario 'servicio' y sesión persistente (24 h).
2. Configura Last Will (LWT) en 'servicio/estado' con payload 'offline' (QoS 1, retained).
3. dPublica 'servicio/estado' = 'online' (QoS 1, retained) en cada conexión.
4. Se suscribe a los 4 canales de entrada del sistema:
   - 'reportes/+/+/posicion' (telemetría de móviles, QoS 0).
   - 'reportes/+/+/presencia' (estado de conexión LWT de móviles, QoS 1).
   - 'comandos/+/+/+' (órdenes de asignación y liberación del despachador, QoS 1).
   - 'pedidos/+/+' (solicitudes de emergencia de la ciudadanía, QoS 1).
5. Extrae la identidad de los tópicos (según contrato en docs/topics.md) y deriva
   el procesamiento a los handlers modulares.
"""

import sys
import os
import signal
import time
import logging
import argparse
from pathlib import Path

# Agregar raíz del proyecto a sys.path para encontrar 'comun'
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from comun.publisher import MQTTPub, config_entorno
from comun import topics, payloads
try:
    from servicio.src.handlers import posicion, presencia, comandos, pedidos
except ModuleNotFoundError:
    from handlers import posicion, presencia, comandos, pedidos

try:
    from servicio.src.bd import BD
except ModuleNotFoundError:
    from bd import BD

# Configuración de logging estándar
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Servicio] %(message)s",
)
logger = logging.getLogger("servicio")

CLIENT_ID_SERVICIO = "servicio"
USUARIO_DEFAULT = "servicio"
EXPIRACION_SESION_SEG = 86400  # 24 horas (docs/topics.md: no pierde pedidos ni comandos si se reinicia)
CA_CERTS_DEFAULT = "broker/certs/ca.crt"

RUTA_BD = os.getenv("SERVICIO_DB", "servicio/data/bd_servicio.db")#al dockerizar se modifica la ruta

class ServicioCentral:
    """Núcleo central del sistema distribuido de emergencias."""

    def __init__(
        self,
        ip_broker="127.0.0.1",
        puerto=8883,
        usuario=USUARIO_DEFAULT,
        password=None,
        ca_certs=CA_CERTS_DEFAULT,
        ruta_bd=RUTA_BD
    ):
        self.ip_broker = ip_broker
        self.puerto = puerto
        self.usuario = usuario
        self.password = password
        self.activo = False
        try:
            self.bd = BD(ruta_bd)
        except Exception as e:
            logger.exception("Error al instanciar la base de datos: %s", e)
            raise #sin bd el servicio no funciona
        
        # Validación del certificado de CA si se usa TLS
        ca_file = None
        if ca_certs:
            if not Path(ca_certs).exists():
                logger.warning(
                    "No se encontró el certificado CA en '%s'. Si se prueba sin TLS use --no-tls",
                    ca_certs,
                )
            else:
                ca_file = str(ca_certs)

        # 1. Instanciar cliente MQTT base (comun/publisher.py)
        # Se configura sesión persistente de 24 h para que EMQX retenga comandos y pedidos
        # mientras el contenedor del servicio esté temporalmente inactivo.
        self.cliente = MQTTPub(
            ip_broker=self.ip_broker,
            port_broker=self.puerto,
            client_id=CLIENT_ID_SERVICIO,
            usuario=self.usuario,
            password=self.password,
            ca_certs=ca_file,
            sesion_persistente=True,
            expiracion_sesion=EXPIRACION_SESION_SEG,
        )

        # 2. Configurar Last Will and Testament (LWT) antes de conectar (Req. 3)
        # Si el proceso muere de forma imprevista, EMQX publica 'offline' con retained
        self.cliente.configurar_last_will(
            topic=topics.SERVICIO_ESTADO,
            payload=payloads.servicio_estado("offline"),
            qos=1,
            retain=True,
        )

        # 3. Publicación automática al conectar o reconectar
        # Deja el estado oficial en 'online' para despachadores y público civil
        self.cliente.publicar_al_conectar(
            dato=payloads.servicio_estado("online"),
            topic=topics.SERVICIO_ESTADO,
            qos=1,
            retain=True,
        )

        # 4. Registrar suscripciones a los 4 canales de entrada
        # QoS 0 para telemetría continua (posiciones descartables)
        self.cliente.subscribe(topics.SUB_POSICIONES, callback=self._on_posicion, qos=0)
        # QoS 1 para eventos que no deben perderse (presencia, comandos y pedidos)
        self.cliente.subscribe(topics.SUB_PRESENCIAS, callback=self._on_presencia, qos=1)
        self.cliente.subscribe(topics.SUB_COMANDOS, callback=self._on_comando, qos=1)
        self.cliente.subscribe(topics.SUB_PEDIDOS, callback=self._on_pedido, qos=1)

    # ==========================================================================
    # ROUTERS Y CALLBACKS DE MENSAJES MQTT
    # ==========================================================================

    def _on_posicion(self, client, userdata, msg):
        """Maneja reportes de telemetría: reportes/{tipo}/{unidad}/posicion."""
        try:
            tipo, unidad, evento = topics.parse_reporte(msg.topic)
            if evento != "posicion":
                return
        except ValueError as e:
            logger.warning("Tópico de reporte inválido '%s': %s", msg.topic, e)
            return

        posicion.procesar(self.cliente, self.bd, tipo, unidad, msg.payload)

    def _on_presencia(self, client, userdata, msg):
        """Maneja eventos de conexión/desconexión: reportes/{tipo}/{unidad}/presencia."""
        try:
            tipo, unidad, evento = topics.parse_reporte(msg.topic)
            if evento != "presencia":
                return
        except ValueError as e:
            logger.warning("Tópico de reporte inválido '%s': %s", msg.topic, e)
            return

        presencia.procesar(self.cliente, self.bd, tipo, unidad, msg.payload)

    def _on_comando(self, client, userdata, msg):
        """Maneja órdenes del despachador: comandos/{despachador}/{tipo}/{unidad}."""
        try:
            despachador, tipo, unidad = topics.parse_comando(msg.topic)
        except ValueError as e:
            logger.warning("Tópico de comando inválido '%s': %s", msg.topic, e)
            return

        comandos.procesar(self.cliente, self.bd, despachador, tipo, unidad, msg.payload)

    def _on_pedido(self, client, userdata, msg):
        """Maneja pedidos de auxilio ciudadano: pedidos/{clientid}/{tipo}."""
        try:
            clientid, tipo = topics.parse_pedido(msg.topic)
        except ValueError as e:
            logger.warning("Tópico de pedido inválido '%s': %s", msg.topic, e)
            return

        pedidos.procesar(self.cliente, self.bd, clientid, tipo, msg.payload)

    # ==========================================================================
    # CICLO DE VIDA DEL SERVICIO
    # ==========================================================================

    def iniciar(self):
        """Inicia el cliente MQTT y mantiene el proceso activo."""
        logger.info(
            "Iniciando Servicio Central (Broker: %s:%s, TLS: %s, User: %s)...",
            self.ip_broker,
            self.puerto,
            "SÍ" if self.cliente._client._ssl_context else "NO",
            self.usuario or "anónimo",
        )
        self.activo = True
        self.cliente.start()
        logger.info("Servicio Central en ejecución. Esperando mensajes...")

        try:
            while self.activo:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Interrupción por teclado recibida.")
        finally:
            self.detener()

    def detener(self):
        """Detiene el servicio con desconexión limpia pero emitiendo Last Will 'offline'."""
        if not self.activo:
            return
        logger.info("Deteniendo Servicio Central...")
        self.activo = False
        # Desconexión con Reason Code 0x04 ("Disconnect with Will Message"):
        # Notifica inmediatamente a EMQX para que difunda 'servicio/estado' = 'offline'
        self.cliente.stop(con_last_will=True)
        self.bd.cerrar_conn()
        
        logger.info("Servicio Central detenido.")


# ==============================================================================
# ENTRADA PRINCIPAL
# ==============================================================================

if __name__ == "__main__":
    entorno = config_entorno()

    parser = argparse.ArgumentParser(description="Servicio Central (UNPSJB - Proyecto 2)")
    parser.add_argument("pos_broker", nargs="?", default=None, help="Host o IP del broker")
    parser.add_argument("pos_puerto", nargs="?", type=int, default=None, help="Puerto del broker")
    parser.add_argument("--broker", type=str, default=None, help="IP o host del broker MQTT")
    parser.add_argument("--puerto", type=int, default=None, help="Puerto del broker (8883 TLS / 1883 plano)")
    parser.add_argument("--usuario", type=str, default=entorno["usuario"] or USUARIO_DEFAULT, help="Usuario MQTT")
    parser.add_argument("--password", type=str, default=entorno["password"] or "cambiar", help="Contraseña MQTT")
    parser.add_argument("--ca-certs", type=str, default=entorno["ca_certs"] or CA_CERTS_DEFAULT, help="Ruta al ca.crt")
    parser.add_argument("--no-tls", action="store_true", help="Desactivar TLS y conectar en texto plano (puerto 1883)")

    args = parser.parse_args()

    broker = args.broker or args.pos_broker or entorno["ip_broker"]
    if args.no_tls:
        puerto = args.puerto or args.pos_puerto or 1883
        ca_certs = None
    else:
        puerto = args.puerto or args.pos_puerto or entorno["port_broker"]
        ca_certs = args.ca_certs

    servicio = ServicioCentral(
        ip_broker=broker,
        puerto=puerto,
        usuario=args.usuario,
        password=args.password,
        ca_certs=ca_certs,
    )

    # Manejar señales del sistema (Docker stop, kill, Ctrl+C)
    def manejar_senal(sig, frame):
        logger.info("Señal de terminación recibida (%s).", sig)
        servicio.detener()
        sys.exit(0)

    signal.signal(signal.SIGINT, manejar_senal)
    signal.signal(signal.SIGTERM, manejar_senal)

    servicio.iniciar()
