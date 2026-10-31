"""
VIBECODEADO - Para generar las rutas con .json con OSRM a nuestro gusto.

Este script se ejecuta UNA SOLA VEZ para generar los 3 archivos JSON:
1. rutas/ambulancias.json -> Bases fijas en hospitales y centros de salud
2. rutas/bomberos.json    -> Bases fijas en cuarteles y destacamentos de bomberos
3. rutas/policias.json    -> n circuitos viales reales de patrullaje generados con OSRM
"""

import os
import json
import requests

# Carpeta de salida donde se guardarán los 3 archivos JSON
CARPETA_RUTAS = "clientes/rutas"

BASES_AMBULANCIAS = {
    "amb-01": {
        "nombre": "Hospital Regional Dr. V. M. Sanguinetti (Av. Yrigoyen 950)",
        "coordenadas": [[-45.8684, -67.4875]]
    },
    "amb-02": {
        "nombre": "Hospital Zonal Alvear (Km 3, J. R. Balcarce)",
        "coordenadas": [[-45.8344, -67.4619]]
    },
    "amb-03": {
        "nombre": "Clínica del Valle (Av. Alsina 656)",
        "coordenadas": [[-45.8639, -67.4944]]
    },
    "amb-04": {
        "nombre": "Clínica Pueyrredón (Olavarría 292, Zona Sur)",
        "coordenadas": [[-45.8821, -67.5210]]
    },
    "amb-05": {
        "nombre": "Hospital Militar Regional (Km 8, Italo Dell'Oro 850)",
        "coordenadas": [[-45.7960, -67.4286]]
    },
    "amb-06": {
        "nombre": "Centro de Salud Zona Norte (Km 5)",
        "coordenadas": [[-45.8185, -67.4510]]
    },
    "amb-07": {
        "nombre": "Centro de Salud Stella Maris (Zona Sur)",
        "coordenadas": [[-45.8935, -67.5215]]
    },
}

BASES_BOMBEROS = {
    "bom-01": {
        "nombre": "Cuartel Central (Huergo 995)",
        "coordenadas": [[-45.8587, -67.4890]]
    },
    "bom-02": {
        "nombre": "Destacamento 1 (Av. Kennedy 1100)",
        "coordenadas": [[-45.8628, -67.5155]]
    },
    "bom-03": {
        "nombre": "Destacamento 3 (Km 3, Álvaro Barros 27)",
        "coordenadas": [[-45.8379, -67.4824]]
    },
    "bom-04": {
        "nombre": "Destacamento 2 Km 8 'Malvinas Argentinas' (Italo Dell'Oro)",
        "coordenadas": [[-45.7960, -67.4386]]
    },
    "bom-05": {
        "nombre": "Destacamento 4 Laprida (Jamaica 460)",
        "coordenadas": [[-45.8280, -67.5450]]
    },
    "bom-06": {
        "nombre": "Destacamento Zona Norte (Km 5)",
        "coordenadas": [[-45.8230, -67.4560]]
    },
    "bom-07": {
        "nombre": "Destacamento Zona Sur (Stella Maris / Av. Yrigoyen)",
        "coordenadas": [[-45.8870, -67.5200]]
    },
}

