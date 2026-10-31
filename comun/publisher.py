import paho.mqtt.client as mqtt
from paho.mqtt.enums import MQTTProtocolVersion
import json
import logging

logger = logging.getLogger("publisher")

class MQTTPub():

    def __init__(self, ip_broker, port_broker, client_id,
        usuario=None, password=None, ca_certs=None, sesion_persistente=False,
        max_quued_messages=0, keepalive=60):

        self._ip_broker = ip_broker
        self._port_broker = port_broker
        self._keepalive = keepalive
        self._sesion_persistente = sesion_persistente
        self._suscripciones = {}

        #config cliente
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id = client_id,
            protocol= MQTTProtocolVersion.MQTTv5,
            reconnect_on_failure=True, #este val es x defecto
        )
        self._client.max_queued_messages = max_quued_messages #0 es ilimitado

        # Autenticacion: la consigna prohibe conexiones anonimas
        if usuario:
            self._client.username_pw_set(username=usuario, password=password)

        if ca_certs:
            self._client.tls_set(ca_certs=ca_certs)

        #callbacks importantes para un pub
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish


    def configurar_last_will(self, topic, payload, qos=1, retain=True):
        # Configura el mensaje de ultima voluntad (LWT) - DEBE llamarse ANTES de start()

        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        else:
            payload = str(payload)

        self._client.will_set(topic, payload, qos=qos, retain=retain)
        logger.debug("Last Will configurado en '%s'", topic)

    def subscribe(self, topic, callback=None, qos=1):
        self._suscripciones[topic] = (qos, callback)
        if callback:
            self._client.message_callback_add(topic, callback)
        if self._client.is_connected():
            self._client.subscribe(topic, qos=qos)

    def publish(self, dato, topic, qos=1, retain=False):

        if not topic:
            logger.warning("No declaraste topicos para publicar")
            return False #tecnicamente no sirve ponerle un valor, porque esta funcion la ejecuta paho no nosotros y paho no es que usa el valor retornado para algo

        try:
            payload = json.dumps(dato)
        except (TypeError, ValueError) as e:
            logger.exception("dato no serializable a JSON:%s",e)
            return False

        try:
            info_msg = self._client.publish(topic, payload, qos=qos, retain=retain) # este metodo devuelve un MQTTMessageInfo,
        except (TypeError, ValueError):
            logger.exception("publish con argumentos invalidos")
            return False

        if info_msg.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
            logger.error("cola de paho llena, mensaje descartado")
            return False

        if info_msg.rc == mqtt.MQTT_ERR_NO_CONN and qos == 0:
            logger.error("sin conexión, mensaje QoS 0 perdido")
            return False

        #not in err_success y err_no_conn porque el success asegura que o salio a la red o esta en la cola de pendientes del cliente
        #y el err no conn es para que cuando no hay conexion y la cola tiene lugar y el mesnaje es al QoS 1/2 entonces
        #lo deja en la cola del cliente y al reconectar los envia
        #pero con qos 0 si significa que se perdio con el err no conn por eso arriba esta ese condicional
        if info_msg.rc not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN):
            logger.error(f"publish fallo, rc: {info_msg.rc}")
            return False

        return True

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        if reason_code.is_failure:
            logger.error("Conexion rechazada: %s",reason_code)
            return

        logger.info("Conexion aceptada: %s", reason_code)
        logger.debug("Propiedades: %s", properties)

        if connect_flags.session_present:
            logger.info("Sesion persistente retomada")
        else:
            logger.info("no se encontro sesion previa (o expiro)")

        for topic, (qos, callback) in self._suscripciones.items():
            if callback:
                self._client.message_callback_add(topic, callback)
            self._client.subscribe(topic, qos=qos)



    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        logger.info("Desconectado: %s", reason_code)
        logger.debug("Propiedades: %s", properties)

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        if reason_code.is_failure:
            logger.error("el broker rechazo mid: %s, %s", mid, reason_code)
            return

        logger.info("Mensaje confirmado mid=%s", mid)


    def start(self):
        from paho.mqtt.properties import Properties
        from paho.mqtt.packettypes import PacketTypes

        clean_start = not self._sesion_persistente
        # Booleano para guardar información del estado ante caidas
        # True (despachador | servicio)

        connect_properties = None

        if self._sesion_persistente:
            connect_properties = Properties(PacketTypes.CONNECT)
            connect_properties.SessionExpiryInterval = 86400  # 86400 seg = 24 horas

        try:
            self._client.connect(
                self._ip_broker,
                self._port_broker,
                keepalive=self._keepalive,
                clean_start=clean_start,
                properties=connect_properties,
            )
        except OSError:
            logger.exception("No se pudo conectar al broker")
            raise
        #si pudimos conectarnos entonces iniciamos el loop con el hilo aparte
        self._client.loop_start()

    def stop(self):
        self._client.disconnect()
        self._client.loop_stop()
