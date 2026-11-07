#!/usr/bin/env python3
"""
pruebas/webhook_acl_denegada.py
Dispara el webhook por regla (Issue #17): una unidad intenta publicar en el topic de otra,
la ACL lo deniega y el broker avisa al receptor.

Necesita el broker y el receptor levantados. Usa el client ID de amb-02, así que
desconecta a esa unidad si está corriendo.

Desde la raíz del repo:
    .venv/Scripts/python.exe pruebas/webhook_acl_denegada.py        (Windows)
    .venv/bin/python pruebas/webhook_acl_denegada.py                (Linux)

Con otro broker: MQTT_HOST=<ip-broker> y el mismo comando.
El aviso aparece en la página del receptor (puerto 5000) como "Publicación denegada".
"""

import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from clientes.credenciales import clave
from test_seguridad_emqx import crear_cliente_autenticado, probar_publicacion

UNIDAD = "amb-02"
TOPIC_PROHIBIDO = "reportes/policia/pol-03/posicion"


def main():
    cliente = crear_cliente_autenticado(UNIDAD, UNIDAD, clave(UNIDAD))
    permitida, codigo = probar_publicacion(cliente, TOPIC_PROHIBIDO)
    # Margen para que el broker termine de procesar antes de desconectar
    time.sleep(2)
    cliente.loop_stop()
    cliente.disconnect()

    if permitida:
        print(f"ERROR: la ACL dejó publicar a {UNIDAD} en {TOPIC_PROHIBIDO} (código {codigo})")
        sys.exit(1)

    print(f"{UNIDAD} intentó publicar en {TOPIC_PROHIBIDO}: denegado (código {codigo})")
    print("Revisá el aviso en la página del receptor (puerto 5000)")


if __name__ == "__main__":
    main()
