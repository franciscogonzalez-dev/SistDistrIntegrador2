import sqlite3
import logging
from pathlib import Path

#usamos sqlite ya que es una bd embebida facil de usar
# basicamente la bd vive en un archivo y nos conectamos a traves del script
#nos permite ejecutar logica de sql, sin necesitar de herramientas mas potentes como postgres o mysql

logger = logging.getLogger("servicio.bd")

class BD:
    TABLA_UNIDADES="""
        create table if not exists unidades(
            unidad TEXT PRIMARY KEY,
            tipo TEXT NOT NULL CHECK (tipo IN ('policia', 'ambulancia', 'bomberos')),
            conexion TEXT NOT NULL DEFAULT 'offline' CHECK (conexion IN ('online', 'offline')),
            lat Real,
            lon Real,
            rumbo Real,
            ts_posicion real
        )
    """
    
    TABLA_EMERGENCIAS="""
        create table if not exists emergencias(
            eid TEXT PRIMARY KEY,
            tipo TEXT NOT NULL CHECK (tipo in ('policia','ambulancia','bomberos')),
            lat real not null,
            lon real not null,
            ts real not null,
            unidad text references unidades(unidad)
        )
    """
    
    LEER_UNIDAD = """
        SELECT u.*, e.eid 
        FROM unidades u
        left join emergencias e on e.unidad = u.unidad
        where u.unidad = ?
    """
    
    #guardamos la pos con upsert, si ya existe la unidad actualizamos sus vlaores posicionales, caso contrario insertamos todo
        #excluded seria el valor nuevo
        #el unico valor opsiconal es el del rumbo
    GUARDAR_POSICION="""
        INSERT INTO unidades (unidad, tipo, lat, lon, rumbo, ts_posicion)
        Values (?, ?, ?, ?, ?, ?)
        On conflict (unidad) do update set
            lat = excluded.lat,
            lon = excluded.lon,
            rumbo = excluded.rumbo,
            ts_posicion = excluded.ts_posicion 
    """
    
    ACTUALIZAR_PRESENCIA="""
        Insert into unidades (unidad, tipo, conexion)
        values(?, ?, ?)
        ON conflict (unidad) do update set conexion = excluded.conexion
    """
    
    DEASIGNAR_UNIDAD="""
        update emergencias set unidad = null where unidad = ?
    """
    
    LEER_EMERGENCIA="""
        select * from emergencias where eid = ?
    """

    
    def __init__(self, ruta_bd, timeout_escritura= 9):
        """
        Explicacion de param:
            + ruta_bd: donde se encuentra el archivo de la bd, si se usa ":memory:" se crea  la bd en la RAM y se destruye al terminar
            + timeout_escritura: para tema de concurrencia y bloqueos, si alguien escribe en la bd pero el archivo esta bloqueado entonces este timout es la espera para indicar a sqlite cuanto debe esperar antes de tirar un error
            + check_same_thread: por defecto true, tal que solo el hilo que creó la conexion a la Bd puede usar la misma. Esto choca con el servicio y su metodo de usar paho
                ya que el servicio es tanto pub como sub, para publicar no podes tener el hilo principal bloqueado (con un loop_forever), entonces se usa un loop_start dejando que otro hilo atienda los callbacks de paho mientras le ppal se encarga de publicar
                Como el servicio es el que usa la bd, pero es el hilo de fondo (el del loop_start) el que llama a los handlers en respuesta de los callbacks, y los handlers usan la bd para ler/escribir entonces es otro el hilo diff al de main (que inicio la conexion) y sqlite tira error
                , seteandolo en false le prohibimos a python bloquear el uso de la conexion entre mutiple hilos. Tampoco es un problema porque no existe el acceso concurrente, solo el hilo de fondo es el que usa la bd pero la conexion la inciia el hilo main.
        
        Que hace la funcion: 
            1. Creamos la conexion con la bd
            2. Creamos las tablas (si no existen)
        """
        #vemos si existe la ruta, sino creamos la carpeta en esa ruta para guardar el arch de la bd
        Path(ruta_bd).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(ruta_bd, timeout= timeout_escritura, check_same_thread=False)
            logger.info("Conexion exitosa con la base de datos en %s", ruta_bd)
        except sqlite3.OperationalError as e:
            logger.exception("Fallo al conectarse a la base de datos: %s", e)
            raise
        
        
        self._conn.row_factory = sqlite3.Row #por defecot sqlite3 tras una consulta devuelve el resu en una tupla, teniendo que acceder por indice a una columna, pero con esto se permite acceder por nombre
        
        try:
            cursor = self._conn.cursor()
            
            #sqlite apaga el chequeo de claves foraneas, asique en cada conexion hay que activarlo para que chequee la fk en emergencais comprobando si la unidad asociada a la emergencia existe
            cursor.execute("PRAGMA foreign_keys = ON;")
            
            #es idempotente porque crea la tabla solo si no existe en la bd
            cursor.execute(BD.TABLA_UNIDADES)
            cursor.execute(BD.TABLA_EMERGENCIAS)
            
            logger.info("Tablas de unidades y emergencias creadas")
        except sqlite3.Error as e:
            logger.exception("Ocurrio un error al crear las tablas de unidades y emergencias: %s", e)
            raise
    

    def transaccion(self):
        #la conexion usada con with: commit si el bloque sale bien, rollback si hay excepcion (no la cierra)
        #los metodos que escriben no abren su propia transaccion, la abre el handler para agrupar
        #las escrituras de un mismo mensaje (ej: offline + desasignar van juntas o no va ninguna)
        return self._conn

    def leer_unidad(self, unidad):
        #leemos la fila de la unidad junto con el eid de la emergencia que esta atendiendo (none si esta libre o directamente no tiene)
        try:
            #fetchone devuelve una sola fila del resu de la query
            return self._conn.execute(BD.LEER_UNIDAD, (unidad,)).fetchone()
        except sqlite3.Error as e:
            logger.exception("Error al leer la unidad: %s", e)
            raise

    
    def guardar_posicion(self, tipo, unidad, lat, lon, rumbo, ts):
        #va dentro de bd.transaccion(), el commit lo hace el handler
        try:
            self._conn.execute(BD.GUARDAR_POSICION, (unidad, tipo, lat, lon, rumbo, ts))
        except sqlite3.Error as e:
            logger.exception("No se pudo guardar la posicion de la unidad en la BD: %s",e)
            raise
            
    def actualizar_presencia(self, unidad, tipo, conexion):
        #va dentro de bd.transaccion(), el commit lo hace el handler
        try:
            self._conn.execute(BD.ACTUALIZAR_PRESENCIA, (unidad, tipo, conexion,))
        except sqlite3.Error as e:
            logger.exception("La actualizacion de presencia de la unidad fallo, no se guardo la presencia: %s", e)
            raise
        
    
    def desasignar_unidad(self, unidad):
        try:
            self._conn.execute(BD.DEASIGNAR_UNIDAD, (unidad,))
        except sqlite3.Error as e:
            logger.exception("La desasignación de la unidad fallo: %s",e)
            raise
    
    def leer_emergencia(self, eid):
        try:
            return self._conn.execute(BD.LEER_EMERGENCIA, (eid,)).fetchone()
        except sqlite3.Error as e:
            logger.exception("Error al leer la emergencia: %s", e)
            raise
    
    def cerrar_conn(self):
        self._conn.close()
        logger.info("Base de datos cerrada")