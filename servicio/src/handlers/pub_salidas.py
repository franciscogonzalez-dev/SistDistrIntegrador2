#archivo para contener metodos publish que utilizan los handlers
#son metodos que publican en topicos de Salida

import logging
from comun import payloads, topics
import time
logger = logging.getLogger("servicio.flota")

#sirve para publicar el estado oficial de la unidda en flota/{tipo}/{unidad}/estado
#no es directamente un handler sino una funcion que usa los handlers 
def publicar_flota(cliente, bd, tipo, unidad):
    #se llama despues del commit, asi nunca se publica algo que no quedo guardado
    fila = bd.leer_unidad(unidad)
    if fila is None:
        return
    estado = payloads.flota_estado(
        conexion=fila["conexion"],
        disponibilidad="ocupada" if fila["eid"] else "libre",
        lat=fila["lat"],
        lon=fila["lon"],
        rumbo=fila["rumbo"],
        eid=fila["eid"],
        ts=fila["ts_posicion"], #sin posicion todavia es None y el builder pone la hora actual
    )
    topic_flota = topics.flota_estado(tipo, unidad)
    cliente.publish(estado, topic=topic_flota, qos=1, retain=True)
    logger.info("Publicado estado oficial en %s", topic_flota)

def publicar_emergencia(cliente, bd, tipo, eid):
    fila = bd.leer_emergencia(eid)
    if fila is None:
        return
    
    estado = payloads.emergencia(
        eid= fila['eid'], 
        lat= fila['lat'], 
        lon= fila['lon'], 
        estado= 'asignada' if fila['unidad'] else 'sin_asignar', 
        unidad= fila['unidad'], 
        ts=fila['ts']
    )
    topic_emerg = topics.emergencia(tipo, eid)
    cliente.publish(estado, topic=topic_emerg, qos=1, retain=True)
    logger.info("Emergencia publicada en %s", topic_emerg)
    
    
def publicar_alerta(cliente, tipo, motivo, mensaje, unidad, eid):
    estado = payloads.alerta(
        motivo, 
        mensaje, 
        unidad=unidad, 
        eid=eid, 
        ts=time.time()
    )
    topcis_alerta = topics.alertas(tipo)
    cliente.publish(estado, topic=topcis_alerta, qos=1)
    logger.info("Alerta publicada en %s",topcis_alerta)