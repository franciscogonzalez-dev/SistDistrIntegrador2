#!/usr/bin/env bash
# ==============================================================================
# scripts/generar_certs.sh
# Generación de certificados TLS propios para EMQX y clientes.
# Reutiliza la CA si ya existe y genera el certificado del broker con SAN múltiple.
# Los archivos generados se ubican en: broker/certs/
# ==============================================================================
set -euo pipefail

# 1. En Windows, si OPENSSL_CONF apunta a otra app (ej. PostgreSQL), openssl falla al arrancar
unset OPENSSL_CONF 2>/dev/null || true

# Validación de parámetros de entrada (acepta 1 o más destinos)
if [ "$#" -lt 1 ]; then
    echo "Uso: $0 <ip-o-nombre> [ip-o-nombre-2 ...]"
    echo "Ejemplos:"
    echo "  $0 localhost"
    echo "  $0 broker.local 100.64.0.1 192.168.1.50"
    exit 1
fi

PRIMARY_TARGET="$1"

# Resolución dinámica de rutas relativas a la raíz del proyecto
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$ROOT_DIR/broker/certs"

mkdir -p "$OUT_DIR"

# Compatibilidad multiplataforma (EMQX con TLS):
# En entornos MSYS/Git Bash (Windows), deshabilitamos la conversión automática de rutas
# en openssl (que rompe flags como -subj "/C=AR/...") y normalizamos las barras de ruta.
export MSYS_NO_PATHCONV=1
if command -v cygpath >/dev/null 2>&1; then
    OUT_DIR_PATH="$(cygpath -m "$OUT_DIR")"
else
    OUT_DIR_PATH="$OUT_DIR"
fi

echo "==> Generando certificados en: $OUT_DIR_PATH"

# ------------------------------------------------------------------------------
# PASO 1: Reutilizar o crear la CA propia (ca.key, ca.crt)
# ------------------------------------------------------------------------------
CA_KEY="$OUT_DIR_PATH/ca.key"
CA_CRT="$OUT_DIR_PATH/ca.crt"

ca_valida=false
if [ -f "$OUT_DIR/ca.key" ] && [ -f "$OUT_DIR/ca.crt" ]; then
    # Verificar que la clave privada coincida con el certificado de la CA
    KEY_MOD=$(openssl rsa -noout -modulus -in "$CA_KEY" 2>/dev/null | openssl md5 || true)
    CRT_MOD=$(openssl x509 -noout -modulus -in "$CA_CRT" 2>/dev/null | openssl md5 || true)
    if [ -n "$KEY_MOD" ] && [ "$KEY_MOD" = "$CRT_MOD" ]; then
        ca_valida=true
    else
        echo "==> [Aviso] ca.key y ca.crt existentes no coinciden. Regenerando CA..."
    fi
fi

if [ "$ca_valida" = true ]; then
    echo "==> [1/4] Reutilizando CA existente ($CA_KEY, $CA_CRT)..."
else
    echo "==> [1/4] Creando CA propia (ca.key, ca.crt)..."
    openssl genrsa -out "$CA_KEY" 4096
    openssl req -x509 -new -nodes -key "$CA_KEY" -sha256 -days 365 \
        -subj "/C=AR/ST=BA/O=DistrOrg/CN=DistrCA" \
        -out "$CA_CRT"
fi

# ------------------------------------------------------------------------------
# PASO 2: Generar clave privada del broker (server.key)
# ------------------------------------------------------------------------------
echo "==> [2/4] Creando clave privada del broker (server.key)..."
openssl genrsa -out "$OUT_DIR_PATH/server.key" 2048

# ------------------------------------------------------------------------------
# PASO 3: Configurar Subject Alternative Name (SAN) múltiple sin duplicados
# ------------------------------------------------------------------------------
SAN_LIST=()

agregar_san() {
    local val="$1"
    local entrada
    if [[ "$val" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        entrada="IP:$val"
    else
        entrada="DNS:$val"
    fi
    for exist in "${SAN_LIST[@]}"; do
        if [[ "$exist" == "$entrada" ]]; then
            return 0
        fi
    done
    SAN_LIST+=("$entrada")
}

# Agregar todos los destinos pasados como argumento
for target in "$@"; do
    agregar_san "$target"
done
# Asegurar siempre localhost y 127.0.0.1 como fallback
agregar_san "localhost"
agregar_san "127.0.0.1"

SAN_EXT=$(IFS=,; echo "${SAN_LIST[*]}")
echo "==> [3/4] SAN configurado: $SAN_EXT"

CSR_FILE="$OUT_DIR_PATH/server.csr"
EXT_FILE="$OUT_DIR_PATH/server_ext.cnf"

# Generar la solicitud de firma (CSR) usando el primer destino como CN
openssl req -new -key "$OUT_DIR_PATH/server.key" \
    -subj "/C=AR/ST=BA/O=DistrOrg/CN=$PRIMARY_TARGET" \
    -out "$CSR_FILE"

# Archivo de extensiones X.509 v3 para incluir los SAN
cat > "$EXT_FILE" <<EOF
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $SAN_EXT
EOF

# ------------------------------------------------------------------------------
# PASO 4: Firmar server.crt con la CA
# ------------------------------------------------------------------------------
echo "==> [4/4] Firmando server.crt con la CA..."
openssl x509 -req -in "$CSR_FILE" \
    -CA "$CA_CRT" \
    -CAkey "$CA_KEY" \
    -CAcreateserial \
    -out "$OUT_DIR_PATH/server.crt" \
    -days 365 \
    -sha256 \
    -extfile "$EXT_FILE"

# Limpieza de archivos temporales
rm -f "$CSR_FILE" "$EXT_FILE" "$OUT_DIR_PATH/ca.srl"

# Permisos:
# - ca.key: solo dueño (600), no la usa Docker
# - server.key: lectura para el contenedor (644), donde EMQX corre como uid 1000
# - certificados públicos: lectura general (644)
chmod 600 "$OUT_DIR_PATH/ca.key"
chmod 644 "$OUT_DIR_PATH/server.key"
chmod 644 "$OUT_DIR_PATH/ca.crt" "$OUT_DIR_PATH/server.crt"

echo ""
echo "==> ¡Certificados generados exitosamente!"
ls -la "$OUT_DIR"
