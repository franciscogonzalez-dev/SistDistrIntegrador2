"""JWT de la sesion local del despachador (RFC 7519, HS256, solo biblioteca estandar).

El token vive en la app del despachador: no viaja por MQTT y el servicio no lo valida.
La identidad de quien publica sigue saliendo del topico y de la ACL (docs/topics.md, docs/jwt.md).
"""

import math
import time
import json
import hmac
import uuid
import base64
import secrets
import hashlib

#clave aleatoria por proceso, solo en memoria: no hay nada que configurar ni que se pueda filtrar por el repo
#un token solo es valido en la app que lo emitio y deja de serlo al cerrarla
_SECRETO_PROCESO = secrets.token_urlsafe(32)
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
        secreto = _SECRETO_PROCESO

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
        secreto = _SECRETO_PROCESO

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
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        raise TokenInvalido("Claim exp ausente o inválido")
    if ahora >= exp:
        raise TokenExpirado(f"El token ha expirado en {exp} (hora actual: {ahora})")

    if payload.get("iss") != EMISOR_DEFAULT:
        raise TokenInvalido("Emisor no reconocido")

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
