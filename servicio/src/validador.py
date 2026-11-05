"""Validador geoespacial y temporal para reportes de telemetría (Issue #9).

Módulo puro sin dependencias externas (solo math, json, time, pathlib).
Valida que las posiciones reportadas por las unidades:
1. No estén desfasadas respecto al reloj del servicio (ventana de tolerancia).
2. Sean monótonas en el tiempo (ts estrictamente creciente respecto al último válido).
3. Se encuentren dentro de la zona operativa terrestre de Comodoro Rivadavia (sin pisar el mar).
4. No representen un salto físicamente imposible (velocidad calculada <= MAX_VELOCIDAD_MS).
"""

import json
import math
import time
from pathlib import Path

# Constantes del contrato (docs/dudas_resueltas.md)
MAX_VELOCIDAD_MS = 75.0  # Límite físico: 150 metros en 2 segundos (~270 km/h)
TOLERANCIA_TS = 10.0     # Ventana máxima de diferencia con el reloj del servidor (segundos)

# Ruta por defecto al GeoJSON de la zona operativa
RUTA_GEOJSON_DEFAULT = Path(__file__).resolve().parent.parent.parent / "comun" / "zona.geojson"


def _cargar_poligono(ruta_archivo: Path | str):
    """Carga los vértices del polígono y calcula su Bounding Box en memoria."""
    ruta = Path(ruta_archivo)
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el archivo de zona: {ruta}")

    with open(ruta, "r", encoding="utf-8") as f:
        datos = json.load(f)

    # Extraer coordenadas del primer polígono en el FeatureCollection
    vertices = datos["features"][0]["geometry"]["coordinates"][0]

    min_lon = min(pt[0] for pt in vertices)
    max_lon = max(pt[0] for pt in vertices)
    min_lat = min(pt[1] for pt in vertices)
    max_lat = max(pt[1] for pt in vertices)

    bbox = (min_lon, max_lon, min_lat, max_lat)
    return vertices, bbox


# Carga única en memoria al importar el módulo
VERTICES_ZONA, BBOX_ZONA = _cargar_poligono(RUTA_GEOJSON_DEFAULT)


def distancia_metros(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula la distancia geodésica en metros entre dos coordenadas (fórmula de Haversine)."""
    r_tierra = 6371000.0  # Radio medio de la Tierra en metros
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r_tierra * c


def en_zona(lon: float, lat: float, vertices=None, bbox=None) -> bool:
    """Determina si una coordenada (lon, lat) está dentro del polígono terrestre de Comodoro Rivadavia.

    Aplica primero un filtro O(1) de Bounding Box, y si entra, el algoritmo Ray-Casting (Jordan curve).
    """
    if vertices is None:
        vertices = VERTICES_ZONA
    if bbox is None:
        bbox = BBOX_ZONA

    min_lon, max_lon, min_lat, max_lat = bbox

    # 1. Filtro rápido O(1): fuera del Bounding Box
    if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
        return False

    # 2. Algoritmo Ray-Casting sobre los segmentos del polígono
    dentro = False
    n = len(vertices)
    j = n - 1

    for i in range(n):
        xi, yi = vertices[i]  # lon, lat
        xj, yj = vertices[j]

        # Chequear si la semirrecta horizontal cruza el segmento
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            dentro = not dentro

        j = i

    return dentro


def validar_posicion(
    payload: dict,
    ultimo_estado: dict | None = None,
    hora_servicio: float | None = None,
) -> tuple[bool, str | None, str | None]:
    """Evalúa la validez física y temporal de un reporte de telemetría.

    Orden de evaluación (menor a mayor costo computacional):
    1. Ventana de reloj del servicio (|hora_servicio - ts| <= TOLERANCIA_TS). - Tolerancia ante el clock shift
    2. Monotonía temporal (ts > ultimo_ts). - Envio de paquetes viejos ante caidas de la red
    3. Zona geográfica (Bounding box + Ray-Casting). - Caiga dentro de zona.geoJSON (ZONA DE COMODORO RIVADAVIA)
    4. Salto imposible (distancia / dt <= MAX_VELOCIDAD_MS). - Velocidad en la que iria la unidad en cuestion metros/seg

    Retorna:
        (es_valido: bool, codigo_error: str | None, detalle: str | None)
    """
    if hora_servicio is None:
        hora_servicio = time.time()

    ts = payload["ts"]
    lat = payload["lat"]
    lon = payload["lon"]

    # 1. Ventana del reloj del servicio: impide adelantar o atrasar ts maliciosamente
    if abs(hora_servicio - ts) > TOLERANCIA_TS:
        return (
            False,
            "ts_desfasado",
            f"Timestamp {ts} desfasado por {abs(hora_servicio - ts):.1f}s del reloj del servicio (tolerancia: {TOLERANCIA_TS}s)",
        )

    # 2. Monotonía temporal: el timestamp debe ser estrictamente posterior al último aceptado
    if ultimo_estado and ultimo_estado.get("ts") is not None:
        if ts <= ultimo_estado["ts"]:
            return (
                False,
                "ts_repetido_o_viejo",
                f"Timestamp {ts} es menor o igual al último aceptado ({ultimo_estado['ts']})",
            )

    # 3. Zona geográfica: no puede caer en el mar ni fuera del radio operativo
    if not en_zona(lon, lat):
        return (
            False,
            "fuera_de_zona",
            f"Coordenadas lat={lat:.4f}, lon={lon:.4f} fuera de la zona operativa de Comodoro Rivadavia",
        )

    # 4. Salto imposible: verificar velocidad si hay posición previa y no viene de reconectarse
    if (
        ultimo_estado
        and ultimo_estado.get("lat") is not None
        and ultimo_estado.get("lon") is not None
        and ultimo_estado.get("ts") is not None
        and not ultimo_estado.get("recien_conectado", False)
    ):
        dt = ts - ultimo_estado["ts"]
        if dt > 0:
            dist = distancia_metros(ultimo_estado["lat"], ultimo_estado["lon"], lat, lon)
            vel = dist / dt
            if vel > MAX_VELOCIDAD_MS:
                return (
                    False,
                    "salto_imposible",
                    f"Velocidad calculada ({vel:.1f} m/s) excede el límite máximo ({MAX_VELOCIDAD_MS} m/s)",
                )

    return True, None, None
