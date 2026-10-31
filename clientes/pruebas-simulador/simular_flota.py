import time
import subprocess

# Lista de las 15 unidades de la flota
unidades = [
    "amb-01", "amb-02", "amb-03", "amb-04", "amb-05",
    "pol-01", "pol-02", "pol-03", "pol-04", "pol-05",
    "bom-01", "bom-02", "bom-03", "bom-04", "bom-05",
]

procesos = []
UBICACION_UNIDAD = "clientes/unidad.py"

print("[*] Iniciando las 15 unidades de la flota en segundo plano...")

# Lanza cada unidad con Popen (como si abrieras 15 terminales)
for u in unidades:
    p = subprocess.Popen(["python", UBICACION_UNIDAD, "--id", u])
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
