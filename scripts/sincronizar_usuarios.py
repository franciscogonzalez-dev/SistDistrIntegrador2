#!/usr/bin/env python3
"""
Sincroniza los usuarios y claves desde clientes/credenciales.json hacia broker/emqx/usuarios.csv.
Garantiza que el broker EMQX tenga registrados exactamente los mismos usuarios y claves
que utilizan las unidades móviles, el despachador, el servicio central y el público civil.
"""

import csv
import json
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CREDENCIALES_JSON = ROOT_DIR / "clientes" / "credenciales.json"
USUARIOS_CSV = ROOT_DIR / "broker" / "emqx" / "usuarios.csv"
USUARIOS_EXAMPLE_CSV = ROOT_DIR / "broker" / "emqx" / "usuarios.example.csv"

# Orden oficial de los 18 usuarios según docs/acl.md
USUARIOS_ORDEN = [
    "servicio",
    "desp-01",
    "amb-01", "amb-02", "amb-03", "amb-04", "amb-05",
    "pol-01", "pol-02", "pol-03", "pol-04", "pol-05",
    "bom-01", "bom-02", "bom-03", "bom-04", "bom-05",
    "publico",
]


def sincronizar():
    if not CREDENCIALES_JSON.exists():
        raise SystemExit(
            f"Error: No existe {CREDENCIALES_JSON}. Copie credenciales.example.json primero."
        )

    with open(CREDENCIALES_JSON, "r", encoding="utf-8") as f:
        credenciales = json.load(f)

    # Permitir que el servicio tome su clave de variable de entorno si está definida
    clave_servicio_env = os.getenv("MQTT_PASSWORD")
    if clave_servicio_env:
        credenciales["servicio"] = clave_servicio_env

    # Validar que todos los usuarios requeridos tengan clave
    faltantes = [u for u in USUARIOS_ORDEN if u not in credenciales]
    if faltantes:
        raise SystemExit(
            f"Error: Faltan claves en {CREDENCIALES_JSON} para los usuarios: {faltantes}"
        )

    USUARIOS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(USUARIOS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "password", "is_superuser"])
        for u in USUARIOS_ORDEN:
            # is_superuser es siempre false por principio de menor privilegio
            writer.writerow([u, credenciales[u], "false"])

    print(f"==> Generado exitosamente: {USUARIOS_CSV} ({len(USUARIOS_ORDEN)} usuarios)")


if __name__ == "__main__":
    sincronizar()
