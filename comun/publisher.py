import paho.mqtt.client as mqtt
from paho.mqtt.enums import MQTTProtocolVersion
import json
import logging

logger = logging.getLogger("publisher")

class MQTTPub():

    def __init__(self, ip_broker, port_broker, client_id, max_quued_messages=0, keepalive=60):
        self._ip_broker = ip_broker
        self._port_broker = port_broker
        
        #config cliente
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id = client_id,
            protocol= MQTTProtocolVersion.MQTTv5,
            reconnect_on_failure=True, #este val es x defecto
            keepalive = keepalive
        )
        self._client.max_queued_messages = max_quued_messages #0 es ilimitado
        
        #callbacks importantes para un pub
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish
        
        
    def publish(self, dato, topic, qos=1):
        
        if not topic:
            logger.warning("No declaraste topicos para publicar")
            return False #tecnicamente no sirve ponerle un valor, porque esta funcion la ejecuta paho no nosotros y paho no es que usa el valor retornado para algo

        try:            
            payload = json.dumps(dato)
        except (TypeError, ValueError) as e:
            logger.exception("dato no serializable a JSON:%s",e)
            return False

        try: 
            info_msg = self._client.publish(topic, payload, qos=qos) # este metodo devuelve un MQTTMessageInfo, 
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
            
         
    
    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        logger.info("Desconectado: %s", reason_code)
        logger.debug("Propiedades: %s", properties)
    
    def _on_publish(self, client, userdata, mid, reason_code, properties):
        if reason_code.is_failure:
            logger.error("el broker rechazo mid: %s, %s", mid, reason_code)
            return 

        logger.info("Mensaje confirmado mid=%s", mid)
         
    
    def start(self):
        try:
            self._client.connect(self._ip_broker, self._port_broker)
        except OSError:
            logger.exception("No se pudo conectar al broker")
            raise
        #si pudimos conectarnos entonces iniciamos el loop con el hilo aparte
        self._client.loop_start()
        
    def stop(self):
        self._client.disconnect()
        self._client.loop_stop()