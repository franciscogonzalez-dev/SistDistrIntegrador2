"""Módulo de autenticación y gestión de JSON Web Tokens (JWT).
Proyecto 2 - Sistemas Distribuidos (UNPSJB)

Implementa la especificación RFC 7519 con algoritmo HMAC-SHA256 (HS256)
utilizando únicamente la biblioteca estándar de Python (hmac, hashlib, base64, json).
Es completamente autocontenido, sin dependencias externas obligatorias,
e interoperable con librerías como PyJWT y visores web como jwt.io.
"""

import os
import time
import json
import hmac
import uuid
import base64
import hashlib
from pathlib import Path

# Clave secreta para firmar y validar tokens (definible por entorno o fallback seguro de desarrollo)
JWT_SECRET_DEFAULT = os.getenv("JWT_SECRET", "SistDistr2026_Emergencias_JWT_SecretKey_9918273645")
JWT_ALGORITMO = "HS256"
EXPIRACION_DEFAULT_SEG = 3600  # 1 hora de validez por defecto
EMISOR_DEFAULT = "SistDistr-CentroDespacho"


class ErrorJWT(Exception):
    """Excepción base para fallos relacionados a JWT."""
    pass


class TokenExpirado(ErrorJWT):
    """El token ha expirado (exp superado)."""
    pass


class FirmaInvalida(ErrorJWT):
    """La firma criptográfica no coincide (token adulterado o secreto erróneo)."""
    pass


class TokenInvalido(ErrorJWT):
    """Estructura de token malformada o claims no válidos."""
    pass


# ==============================================================================
# UTILIDADES BASE64URL (RFC 7515 / RFC 7519)
# ==============================================================================

