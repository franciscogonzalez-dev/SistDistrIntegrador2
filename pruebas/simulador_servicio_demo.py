"""
Simulador del Servicio Central para Pruebas del Despachador
UNPSJB - Sistemas Distribuidos (Proyecto 2)

Este simulador permite probar interactivamente el mapa del despachador
sin depender de que el servicio central definitivo esté terminado:
1. Publica 'servicio/estado' = 'online' (retained).
2. Publica la flota de prueba oficial con posiciones en Comodoro Rivadavia en 'flota/.../estado'.
3. Publica incidentes de prueba en 'emergencias/...'.
4. Escucha órdenes en 'comandos/+/+/+':
   - 'asignar': actualiza la flota a ocupada, la emergencia a asignada y responde en 'respuestas/{despachador}'.
   - 'liberar': libera la unidad, cierra la emergencia (payload vacío retained) y confirma en 'respuestas/{despachador}'.
5. Permite simular caídas de unidades ocupadas (para disparar alertas y probar la reasignación).
"""

import sys
import time
import argparse
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from comun.publisher import MQTTPub, config_entorno
from comun import topics, payloads
from clientes.credenciales import clave

CA_CERTS_DEFAULT = str(ROOT_DIR / "broker" / "certs" / "ca.crt")

# Flota demo con coordenadas iniciales en Comodoro Rivadavia
FLOTA_INICIAL = {
    "amb-01": {"tipo": "ambulancia", "lat": -45.8640, "lon": -67.4850, "rumbo": 90, "conexion": "online", "disponibilidad": "libre", "eid": None},
    "amb-02": {"tipo": "ambulancia", "lat": -45.8610, "lon": -67.4900, "rumbo": 180, "conexion": "online", "disponibilidad": "libre", "eid": None},
    "pol-01": {"tipo": "policia", "lat": -45.8700, "lon": -67.4780, "rumbo": 45, "conexion": "online", "disponibilidad": "libre", "eid": None},
    "pol-02": {"tipo": "policia", "lat": -45.8550, "lon": -67.4950, "rumbo": 270, "conexion": "online", "disponibilidad": "libre", "eid": None},
    "bom-01": {"tipo": "bomberos", "lat": -45.8680, "lon": -67.4820, "rumbo": 0, "conexion": "online", "disponibilidad": "libre", "eid": None},
    "bom-02": {"tipo": "bomberos", "lat": -45.8750, "lon": -67.4900, "rumbo": 135, "conexion": "online", "disponibilidad": "libre", "eid": None},
}

# Emergencias iniciales
EMERGENCIAS_INICIALES = {
    "E-01": {"tipo": "ambulancia", "lat": -45.8625, "lon": -67.4810, "estado": "sin_asignar", "unidad": None},
    "E-02": {"tipo": "bomberos", "lat": -45.8720, "lon": -67.4880, "estado": "sin_asignar", "unidad": None},
}


