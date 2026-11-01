import os
import hmac
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import deque
from flask import Flask, request, jsonify, render_template_string

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

# Secreto compartido por variable de entorno (sale del .env, que no se sube al repo).
# Sin secreto no arranca: un valor por defecto en el codigo seria un secreto publico
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise SystemExit("Falta la variable WEBHOOK_SECRET (ver broker/.env.example)")

# El container corre en UTC: la hora de los avisos se muestra en la de Argentina (UTC-3, sin horario de verano)
ZONA_HORARIA = timezone(timedelta(hours=-3))

# Historial de los últimos N avisos
MAX_AVISOS = int(os.getenv("MAX_AVISOS", "50")) #ahora lo confugré en 50 pero se puede configurar
avisos_log = deque(maxlen=MAX_AVISOS)

# Cada aviso tambien se agrega a un archivo (una linea JSON por aviso), en un volumen de Docker:
# si el container se reinicia o se recrea, la pagina vuelve a mostrar los ultimos avisos
ARCHIVO_AVISOS = Path(os.getenv("ARCHIVO_AVISOS", Path(__file__).resolve().parent / "datos" / "avisos.jsonl"))

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="5">
    <title>Receptor de Webhooks - Monitoreo</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 30px; background-color: #f7f9fc; color: #333; }
        h1 { color: #1a365d; }
        .counter { color: #718096; margin-bottom: 20px; }
        table { width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #edf2f7; }
        th { background-color: #2b6cb0; color: white; font-weight: 600; }
        tr:hover { background-color: #f8fafc; }
        .badge { padding: 4px 8px; border-radius: 4px; font-size: 0.85em; font-weight: bold; }
        .badge-directo { background-color: #e2e8f0; color: #2d3748; }
        .badge-regla { background-color: #feebc8; color: #7b341e; }
        pre { margin: 0; background: #f7fafc; padding: 6px; border-radius: 4px; font-size: 0.85em; overflow-x: auto; max-width: 600px; white-space: pre-wrap; word-break: break-all; }
    </style>
</head>
<body>
    <h1>Centro de Monitoreo - Avisos de Webhooks</h1>
    <div class="counter">Mostrando los últimos {{ avisos|length }} avisos (actualización automática cada 5s).</div>
    <table>
        <thead>
            <tr>
                <th style="width: 180px;">Fecha y Hora</th>
                <th style="width: 120px;">Tipo de Aviso</th>
                <th>Contenido (Payload)</th>
            </tr>
        </thead>
        <tbody>
            {% for a in avisos %}
            <tr>
                <td>{{ a.timestamp }}</td>
                <td><span class="badge badge-{{ a.tipo }}">{{ a.tipo|upper }}</span></td>
                <td><pre>{{ a.payload }}</pre></td>
            </tr>
            {% else %}
            <tr>
                <td colspan="3" style="text-align: center; color: #a0aec0; padding: 30px;">Aún no se han recibido avisos.</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</body>
</html>
"""

def verificar_secreto(): #si la clave secreta de la request POST no coincide o directamente no está, se rechaza la peticion
    header_secret = request.headers.get("X-Webhook-Secret")
    #compare_digest tarda lo mismo acierte o no, asi no se puede adivinar el secreto midiendo el tiempo de respuesta
    if not header_secret or not hmac.compare_digest(header_secret.encode(), WEBHOOK_SECRET.encode()):
        logging.warning("Acceso no autorizado: X-Webhook-Secret inválido o ausente.")
        return False
    return True

def cargar_avisos():
    if not ARCHIVO_AVISOS.exists():
        return
    with open(ARCHIVO_AVISOS, encoding="utf-8") as f:
        for linea in f:
            try:
                avisos_log.appendleft(json.loads(linea)) #la deque se queda con los ultimos MAX_AVISOS
            except json.JSONDecodeError:
                #una linea cortada (el container murio mientras escribia) no tiene que impedir que arranque
                logging.warning("Línea inválida en %s, se ignora", ARCHIVO_AVISOS)

def guardar_aviso(tipo, payload_str):
    aviso = {
        "timestamp": datetime.now(ZONA_HORARIA).strftime("%Y-%m-%d %H:%M:%S"),
        "tipo": tipo,
        "payload": payload_str
    }
    avisos_log.appendleft(aviso)
    ARCHIVO_AVISOS.parent.mkdir(parents=True, exist_ok=True)
    with open(ARCHIVO_AVISOS, "a", encoding="utf-8") as f:
        f.write(json.dumps(aviso, ensure_ascii=False) + "\n")

def parse_payload():
    data = request.get_json(silent=True)
    if data is not None:
        return json.dumps(data, indent=2, ensure_ascii=False)
    if request.form:
        return json.dumps(request.form.to_dict(), indent=2, ensure_ascii=False)
    text = request.get_data(as_text=True)
    return text if text else "{}"

@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE, avisos=list(avisos_log))

@app.route("/avisos/directo", methods=["POST"])
def aviso_directo():
    if not verificar_secreto():
        return jsonify({"error": "No autorizado"}), 401

    payload_str = parse_payload()
    guardar_aviso("directo", payload_str)
    logging.info(f"[DIRECTO] Aviso recibido: {payload_str}")
    return jsonify({"status": "ok", "tipo": "directo"}), 200

@app.route("/avisos/regla", methods=["POST"])
def aviso_regla():
    if not verificar_secreto():
        return jsonify({"error": "No autorizado"}), 401

    payload_str = parse_payload()
    guardar_aviso("regla", payload_str)
    logging.info(f"[REGLA] Aviso recibido: {payload_str}")
    return jsonify({"status": "ok", "tipo": "regla"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200

cargar_avisos()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
