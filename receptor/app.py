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
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Centro de Monitoreo - Avisos</title>
    <style>
        :root {
            --fondo: #f2f4f7;
            --superficie: #ffffff;
            --borde: #d8dee6;
            --texto: #1b2633;
            --secundario: #566373;
            --marino: #14263d;
            --azul: #1f4e79;
            --azul-suave: #e7eef5;
            --rojo: #9c1c1c;
            --rojo-suave: #f8e9e9;
        }
        * { box-sizing: border-box; }
        [hidden] { display: none !important; }
        body { margin: 0; background: var(--fondo); color: var(--texto); font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, "Helvetica Neue", Arial, sans-serif; font-size: 15px; line-height: 1.45; }
        .contenedor { max-width: 1180px; margin: 0 auto; padding-inline: 24px; }
        .encabezado { background: var(--marino); color: #ffffff; border-bottom: 3px solid var(--azul); }
        .encabezado .contenedor { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px 32px; padding-block: 22px; }
        .sistema { margin: 0 0 2px; color: #b4c2d3; font-size: 13px; letter-spacing: 0.04em; }
        h1 { margin: 0; font-size: 24px; font-weight: 600; }
        .actualizacion { color: #d5deea; font-size: 13px; }
        main { padding-block: 24px 40px; }
        .indicadores { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .indicador { background: var(--superficie); border: 1px solid var(--borde); border-top: 3px solid var(--marino); padding: 14px 18px; }
        .indicador-directo { border-top-color: var(--azul); }
        .indicador-regla { border-top-color: var(--rojo); }
        .indicador-valor { display: block; font-size: 30px; font-weight: 600; line-height: 1.2; font-variant-numeric: tabular-nums; }
        .indicador-nombre { color: var(--secundario); font-size: 13px; }
        h2 { margin: 0; font-size: 17px; font-weight: 600; }
        .nota { margin: 2px 0 0; color: var(--secundario); font-size: 13px; }
        .registro { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 10px 24px; margin-bottom: 12px; }
        .acciones { display: flex; flex-wrap: wrap; gap: 8px; }
        .boton { padding: 6px 12px; background: var(--superficie); border: 1px solid var(--azul); border-radius: 3px; color: var(--azul); font: inherit; font-size: 13px; font-weight: 600; cursor: pointer; }
        .boton:hover:not(:disabled) { background: var(--azul-suave); }
        .boton:focus-visible { outline: 2px solid var(--azul); outline-offset: 2px; }
        .boton:disabled { border-color: var(--borde); color: var(--secundario); cursor: default; }
        .tabla { overflow-x: auto; background: var(--superficie); border: 1px solid var(--borde); }
        table { width: 100%; min-width: 660px; border-collapse: collapse; }
        th, td { padding: 12px 16px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--borde); }
        tbody tr:last-child td { border-bottom: 0; }
        th { background: #e9edf2; color: var(--secundario); font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
        .col-fecha { width: 175px; }
        .col-webhook { width: 215px; }
        .fecha { color: var(--secundario); white-space: nowrap; font-variant-numeric: tabular-nums; }
        .fila-directo .fecha { box-shadow: inset 3px 0 0 var(--azul); }
        .fila-regla .fecha { box-shadow: inset 3px 0 0 var(--rojo); }
        .etiqueta { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 13px; font-weight: 600; white-space: nowrap; }
        .etiqueta-directo { background: var(--azul-suave); color: var(--azul); }
        .etiqueta-regla { background: var(--rojo-suave); color: var(--rojo); }
        .resumen { font-weight: 600; overflow-wrap: anywhere; }
        details { margin-top: 4px; }
        summary { width: fit-content; color: var(--azul); font-size: 13px; cursor: pointer; }
        summary:focus-visible { outline: 2px solid var(--azul); outline-offset: 2px; }
        pre { margin: 8px 0 0; padding: 10px 12px; background: var(--fondo); border: 1px solid var(--borde); font-family: Consolas, "Liberation Mono", Menlo, monospace; font-size: 12.5px; white-space: pre-wrap; overflow-wrap: anywhere; }
        .vacio { padding: 40px 16px; text-align: center; color: var(--secundario); }
    </style>
</head>
<body>
    <header class="encabezado">
        <div class="contenedor">
            <div>
                <p class="sistema">Sistema de despacho de emergencias</p>
                <h1>Centro de Monitoreo</h1>
            </div>
            <div class="actualizacion">Actualización automática cada 5 segundos</div>
        </div>
    </header>
    <main class="contenedor">
        <section class="indicadores" aria-label="Totales de los avisos mostrados">
            <div class="indicador">
                <span class="indicador-valor" id="total-avisos">{{ avisos|length }}</span>
                <span class="indicador-nombre">Avisos mostrados</span>
            </div>
            <div class="indicador indicador-directo">
                <span class="indicador-valor" id="total-directo">{{ avisos|selectattr("tipo", "equalto", "directo")|list|length }}</span>
                <span class="indicador-nombre">Conexiones y desconexiones</span>
            </div>
            <div class="indicador indicador-regla">
                <span class="indicador-valor" id="total-regla">{{ avisos|selectattr("tipo", "equalto", "regla")|list|length }}</span>
                <span class="indicador-nombre">Publicaciones denegadas por la ACL</span>
            </div>
        </section>
        <div class="registro">
            <div>
                <h2>Registro de avisos</h2>
                <p class="nota">Del más reciente al más antiguo. La actualización se pausa mientras haya un payload abierto.</p>
                <p class="nota">Limpiar la vista oculta los avisos solo en este navegador: no borra el registro.</p>
            </div>
            <div class="acciones">
                <button type="button" class="boton" id="mostrar" hidden>Mostrar todos</button>
                <button type="button" class="boton" id="limpiar">Limpiar vista</button>
            </div>
        </div>
        <div class="tabla">
            <table>
                <thead>
                    <tr>
                        <th class="col-fecha">Fecha y hora</th>
                        <th class="col-webhook">Webhook</th>
                        <th>Aviso</th>
                    </tr>
                </thead>
                <tbody>
                    {% for a in avisos %}
                    <tr class="fila-{{ a.tipo }}" data-ts="{{ a.timestamp }}" data-tipo="{{ a.tipo }}">
                        <td class="fecha">{{ a.timestamp }}</td>
                        <td><span class="etiqueta etiqueta-{{ a.tipo }}">{{ a.nombre }}</span></td>
                        <td>
                            <div class="resumen">{{ a.resumen }}</div>
                            <details>
                                <summary>Ver payload</summary>
                                <pre>{{ a.crudo }}</pre>
                            </details>
                        </td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="3" class="vacio">Aún no se recibieron avisos.</td>
                    </tr>
                    {% endfor %}
                    <tr id="sin-visibles" hidden>
                        <td colspan="3" class="vacio">No hay avisos nuevos desde la última limpieza.</td>
                    </tr>
                </tbody>
            </table>
        </div>
    </main>
    <script>
        // Limpiar la vista guarda hasta que fecha ocultar: el archivo de avisos no se toca
        var CLAVE = "avisos_ocultos_hasta";
        var filas = Array.prototype.slice.call(document.querySelectorAll("tr[data-ts]"));

        function leerLimite() {
            try { return localStorage.getItem(CLAVE) || ""; } catch (e) { return ""; }
        }

        function guardarLimite(valor) {
            try {
                if (valor) localStorage.setItem(CLAVE, valor);
                else localStorage.removeItem(CLAVE);
            } catch (e) {}
        }

        function aplicarVista() {
            var limite = leerLimite();
            var visibles = { directo: 0, regla: 0 };
            var total = 0;
            filas.forEach(function (fila) {
                // Las fechas son AAAA-MM-DD HH:MM:SS: como texto se ordenan igual que por tiempo
                fila.hidden = Boolean(limite) && fila.dataset.ts <= limite;
                if (!fila.hidden) {
                    visibles[fila.dataset.tipo] += 1;
                    total += 1;
                }
            });
            document.getElementById("total-avisos").textContent = total;
            document.getElementById("total-directo").textContent = visibles.directo;
            document.getElementById("total-regla").textContent = visibles.regla;
            document.getElementById("limpiar").disabled = total === 0;
            document.getElementById("mostrar").hidden = total === filas.length;
            document.getElementById("sin-visibles").hidden = filas.length === 0 || total > 0;
        }

        document.getElementById("limpiar").addEventListener("click", function () {
            // La primera fila es el aviso mas reciente
            guardarLimite(filas[0].dataset.ts);
            aplicarVista();
        });

        document.getElementById("mostrar").addEventListener("click", function () {
            guardarLimite("");
            aplicarVista();
        });

        aplicarVista();

        // Recargar la pagina cerraria el payload que se esta leyendo
        setInterval(function () {
            if (!document.querySelector("tr:not([hidden]) details[open]")) location.reload();
        }, 5000);
    </script>
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

def resumir_directo(datos):
    cliente = datos.get("clientid", "Cliente desconocido")
    evento = datos.get("event")
    if evento == "client.connected":
        return f"{cliente} se conectó"
    if evento == "client.disconnected":
        motivo = datos.get("reason")
        return f"{cliente} se desconectó (motivo: {motivo})" if motivo else f"{cliente} se desconectó"
    return f"{cliente}: {evento or 'evento desconocido'}"

def resumir_regla(datos):
    cliente, topic = datos.get("clientid"), datos.get("topic")
    if cliente is None or topic is None:
        return "Faltan datos en el aviso"
    return f"{cliente} intentó publicar en {topic}: denegado"

def preparar_aviso(aviso):
    #se calcula al mostrar, asi tambien se ven bien los avisos ya guardados en el archivo
    es_directo = aviso["tipo"] == "directo"
    preparado = {
        **aviso,
        "nombre": "Conexión / desconexión" if es_directo else "Publicación denegada",
        "resumen": "Aviso sin datos para resumir",
        "crudo": aviso["payload"]
    }
    try:
        datos = json.loads(aviso["payload"])
    except json.JSONDecodeError:
        return preparado
    if isinstance(datos, dict):
        preparado["resumen"] = resumir_directo(datos) if es_directo else resumir_regla(datos)
        preparado["crudo"] = json.dumps(datos, indent=2, ensure_ascii=False)
    return preparado

@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE, avisos=[preparar_aviso(a) for a in avisos_log])

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