class SimuladorServicio:

    def __init__(self, ip_broker="127.0.0.1", puerto=8883, ca_certs=CA_CERTS_DEFAULT):
        self.ip_broker = ip_broker
        self.puerto = puerto
        self.ca_certs = ca_certs

        self.flota = dict(FLOTA_INICIAL)
        self.emergencias = dict(EMERGENCIAS_INICIALES)
        self.activo = True

        self.cliente = MQTTPub(
            ip_broker=ip_broker,
            port_broker=puerto,
            client_id="servicio",
            usuario="servicio",
            password="cambiar",
            ca_certs=ca_certs if (ca_certs and Path(ca_certs).exists()) else None,
            sesion_persistente=True,
            expiracion_sesion=86400,
        )

        # Suscripción a comandos del despachador
        self.cliente.subscribe(topics.SUB_COMANDOS, callback=self._on_comando, qos=1)

    def iniciar(self):
        print(f"[*] Conectando Simulador del Servicio Central ({self.ip_broker}:{self.puerto})...")
        self.cliente.start()
        time.sleep(1)

        # 1. Publicar servicio online
        print("[*] Publicando 'servicio/estado' = online...")
        payload_srv = payloads.servicio_estado("online")
        self.cliente.publish(payload_srv, topic=topics.SERVICIO_ESTADO, qos=1, retain=True)

        # 2. Publicar flota inicial
        print("[*] Publicando flota inicial oficial (flota/#)...")
        for uid, datos in self.flota.items():
            self._publicar_unidad(uid)

        # 3. Publicar emergencias iniciales
        print("[*] Publicando emergencias iniciales (emergencias/#)...")
        for eid, datos in self.emergencias.items():
            self._publicar_emergencia(eid)

        print("\n" + "=" * 65)
        print(" SIMULADOR DE SERVICIO CENTRAL ACTIVO")
        print(" Opciones interactivas:")
        print("  [d] Simular CAÍDA de una unidad ocupada (dispara alerta visible y reasignación)")
        print("  [e] Crear una NUEVA emergencia aleatoria")
        print("  [r] Enviar rechazo simulado a desp-01")
        print("  [q] Salir")
        print("=" * 65 + "\n")

    def _publicar_unidad(self, uid):
        datos = self.flota[uid]
        p = payloads.flota_estado(
            conexion=datos["conexion"],
            disponibilidad=datos["disponibilidad"],
            lat=datos["lat"],
            lon=datos["lon"],
            rumbo=datos["rumbo"],
            eid=datos["eid"],
        )
        topico = topics.flota_estado(datos["tipo"], uid)
        self.cliente.publish(p, topic=topico, qos=1, retain=True)

    def _publicar_emergencia(self, eid):
        datos = self.emergencias[eid]
        p = payloads.emergencia(
            eid=eid,
            lat=datos["lat"],
            lon=datos["lon"],
            estado=datos["estado"],
            unidad=datos["unidad"],
        )
        topico = topics.emergencia(datos["tipo"], eid)
        self.cliente.publish(p, topic=topico, qos=1, retain=True)

    def _cerrar_emergencia_retained(self, tipo, eid):
        """Cierra una emergencia enviando un payload vacío con retained."""
        topico = topics.emergencia(tipo, eid)
        self.cliente.publish(b"", topic=topico, qos=1, retain=True)
        if eid in self.emergencias:
            del self.emergencias[eid]
        print(f"[Simulador] Emergencia {eid} cerrada en {topico} (payload vacío retained).")

    def _on_comando(self, client, userdata, msg):
        """Maneja órdenes de asignación y liberación provenientes del despachador."""
        try:
            despachador, tipo, unidad = topics.parse_comando(msg.topic)
            datos = payloads.validar(payloads.COMANDO, msg.payload)
        except ValueError as e:
            print(f"[Simulador] Comando ignorado en {msg.topic}: {e}")
            return

        cmd_id = datos["id"]
        accion = datos["accion"]
        eid = datos.get("eid")

        print(f"\n[Comando Recibido] De: {despachador} | Unidad: {unidad} | Acción: {accion} | EID: {eid} [ID: {cmd_id[:8]}...]")

        if unidad not in self.flota:
            resp = payloads.respuesta(id=cmd_id, ok=False, motivo=f"Móvil {unidad} no existe en la flota")
            self.cliente.publish(resp, topic=topics.respuestas(despachador), qos=1)
            return

        info_unidad = self.flota[unidad]

        if accion == "asignar":
            # Si la unidad ya está ocupada, rechazar
            if info_unidad["disponibilidad"] == "ocupada":
                motivo = f"El móvil {unidad} ya está ocupado en la emergencia {info_unidad['eid']}"
                print(f"  -> Rechazando: {motivo}")
                resp = payloads.respuesta(id=cmd_id, ok=False, motivo=motivo)
                self.cliente.publish(resp, topic=topics.respuestas(despachador), qos=1)
                return

            # Asignar unidad a la emergencia
            info_unidad["disponibilidad"] = "ocupada"
            info_unidad["eid"] = eid
            self._publicar_unidad(unidad)

            if eid in self.emergencias:
                self.emergencias[eid]["estado"] = "asignada"
                self.emergencias[eid]["unidad"] = unidad
                self._publicar_emergencia(eid)

            resp = payloads.respuesta(id=cmd_id, ok=True)
            self.cliente.publish(resp, topic=topics.respuestas(despachador), qos=1)
            print(f"  [OK] Móvil {unidad} asignado a emergencia {eid}.")

        elif accion == "liberar":
            eid_asignado = info_unidad["eid"]
            tipo_unidad = info_unidad["tipo"]

            info_unidad["disponibilidad"] = "libre"
            info_unidad["eid"] = None
            self._publicar_unidad(unidad)

            # Cerrar la emergencia asociada si existía
            if eid_asignado:
                self._cerrar_emergencia_retained(tipo_unidad, eid_asignado)

            resp = payloads.respuesta(id=cmd_id, ok=True)
            self.cliente.publish(resp, topic=topics.respuestas(despachador), qos=1)
            print(f"  [OK] Móvil {unidad} liberado. Emergencia {eid_asignado} finalizada.")

    def simular_caida_unidad_ocupada(self):
        """Simula la desconexión de un móvil ocupado, publica la alerta y devuelve la emergencia a sin_asignar."""
        ocupadas = [uid for uid, d in self.flota.items() if d["disponibilidad"] == "ocupada" and d["conexion"] == "online"]
        if not ocupadas:
            print("[Simulador] No hay unidades ocupadas actualmente. Asigná una primero desde el mapa!")
            return

        uid = ocupadas[0]
        datos_u = self.flota[uid]
        eid = datos_u["eid"]
        tipo = datos_u["tipo"]

        print(f"\n[Simulador] Simulando caída de móvil ocupado: {uid} en emergencia {eid}...")

        # 1. Poner móvil en offline y liberarlo para cuando vuelva
        datos_u["conexion"] = "offline"
        datos_u["disponibilidad"] = "libre"
        datos_u["eid"] = None
        self._publicar_unidad(uid)

        # 2. Devolver la emergencia a sin_asignar
        if eid in self.emergencias:
            self.emergencias[eid]["estado"] = "sin_asignar"
            self.emergencias[eid]["unidad"] = None
            self._publicar_emergencia(eid)

        # 3. Publicar alerta oficial en alertas/{tipo}
        p_alerta = payloads.alerta(
            motivo="unidad_desconectada",
            mensaje=f"Móvil {uid} ({tipo}) se desconectó con la emergencia {eid} en curso",
            unidad=uid,
            eid=eid,
        )
        self.cliente.publish(p_alerta, topic=topics.alertas(tipo), qos=1)
        print(f"[Simulador] Alerta publicada en alertas/{tipo}: {p_alerta['mensaje']}\n")

    def crear_emergencia_aleatoria(self):
        """Crea una nueva emergencia para despacho."""
        nuevo_id = f"E-{len(self.emergencias) + 10:02d}"
        tipo = "policia" if len(self.emergencias) % 2 == 0 else "ambulancia"
        lat = -45.8600 - (len(self.emergencias) * 0.003)
        lon = -67.4800 - (len(self.emergencias) * 0.002)

        self.emergencias[nuevo_id] = {
            "tipo": tipo,
            "lat": lat,
            "lon": lon,
            "estado": "sin_asignar",
            "unidad": None,
        }
        self._publicar_emergencia(nuevo_id)
        print(f"[Simulador] Nueva emergencia creada: {nuevo_id} ({tipo}) en ({lat:.4f}, {lon:.4f})")

    def detener(self):
        print("[*] Deteniendo simulador...")
        self.activo = False
        payload_srv = payloads.servicio_estado("offline")
        self.cliente.publish(payload_srv, topic=topics.SERVICIO_ESTADO, qos=1, retain=True)
        time.sleep(0.5)
        self.cliente.stop()


