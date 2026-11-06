"""Claves MQTT de los clientes (unidades y despachador).

Las claves reales no van al repo: se copia credenciales.example.json a credenciales.json
(ignorado por git) y se completan con las mismas claves que tienen los usuarios en EMQX (#6).
"""

import hmac
import json
from pathlib import Path

from comun import jwt_auth

ARCHIVO = Path(__file__).resolve().parent / "credenciales.json"


def clave(usuario):
    if not ARCHIVO.exists():
        raise SystemExit(f"falta clientes/{ARCHIVO.name}: copiar credenciales.example.json y completar las claves")

    with open(ARCHIVO, encoding="utf-8") as f:
        claves = json.load(f)

    if usuario not in claves:
        raise SystemExit(f"clientes/{ARCHIVO.name} no tiene la clave de {usuario}")
    return claves[usuario]


def autenticar_despachador(usuario, password, clave_esperada=None, archivo=ARCHIVO):
    """Login del despachador: si la clave es correcta devuelve (token, claims) de la sesion local.

    clave_esperada es la que se paso con --password; si no hay, se busca en credenciales.json.
    Nunca se usa credenciales.example.json: sus claves son publicas. Si falla, lanza ValueError.
    """
    if not usuario.startswith("desp-"):
        raise ValueError("Solo un despachador puede iniciar sesión")

    if clave_esperada is None:
        archivo = Path(archivo)
        if not archivo.exists():
            raise ValueError(f"Falta clientes/{archivo.name}: copiar credenciales.example.json y completar las claves")
        with open(archivo, encoding="utf-8") as f:
            clave_esperada = json.load(f).get(usuario)

    if clave_esperada is None or not hmac.compare_digest(clave_esperada.encode("utf-8"), password.encode("utf-8")):
        raise ValueError("Usuario o contraseña incorrectos")

    token = jwt_auth.generar_token(usuario=usuario, rol="despachador")
    return token, jwt_auth.verificar_token(token)
