import sys
import time
import subprocess
import argparse
from pathlib import Path

# Raíz del proyecto y ubicación de unidad.py
ROOT_DIR = Path(__file__).resolve().parent.parent
UBICACION_UNIDAD = Path(__file__).resolve().parent / "unidad.py"

# Lista de las 15 unidades de la flota
unidades = [
    "amb-01", "amb-02", "amb-03", "amb-04", "amb-05",
    "pol-01", "pol-02", "pol-03", "pol-04", "pol-05",
    "bom-01", "bom-02", "bom-03", "bom-04", "bom-05",
]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lanzador de Flota Demo (15 unidades)")
    parser.add_argument("--broker", type=str, default=None, help="IP del broker MQTT")
    parser.add_argument("--puerto", type=int, default=None, help="Puerto del broker MQTT (8883 para TLS, 1883 estándar)")
    parser.add_argument("--ca-certs", type=str, default=None, help="Ruta al archivo CA para TLS")
    parser.add_argument("--no-tls", action="store_true", help="Desactivar TLS y conectar en texto plano (puerto 1883)")

    args = parser.parse_args()

    cmd_extra = []
    if args.broker:
        cmd_extra.extend(["--broker", args.broker])
    if args.puerto:
        cmd_extra.extend(["--puerto", str(args.puerto)])
    if args.ca_certs:
        cmd_extra.extend(["--ca-certs", args.ca_certs])
    if args.no_tls:
        cmd_extra.append("--no-tls")

    procesos = []

    modo_str = "en texto plano (1883)" if args.no_tls or args.puerto == 1883 else "con TLS cifrado (8883)"
    print(f"[*] Iniciando las 15 unidades de la flota en segundo plano {modo_str}...")

    # Lanza cada unidad con Popen (como si abrieras 15 terminales)
    for u in unidades:
        cmd = [sys.executable, str(UBICACION_UNIDAD), "--id", u] + cmd_extra
        p = subprocess.Popen(cmd, cwd=ROOT_DIR)
        procesos.append(p)
        print("  -> Lanzado:", u)
        time.sleep(0.1)

    print("\n[OK] Flota completa en línea. Presioná Ctrl+C para detenerla.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Deteniendo todas las unidades...")
        for p in procesos:
            p.terminate()
        print("[OK] Flota detenida.")
