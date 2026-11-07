"""Cliente MQTT base de todos los componentes Python (unidad, despachador, publico, servicio).

Maneja TLS, usuario y clave, Last Will, sesion persistente, suscripciones con callback
(se rehacen solas al reconectar) y reconexion automatica.

Ejemplo, con la conexion tomada de variables de entorno (ver config_entorno):

    from comun.publisher import MQTTPub, config_entorno
    from comun import topics, payloads

    def on_posicion(client, userdata, msg):
        tipo, unidad, _ = topics.parse_reporte(msg.topic)
        datos = payloads.validar(payloads.POSICION, msg.payload)

    cli = MQTTPub(client_id="servicio", sesion_persistente=True, expiracion_sesion=86400,
                  **config_entorno())
    cli.configurar_last_will(topics.SERVICIO_ESTADO, payloads.servicio_estado("offline"))
    cli.publicar_al_conectar(payloads.servicio_estado("online"), topics.SERVICIO_ESTADO)
    cli.subscribe(topics.SUB_POSICIONES, callback=on_posicion, qos=1)
    cli.start()
"""

import paho.mqtt.client as mqtt
from paho.mqtt.enums import MQTTProtocolVersion
import json
import logging
import os

logger = logging.getLogger("publisher")


def config_entorno():
    """Lee la conexion de variables de entorno, asi no se hardcodean host ni claves.

    MQTT_HOST (127.0.0.1), MQTT_PORT (8883), MQTT_USUARIO, MQTT_PASSWORD, MQTT_CA_CERTS.
    Devuelve los kwargs de conexion de MQTTPub: MQTTPub(client_id=..., **config_entorno())
    """
    return {
        "ip_broker": os.getenv("MQTT_HOST", "127.0.0.1"),
        "port_broker": int(os.getenv("MQTT_PORT", "8883")),
        "usuario": os.getenv("MQTT_USUARIO"),
        "password": os.getenv("MQTT_PASSWORD"),
        "ca_certs": os.getenv("MQTT_CA_CERTS"),
    }