if __name__ == "__main__":
    entorno = config_entorno()
    parser = argparse.ArgumentParser(description="Simulador de Servicio Central")
    parser.add_argument("--broker", type=str, default=entorno["ip_broker"] or "127.0.0.1")
    parser.add_argument("--puerto", type=int, default=None)
    parser.add_argument("--no-tls", action="store_true")
    args = parser.parse_args()

    puerto = 1883 if args.no_tls else (args.puerto or entorno["port_broker"] or 8883)
    ca_certs = None if args.no_tls else CA_CERTS_DEFAULT

    sim = SimuladorServicio(ip_broker=args.broker, puerto=puerto, ca_certs=ca_certs)
    sim.iniciar()

    try:
        while sim.activo:
            cmd = input().strip().lower()
            if cmd == "d":
                sim.simular_caida_unidad_ocupada()
            elif cmd == "e":
                sim.crear_emergencia_aleatoria()
            elif cmd == "r":
                p_rechazo = payloads.respuesta(id="test-cmd", ok=False, motivo="Simulación: Comando rechazado por falta de recursos")
                sim.cliente.publish(p_rechazo, topic=topics.respuestas("desp-01"), qos=1)
                print("[Simulador] Rechazo enviado a respuestas/desp-01")
            elif cmd == "q":
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        sim.detener()
