"""Handler de reportes de presencia (reportes/{tipo}/{unidad}/presencia).
"""

import logging
from comun import payloads
import sqlite3
from . import pub_salidas
logger = logging.getLogger("servicio.presencia")


def procesar(cliente, bd, tipo, unidad, raw_payload):
    """Procesa el estado de conexión (online/offline vía LWT) de una unidad."""
    try:
        datos = payloads.validar(payloads.PRESENCIA, raw_payload)
        logger.info(
            "Presencia recibida de %s (%s): estado=%s",
            unidad,
            tipo,
            datos["estado"],
        )
        
        #leemos la unidad en donde se tiene el estado de presencia de la unidad en la bd, si difiere del recibido por mensaje actualizamos
        resu=bd.leer_unidad(unidad)
    
        #si la unidad no existe en la bd es como si estuviera offline (el default de la tabla)
        conexion_anterior = "offline" if resu is None else resu['conexion']

        
        if conexion_anterior != datos['estado']:
            #solo si hubo un cambio de estado x ej online -> offline o al reves, actualizamos la presencia
            #y desasignamos la emergencia solo si ahora se encuentra offline y tiene una emergencia asignaca, x ej tras una caida
            
            eid = None if resu is None else resu['eid'] #si se desasigna pasaria a ser null, por ende perderiamos el id de la emergencia para publciar al emergencia y la alerta, por eso la guardamos aca
            se_cae_ocupada = datos['estado'] == 'offline' and eid #si ahora estas offline y tenias emergencia asignada, entonces se 
            
            with bd.transaccion():
                bd.actualizar_presencia(unidad, tipo, datos['estado'])
                if se_cae_ocupada:
                    bd.desasignar_unidad(unidad)
             
            #solo publicamos si hubo un cambio de estado
            #publicacion oficial en flota/{tipo}/{unidad}/estado (QoS 1, retained=True)
            pub_salidas.publicar_flota(cliente, bd, tipo, unidad)
            
            #solo si tenia emergencia y hubo cambio de estado, publicamos su designacion
            if se_cae_ocupada:
                #publicamos la desasignacion de la emergencia 
                pub_salidas.publicar_emergencia(cliente, bd, tipo, eid)
                
                #publicamos la alerta que se cayo la unidad
                motivo= "unidad_desconectada"
                mensaje=f"{unidad} se desconecto con {eid} en curso"
                pub_salidas.publicar_alerta(cliente, tipo, motivo, mensaje, unidad, eid)
            
            
        return datos
    except payloads.PayloadInvalido as e:
        logger.warning("Presencia inválida de %s: %s", unidad, e)
        return None
    except sqlite3.Error:
        logger.exception("No se pudo actualizar la presencia de %s", unidad)
        return None