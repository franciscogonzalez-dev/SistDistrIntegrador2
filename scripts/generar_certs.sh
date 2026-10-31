#!/usr/bin/env bash
# ==============================================================================
# scripts/generar_certs.sh
# Genera una CA propia y el certificado del broker firmado por ella con SAN.
# Salida en: broker/certs/
# ==============================================================================
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Uso: $0 <ip-o-nombre>"
    echo "Ejemplos:"
    echo "  $0 localhost"
    echo "  $0 192.168.1.50"
    echo "  $0 broker.local"
    exit 1
fi

TARGET="$1"

# Directorio raíz del proyecto (relativo al script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$ROOT_DIR/broker/certs"

mkdir -p "$OUT_DIR"

echo "==> Generando certificados en: $OUT_DIR"
echo "==> Destino principal (Target): $TARGET"

# 1. Generar clave privada y certificado autofirmado de la CA
echo "==> [1/4] Creando CA propia (ca.key, ca.crt)..."
openssl genrsa -out "$OUT_DIR/ca.key" 4096
openssl req -x509 -new -nodes -key "$OUT_DIR/ca.key" -sha256 -days 365 \
    -subj "/C=AR/ST=BA/O=DistrOrg/CN=DistrCA" \
    -out "$OUT_DIR/ca.crt"

# 2. Generar clave privada del servidor (broker)
echo "==> [2/4] Creando clave privada del broker (server.key)..."
openssl genrsa -out "$OUT_DIR/server.key" 2048

# 3. Determinar Subject Alternative Name (SAN)
# Incluye siempre localhost y 127.0.0.1 además del argumento recibido
if [[ "$TARGET" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN_EXT="IP:$TARGET,IP:127.0.0.1,DNS:localhost"
else
    SAN_EXT="DNS:$TARGET,DNS:localhost,IP:127.0.0.1"
fi
echo "==> [3/4] SAN configurado: $SAN_EXT"

CSR_FILE="$OUT_DIR/server.csr"
EXT_FILE="$OUT_DIR/server_ext.cnf"

# Generar CSR
openssl req -new -key "$OUT_DIR/server.key" \
    -subj "/C=AR/ST=BA/O=DistrOrg/CN=$TARGET" \
    -out "$CSR_FILE"

# Archivo de extensiones para incluir SAN
cat > "$EXT_FILE" <<EOF
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $SAN_EXT
EOF

# 4. Firmar el certificado del servidor con la CA
echo "==> [4/4] Firmando server.crt con la CA..."
openssl x509 -req -in "$CSR_FILE" \
    -CA "$OUT_DIR/ca.crt" \
    -CAkey "$OUT_DIR/ca.key" \
    -CAcreateserial \
    -out "$OUT_DIR/server.crt" \
    -days 365 \
    -sha256 \
    -extfile "$EXT_FILE"

# Limpieza de archivos temporales
rm -f "$CSR_FILE" "$EXT_FILE" "$OUT_DIR/ca.srl"

# Permisos seguros: claves privadas sólo lectura de usuario, certificados legibles
chmod 600 "$OUT_DIR/ca.key" "$OUT_DIR/server.key"
chmod 644 "$OUT_DIR/ca.crt" "$OUT_DIR/server.crt"

echo ""
echo "==> ¡Certificados generados exitosamente!"
ls -la "$OUT_DIR"
