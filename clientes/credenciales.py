"""Claves MQTT de los clientes (unidades y despachador).

Las claves reales no van al repo: se copia credenciales.example.json a credenciales.json
(ignorado por git) y se completan con las mismas claves que tienen los usuarios en EMQX (#6).
"""

import json
from pathlib import Path

ARCHIVO = Path(__file__).resolve().parent / "credenciales.json"


def clave(usuario):
    if not ARCHIVO.exists():
        raise SystemExit(f"falta clientes/{ARCHIVO.name}: copiar credenciales.example.json y completar las claves")

    with open(ARCHIVO, encoding="utf-8") as f:
        claves = json.load(f)

    if usuario not in claves:
        raise SystemExit(f"clientes/{ARCHIVO.name} no tiene la clave de {usuario}")
    return claves[usuario]