def _b64url_encode(data: bytes) -> str:
    """Codifica bytes en Base64 URL-safe sin relleno '='."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(segmento: str) -> bytes:
    """Decodifica un string Base64 URL-safe agregando el relleno '=' faltante."""
    modulo = len(segmento) % 4
    if modulo != 0:
        segmento += "=" * (4 - modulo)
    try:
        return base64.urlsafe_b64decode(segmento.encode("utf-8"))
    except Exception as e:
        raise TokenInvalido(f"Formato Base64 inválido: {e}") from e


# ==============================================================================
# GENERACIÓN Y VALIDACIÓN DE TOKENS JWT (HS256)
# ==============================================================================

def generar_token(
    usuario: str,
    rol: str = "despachador",
    permisos: list = None,
    expiracion_segundos: int = EXPIRACION_DEFAULT_SEG,
    secreto: str = None,
    claims_adicionales: dict = None,
) -> str:
    """
    Genera un JWT firmado con HS256 conteniendo los claims estándar y de rol.

    Claims incluidos:
    - sub (Subject): Identificador del usuario (ej. 'desp-01')
    - rol: Rol del usuario en el sistema ('despachador', 'supervisor', 'admin')
    - permisos: Lista de operaciones autorizadas
    - iat (Issued At): Momento de emisión en tiempo Unix
    - exp (Expiration Time): Momento de vencimiento
    - iss (Issuer): Emisor del token
    - jti (JWT ID): Identificador único del token para trazabilidad
    """
    if secreto is None:
        secreto = JWT_SECRET_DEFAULT

    ahora = int(time.time())
    if permisos is None:
        if rol == "despachador":
            permisos = ["despacho:asignar", "despacho:liberar", "monitoreo:ver"]
        elif rol == "admin":
            permisos = ["*"]
        else:
            permisos = ["monitoreo:ver"]

    header = {
        "alg": JWT_ALGORITMO,
        "typ": "JWT",
    }

    payload = {
        "sub": usuario,
        "rol": rol,
        "permisos": permisos,
        "iat": ahora,
        "exp": ahora + expiracion_segundos,
        "iss": EMISOR_DEFAULT,
        "jti": str(uuid.uuid4()),
    }

    if claims_adicionales:
        payload.update(claims_adicionales)

    # Serializar header y payload a JSON canónico
    header_bytes = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    header_b64 = _b64url_encode(header_bytes)
    payload_b64 = _b64url_encode(payload_bytes)

    # Firma HMAC-SHA256
    mensaje_a_firmar = f"{header_b64}.{payload_b64}".encode("utf-8")
    firma_bytes = hmac.new(secreto.encode("utf-8"), mensaje_a_firmar, hashlib.sha256).digest()
    firma_b64 = _b64url_encode(firma_bytes)

    return f"{header_b64}.{payload_b64}.{firma_b64}"


def verificar_token(token: str, secreto: str = None) -> dict:
    """
    Valida la firma, estructura y tiempo de vida de un token JWT.
    Retorna el diccionario de claims del payload si es válido.
    Lanza TokenInvalido, TokenExpirado o FirmaInvalida si falla.
    """
    if secreto is None:
        secreto = JWT_SECRET_DEFAULT

    if not token or not isinstance(token, str):
        raise TokenInvalido("El token proporcionado no es un string válido")

    partes = token.strip().split(".")
    if len(partes) != 3:
        raise TokenInvalido(f"Formato de token erróneo: se esperaban 3 partes separadas por puntos, se recibieron {len(partes)}")

    header_b64, payload_b64, firma_b64 = partes

    # 1. Validar firma criptográfica
    mensaje_firmado = f"{header_b64}.{payload_b64}".encode("utf-8")
    firma_esperada = hmac.new(secreto.encode("utf-8"), mensaje_firmado, hashlib.sha256).digest()
    firma_recibida = _b64url_decode(firma_b64)

    # Comparación en tiempo constante para evitar ataques de timing
    if not hmac.compare_digest(firma_esperada, firma_recibida):
        raise FirmaInvalida("La firma del token JWT no coincide con el secreto")

    # 2. Decodificar header y payload
    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as e:
        raise TokenInvalido(f"Error al decodificar JSON del token: {e}") from e

    if header.get("alg") != JWT_ALGORITMO:
        raise TokenInvalido(f"Algoritmo de firma no soportado: {header.get('alg')}")

    # 3. Validar expiración (exp)
    ahora = int(time.time())
    exp = payload.get("exp")
    if exp is not None:
        if not isinstance(exp, (int, float)) or ahora > exp:
            raise TokenExpirado(f"El token ha expirado en {exp} (hora actual: {ahora})")

    return payload


def decodificar_sin_verificar(token: str) -> tuple[dict, dict]:
    """
    Decodifica (header, payload) sin verificar la firma.
    Útil para inspección visual o debugging en la interfaz gráfica.
    """
    if not token or not isinstance(token, str):
        raise TokenInvalido("Token vacío o inválido")
    partes = token.strip().split(".")
    if len(partes) < 2:
        raise TokenInvalido("El token no tiene suficientes segmentos")
    header = json.loads(_b64url_decode(partes[0]).decode("utf-8"))
    payload = json.loads(_b64url_decode(partes[1]).decode("utf-8"))
    return header, payload


# ==============================================================================
# AUTENTICACIÓN DE USUARIOS (CREDENCIALES -> JWT)
# ==============================================================================

def autenticar_despachador(
    usuario: str,
    password: str,
    secreto: str = None,
    expiracion_segundos: int = EXPIRACION_DEFAULT_SEG,
) -> tuple[str, dict]:
    """
    Autentica al operador del despachador contra clientes/credenciales.json.
    Si las credenciales coinciden, genera y devuelve (token_jwt, claims).
    Si fallan, lanza ValueError.
    """
    from pathlib import Path
    
    # Buscar credenciales.json en el directorio clientes
    raiz = Path(__file__).resolve().parent.parent
    archivo_credenciales = raiz / "clientes" / "credenciales.json"
    
    # Fallback si aún no se creó credenciales.json: leer credenciales.example.json o validar por defecto
    if not archivo_credenciales.exists():
        archivo_ejemplo = raiz / "clientes" / "credenciales.example.json"
        if archivo_ejemplo.exists():
            archivo_credenciales = archivo_ejemplo
        else:
            raise ValueError("No se encontró el archivo de credenciales de clientes")

    try:
        with open(archivo_credenciales, encoding="utf-8") as f:
            credenciales = json.load(f)
    except Exception as e:
        raise ValueError(f"Error al leer credenciales: {e}") from e

    clave_esperada = credenciales.get(usuario)
    if clave_esperada is None:
        raise ValueError(f"Usuario desconocido: '{usuario}'")

    if clave_esperada != password:
        raise ValueError("Contraseña incorrecta")

    # Credenciales válidas: emitir token
    token = generar_token(
        usuario=usuario,
        rol="despachador",
        expiracion_segundos=expiracion_segundos,
        secreto=secreto,
    )
    claims = verificar_token(token, secreto=secreto)
    return token, claims