class MQTTPub():

    def __init__(self, ip_broker, port_broker, client_id,
        usuario=None, password=None, ca_certs=None, sesion_persistente=False,
        expiracion_sesion=3600, max_quued_messages=0, keepalive=60):

        self._ip_broker = ip_broker
        self._port_broker = port_broker
        self._keepalive = keepalive
        self._sesion_persistente = sesion_persistente
        self._expiracion_sesion = expiracion_sesion #segundos, solo si la sesion es persistente (docs/topics.md: servicio 24 h, despachador 1 h)
        self._suscripciones = {}
        self._al_conectar = [] #mensajes que se publican en cada conexion, ej "online"

        #config cliente
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id = client_id,
            protocol= MQTTProtocolVersion.MQTTv5,
            reconnect_on_failure=True, #este val es x defecto
        )
        self._client.max_queued_messages = max_quued_messages #0 es ilimitado
        #si se corta, reintenta cada 1 s, 2 s, 4 s... hasta 10 s, para que en la demo vuelva rapido
        self._client.reconnect_delay_set(min_delay=1, max_delay=10)

        # Autenticacion: la consigna prohibe conexiones anonimas
        if usuario:
            self._client.username_pw_set(username=usuario, password=password)

        if ca_certs:
            self._client.tls_set(ca_certs=ca_certs)

        #callbacks importantes para un pub
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish
        self._client.on_subscribe = self._on_subscribe


    def configurar_last_will(self, topic, payload, qos=1, retain=True):
        # Configura el mensaje de ultima voluntad (LWT) - DEBE llamarse ANTES de start()

        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        else:
            payload = str(payload)

        self._client.will_set(topic, payload, qos=qos, retain=retain)
        logger.debug("Last Will configurado en '%s'", topic)

    def publicar_al_conectar(self, dato, topic, qos=1, retain=True):
        # Mensaje que se publica en CADA conexion, no solo en la primera (el "online" que complementa al Last Will).
        # Si se publicara una sola vez despues de start(), al reconectar tras una caida el broker
        # ya publico el Last Will "offline" (retained) y nadie lo volveria a poner en "online".
        self._al_conectar.append((dato, topic, qos, retain))
        if self._client.is_connected():
            self.publish(dato, topic, qos=qos, retain=retain)

    def _callback_protegido(self, callback):
        #una excepcion en un callback mata el hilo de red de paho y el proceso sigue vivo sin atender mensajes,
        #asi que Docker no lo reinicia. Se descarta el mensaje: paho lo confirma igual y el broker no lo reentrega
        def protegido(client, userdata, msg):
            try:
                callback(client, userdata, msg)
            except Exception:
                logger.exception("Error procesando un mensaje de '%s', se descarta", msg.topic)
        return protegido

    def subscribe(self, topic, callback=None, qos=1):
        self._suscripciones[topic] = (qos, callback)
        if callback:
            self._client.message_callback_add(topic, self._callback_protegido(callback))
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
            #debug: mientras reconecta es lo esperable (la unidad manda una posicion por segundo)
            logger.debug("sin conexión, mensaje QoS 0 perdido")
            return False

        #not in err_success y err_no_conn porque el success asegura que o salio a la red o esta en la cola de pendientes del cliente
        #y el err no conn es para que cuando no hay conexion y la cola tiene lugar y el mesnaje es al QoS 1/2 entonces
        #lo deja en la cola del cliente y al reconectar los envia
        #pero con qos 0 si significa que se perdio con el err no conn por eso arriba esta ese condicional
        if info_msg.rc not in (mqtt.MQTT_ERR_SUCCESS, mqtt.MQTT_ERR_NO_CONN):
            logger.error("publish fallo, rc: %s", info_msg.rc)
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

        #nos resuscribimos siempre: sin sesion (primera vez, vencio, broker recreado) no hay suscripciones,
        #y con sesion no se pierde nada y el broker reenvia los retained (asi el despachador recibe la foto
        #actual de flota/). Ver docs/topics.md, "Retained y sesion persistente".
        #los callbacks ya quedaron registrados en subscribe()
        for topic, (qos, _) in self._suscripciones.items():
            self._client.subscribe(topic, qos=qos)

        for dato, topic, qos, retain in self._al_conectar: #al conectar publicamos que estamos online
            self.publish(dato, topic, qos=qos, retain=retain)



    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties):
        #si la ACL niega una suscripcion, el broker igual responde el SUBACK pero con codigo de error;
        #sin esto el cliente no se entera y simplemente nunca le llegan mensajes de ese topico
        for rc in reason_code_list:
            if rc.is_failure:
                logger.error("el broker rechazo la suscripcion mid=%s: %s", mid, rc)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        logger.info("Desconectado: %s", reason_code)
        logger.debug("Propiedades: %s", properties)

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        if reason_code.is_failure:
            logger.error("el broker rechazo mid: %s, %s", mid, reason_code)
            return

        #debug y no info: con 15 unidades reportando cada segundo llena el log
        logger.debug("Mensaje confirmado mid=%s", mid)


    def start(self):
        from paho.mqtt.properties import Properties
        from paho.mqtt.packettypes import PacketTypes

        clean_start = not self._sesion_persistente
        # Booleano para guardar información del estado ante caidas
        # True (despachador | servicio)

        connect_properties = None

        if self._sesion_persistente:
            connect_properties = Properties(PacketTypes.CONNECT)
            connect_properties.SessionExpiryInterval = self._expiracion_sesion

        #connect_async no se bloquea ni tira excepcion si el broker no esta: el hilo de loop_start
        #reintenta la primera conexion igual que las reconexiones. Con connect() sincronico,
        #un cliente que arranca antes que el broker se cerraba en vez de esperar.
        self._client.connect_async(
            self._ip_broker,
            self._port_broker,
            keepalive=self._keepalive,
            clean_start=clean_start,
            properties=connect_properties,
        )
        self._client.loop_start()
        logger.info("Conectando a %s:%s (reintenta solo si el broker no responde)", self._ip_broker, self._port_broker)

    def stop(self, con_last_will=False):
        #con_last_will: desconexion limpia pero pidiendole al broker que publique el Last Will igual
        #(reason code 0x04). Sirve para que la unidad quede "offline" al cerrarse bien sin publicarlo a mano
        reason = None
        if con_last_will:
            from paho.mqtt.reasoncodes import ReasonCode
            from paho.mqtt.packettypes import PacketTypes
            reason = ReasonCode(PacketTypes.DISCONNECT, identifier=0x04)
        self._client.disconnect(reasoncode=reason)
        self._client.loop_stop()
