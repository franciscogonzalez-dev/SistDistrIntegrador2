#!/usr/bin/env bash
# ==============================================================================
# scripts/generar_certs.sh
# [TAREA 1 DE ISSUE #5] Generación de certificados TLS propios para EMQX y clientes.
# Crea una Certificate Authority (CA) propia y un certificado de servidor con SAN.
# Los archivos generados se ubican en: broker/certs/
# ==============================================================================
set -euo pipefail

# Validación de parámetros de entrada
if [ "$#" -lt 1 ]; then
    echo "Uso: $0 <ip-o-nombre>"
    echo "Ejemplos:"
    echo "  $0 localhost"
    echo "  $0 192.168.1.50"
    echo "  $0 broker.local"
    exit 1
fi

TARGET="$1"

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
echo "==> Destino principal (Target): $TARGET"

# ------------------------------------------------------------------------------
# PASO 1 para EMQX con TLS: Generar clave privada y certificado autofirmado de la CA

# ca.key: Clave privada de 4096 bits para firmar certificados (ignorada por Git en .gitignore)
# ca.crt: Certificado público raíz que deben poseer todos los clientes (clientes/ y servicio/)
echo "==> [1/4] Creando CA propia (ca.key, ca.crt)..."
openssl genrsa -out "$OUT_DIR_PATH/ca.key" 4096
openssl req -x509 -new -nodes -key "$OUT_DIR_PATH/ca.key" -sha256 -days 365 \
    -subj "/C=AR/ST=BA/O=DistrOrg/CN=DistrCA" \
    -out "$OUT_DIR_PATH/ca.crt"

# ------------------------------------------------------------------------------
# PASO 2: Generar clave privada del broker (servidor MQTT)

# server.key: Clave privada de 2048 bits para el handshake TLS en el puerto 8883
echo "==> [2/4] Creando clave privada del broker (server.key)..."
openssl genrsa -out "$OUT_DIR_PATH/server.key" 2048

# ------------------------------------------------------------------------------
# PASO 3: Configurar Subject Alternative Name (SAN)

# Obligatorio en TLS moderno: los clientes validan que el host al que se conectan
# coincida con el CN o con alguno de los SAN (DNS o IP).
if [[ "$TARGET" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN_EXT="IP:$TARGET,IP:127.0.0.1,DNS:localhost"
else
    SAN_EXT="DNS:$TARGET,DNS:localhost,IP:127.0.0.1"
fi
echo "==> [3/4] SAN configurado: $SAN_EXT"

CSR_FILE="$OUT_DIR_PATH/server.csr"
EXT_FILE="$OUT_DIR_PATH/server_ext.cnf"

# Generar la solicitud de firma de certificado (CSR)
openssl req -new -key "$OUT_DIR_PATH/server.key" \
    -subj "/C=AR/ST=BA/O=DistrOrg/CN=$TARGET" \
    -out "$CSR_FILE"

# Archivo de extensiones X.509 v3 para incluir los SAN
cat > "$EXT_FILE" <<EOF
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $SAN_EXT
EOF

# ------------------------------------------------------------------------------
# PASO 4: Firmar server.crt usando nuestra propia CA

# server.crt: Certificado público del broker que presenta a los clientes al conectar
echo "==> [4/4] Firmando server.crt con la CA..."
openssl x509 -req -in "$CSR_FILE" \
    -CA "$OUT_DIR_PATH/ca.crt" \
    -CAkey "$OUT_DIR_PATH/ca.key" \
    -CAcreateserial \
    -out "$OUT_DIR_PATH/server.crt" \
    -days 365 \
    -sha256 \
    -extfile "$EXT_FILE"

# Limpieza de archivos temporales
rm -f "$CSR_FILE" "$EXT_FILE" "$OUT_DIR_PATH/ca.srl"

# Permisos seguros: claves privadas con lectura restringida, certificados legibles
chmod 600 "$OUT_DIR_PATH/ca.key" "$OUT_DIR_PATH/server.key" 2>/dev/null || true
chmod 644 "$OUT_DIR_PATH/ca.crt" "$OUT_DIR_PATH/server.crt" 2>/dev/null || true

echo ""
echo "==> ¡Certificados generados exitosamente!"
ls -la "$OUT_DIR_PATH"