ESQUINAS_POLICIAS = {
    "pol-01": {
        "nombre": "Patrulla Centro y Costanera",
        "esquinas": [
            (-45.8611, -67.4754),  # [E] San Martín y Güemes
            (-45.8621, -67.4800),  # [E] Av. Rivadavia y Pellegrini
            (-45.8621, -67.4861),  # [E] Av. Rivadavia y España
            (-45.8600, -67.4938),  # [E] Av. Alsina y Sarmiento
            (-45.8659, -67.4844),  # [E] Av. Hipólito Yrigoyen y Belgrano
            (-45.8620, -67.4733),  # [D] Plaza Soberanía (Av. Costanera)
            (-45.8611, -67.4754),  # Cierre
        ]
    },
    "pol-02": {
        "nombre": "Patrulla Av. Rivadavia y Av. Kennedy (Zona Sur)",
        "esquinas": [
            (-45.8680, -67.5217),  # [E] Av. Kennedy y Av. Estados Unidos
            (-45.8710, -67.5255),  # [D] Av. Kennedy 2314
            (-45.8701, -67.5323),  # [E] Av. Polonia y Lisandro de la Torre
            (-45.8766, -67.5218),  # [D] Av. Polonia 575
            (-45.8822, -67.5223),  # [D] Av. Roca 316
            (-45.8701, -67.5179),  # [D] Av. Estados Unidos 763
            (-45.8680, -67.5217),  # Cierre
        ]
    },
    "pol-03": {
        "nombre": "Patrulla Costanera Sur y Puerto",
        "esquinas": [
            (-45.8607, -67.4744),  # [D] Prefectura (Máximo Abasolo 951)
            (-45.8624, -67.4705),  # [D] Zona Franca (Av. Las Toninas)
            (-45.8620, -67.4733),  # [D] Plaza Soberanía / Costanera
            (-45.8621, -67.4800),  # [E] Av. Rivadavia y Pellegrini
            (-45.8679, -67.4882),  # [D] Av. Hipólito Yrigoyen 949
            (-45.8607, -67.4744),  # Cierre
        ]
    },
    "pol-04": {
        "nombre": "Patrulla Km 3 (General Mosconi)",
        "esquinas": [
            (-45.8344, -67.4619),  # [D] Hospital Zonal Alvear
            (-45.8369, -67.4767),  # [E] Acceso Libertador y Av. Tehuelches
            (-45.8379, -67.4824),  # [D] Álvaro Barros 27 (Destacamento 3)
            (-45.8344, -67.4619),  # Cierre
        ]
    },
    "pol-05": {
        "nombre": "Patrulla Stella Maris / Av. Yrigoyen Sur",
        "esquinas": [
            (-45.8767, -67.5024),  # [D] Av. Hipólito Yrigoyen 1848
            (-45.8804, -67.5110),  # [D] Av. Hipólito Yrigoyen 2308
            (-45.8857, -67.5213),  # [D] Av. Hipólito Yrigoyen 3057
            (-45.8947, -67.5232),  # [D] Barrio Stella Maris
            (-45.8767, -67.5024),  # Cierre
        ]
    },
    "pol-06": {
        "nombre": "Patrulla Zona Norte (Km 3 a Km 8)",
        "esquinas": [
            (-45.8344, -67.4619),  # Hospital Zonal Alvear (Km 3)
            (-45.8185, -67.4510),  # Km 5
            (-45.7960, -67.4386),  # Km 8 Malvinas Argentinas
            (-45.8185, -67.4510),  # Vuelta por Km 5
            (-45.8344, -67.4619),  # Cierre
        ]
    },
    "pol-07": {
        "nombre": "Patrulla Zona Oeste (Pueyrredón / Alsina / Yrigoyen)",
        "esquinas": [
            (-45.8821, -67.5210),  # Clínica Pueyrredón
            (-45.8701, -67.5179),  # Av. Estados Unidos 763
            (-45.8639, -67.4944),  # Clínica del Valle (Av. Alsina)
            (-45.8659, -67.4844),  # Av. Yrigoyen y Belgrano
            (-45.8767, -67.5024),  # Av. Yrigoyen 1848
            (-45.8821, -67.5210),  # Cierre
        ]
    }
}

def consultar_osrm(esquinas):
    """Llama a OSRM para convertir esquinas en el trazado vial real de calles."""
    coords_str = ";".join([f"{lon},{lat}" for lat, lon in esquinas])
    url = f"http://router.project-osrm.org/route/v1/driving/{coords_str}?overview=full&geometries=geojson"
    try:
        r = requests.get(url, headers={"User-Agent": "SistDistrComodoro/1.0"}, timeout=8)
        data = r.json()
        if data.get("code") == "Ok":
            coords = data["routes"][0]["geometry"]["coordinates"]
            return [(round(lat, 6), round(lon, 6)) for lon, lat in coords]
    except Exception as e:
        print(f"   [Aviso OSRM: {e}] Usando esquinas base en línea recta.")
    return esquinas


def main():
    os.makedirs(CARPETA_RUTAS, exist_ok=True)
    print("=== GENERANDO ARCHIVOS DE RUTAS Y BASES PARA COMODORO RIVADAVIA ===")

    # 1. Guardar ambulancias.json ======================
    ruta_amb = os.path.join(CARPETA_RUTAS, "ambulancias.json")
    with open(ruta_amb, "w", encoding="utf-8") as f:
        json.dump(BASES_AMBULANCIAS, f, indent=2, ensure_ascii=False)
    print(f"[OK] Generado: {ruta_amb} (5 bases de ambulancias)")

    # 2. Guardar bomberos.json ======================
    ruta_bom = os.path.join(CARPETA_RUTAS, "bomberos.json")
    with open(ruta_bom, "w", encoding="utf-8") as f:
        json.dump(BASES_BOMBEROS, f, indent=2, ensure_ascii=False)
    print(f"[OK] Generado: {ruta_bom} (5 cuarteles de bomberos)")

    # 3. Generar y guardar policias.json con OSRM ======================
    print("[*] Consultando OSRM para las 5 cuadrículas de policía...")
    resultado_policias = {}
    for id_pol, info in ESQUINAS_POLICIAS.items():
        print(f"   -> Generando circuito vial para {id_pol} ({info['nombre']})...")
        trazado_calles = consultar_osrm(info["esquinas"])
        resultado_policias[id_pol] = {
            "nombre": info["nombre"],
            "coordenadas": trazado_calles
        }
        print(f"      Puntos viales generados: {len(trazado_calles)}")

    ruta_pol = os.path.join(CARPETA_RUTAS, "policias.json")
    with open(ruta_pol, "w", encoding="utf-8") as f:
        json.dump(resultado_policias, f, indent=2, ensure_ascii=False)
    print(f"[OK] Generado: {ruta_pol} (5 cuadrículas policiales)")

    print("\n[LISTO] Los 3 archivos JSON están listos en la carpeta 'rutas/'.")


if __name__ == "__main__":
    main()
