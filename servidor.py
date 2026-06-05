import os
import uuid
import json
import io
import csv
import math
import threading
from collections import defaultdict
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

jobs = {}


# ── Column detection ──────────────────────────────────────────────────────────

def _detectar_col(headers, patrones, excluir=None):
    for h in headers:
        hl = h.lower().strip()
        if any(p in hl for p in patrones):
            if excluir and any(e in hl for e in excluir):
                continue
            return h
    return None


def _to_float(val):
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _r(val):
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
        return None
    return round(val, 2)


# ── Pre-calculation (stdlib only: csv, io, collections) ───────────────────────

def precalcular(csv_str: str, col_map: dict = None) -> dict:
    """
    col_map (opcional) — claves: costo, km, ruta, unidad, mes.
    Si una clave viene con valor no vacío, se usa directamente en lugar de
    la detección automática para esa columna.
    Valor especial para ruta: "__combo__<Origen>||<Destino>" combina dos columnas.
    """
    reader = csv.DictReader(io.StringIO(csv_str))
    headers = reader.fieldnames or []
    cm = col_map or {}

    # ── Detección automática (fallback cuando col_map no cubre una clave) ──
    _auto_costo = _detectar_col(headers, ["total"],
                                 excluir=["km", "por", "rate", "tarifa"])
    if _auto_costo is None:
        _auto_costo = _detectar_col(headers, ["costo", "importe", "monto", "flete"],
                                     excluir=["por", "km", "rate", "tarifa"])
    _auto_km     = _detectar_col(headers, ["km", "kilo", "iló", "ilom", "ilóm", "distancia"],
                                  excluir=["costo", "por", "tarifa", "precio", "rate"])
    _auto_ruta   = _detectar_col(headers, ["ruta", "corredor"])
    _auto_origen = _detectar_col(headers, ["origen", "origin", "salida"])
    _auto_dest   = _detectar_col(headers, ["destino", "destination", "llegada"])
    _auto_unidad = _detectar_col(headers, ["unidad", "placa", "vehiculo", "economico", "tracto",
                                            "tipo de unidad", "tipo unidad"])
    _auto_mes    = _detectar_col(headers, ["mes", "month", "periodo", "period"])
    _auto_anio   = _detectar_col(headers, ["año", "anio", "anyo", "year"])

    # ── Aplicar col_map: si viene con valor, tiene prioridad sobre auto-detección ──
    col_costo  = cm.get("costo")  or _auto_costo
    col_km     = cm.get("km")     or _auto_km
    col_unidad = cm.get("unidad") or _auto_unidad
    col_mes    = cm.get("mes")    or _auto_mes
    col_anio   = _auto_anio  # col_map no expone año; se sigue auto-detectando

    # Ruta: soporta valor especial "__combo__<Origen>||<Destino>"
    _ruta_raw  = cm.get("ruta") or ""
    col_ruta   = None
    col_origen = _auto_origen
    col_dest   = _auto_dest
    if _ruta_raw.startswith("__combo__"):
        # El frontend mandó "🔗 Origen + Destino (combinar)" — extraer ambas columnas
        partes = _ruta_raw[len("__combo__"):].split("||", 1)
        if len(partes) == 2:
            col_origen, col_dest = partes[0], partes[1]
    elif _ruta_raw:
        col_ruta = _ruta_raw
    else:
        col_ruta = _auto_ruta

    suma_costos = 0.0
    suma_km     = 0.0
    n_costo     = 0
    n_km        = 0
    total_filas = 0

    rutas    = defaultdict(lambda: {"viajes": 0, "suma_costo": 0.0, "suma_km": 0.0})
    unidades = defaultdict(lambda: {"viajes": 0, "suma_costo": 0.0})
    periodos = defaultdict(lambda: {"viajes": 0, "suma_costo": 0.0})

    for row in reader:
        total_filas += 1

        costo = _to_float(row.get(col_costo)) if col_costo else None
        km    = _to_float(row.get(col_km))    if col_km    else None

        if costo is not None:
            suma_costos += costo
            n_costo += 1
        if km is not None:
            suma_km += km
            n_km += 1

        # Ruta label
        if col_ruta:
            ruta_key = str(row.get(col_ruta, "")).strip()
        elif col_origen and col_dest:
            ruta_key = f"{str(row.get(col_origen,'')).strip()} → {str(row.get(col_dest,'')).strip()}"
        else:
            ruta_key = None

        if ruta_key:
            rutas[ruta_key]["viajes"] += 1
            if costo is not None:
                rutas[ruta_key]["suma_costo"] += costo
            if km is not None:
                rutas[ruta_key]["suma_km"] += km

        if col_unidad:
            uid = str(row.get(col_unidad, "")).strip()
            if uid:
                unidades[uid]["viajes"] += 1
                if costo is not None:
                    unidades[uid]["suma_costo"] += costo

        # Período = Mes + Año (o solo Mes si no hay columna de año)
        if col_mes:
            mes_val  = str(row.get(col_mes,  "")).strip()
            anio_val = str(row.get(col_anio, "")).strip() if col_anio else ""
            periodo_key = f"{mes_val} {anio_val}".strip() if anio_val else mes_val
            if periodo_key:
                periodos[periodo_key]["viajes"] += 1
                if costo is not None:
                    periodos[periodo_key]["suma_costo"] += costo

    promedio_costo       = _r(suma_costos / n_costo) if n_costo else None
    promedio_km          = _r(suma_km     / n_km)    if n_km    else None
    promedio_costo_por_km = _r(suma_costos / suma_km) if suma_km else None

    por_ruta = []
    for ruta, v in sorted(rutas.items(), key=lambda x: -x[1]["viajes"]):
        sc, sk = v["suma_costo"], v["suma_km"]
        por_ruta.append({
            "ruta":          ruta,
            "viajes":        v["viajes"],
            "costo_total":   _r(sc),
            "km_total":      _r(sk),
            "costo_promedio": _r(sc / v["viajes"]) if v["viajes"] else None,
            "costo_por_km":  _r(sc / sk) if sk else None,
        })

    por_unidad = []
    for uid, v in sorted(unidades.items(), key=lambda x: -x[1]["viajes"]):
        por_unidad.append({
            "unidad":       uid,
            "viajes":       v["viajes"],
            "costo_total":  _r(v["suma_costo"]),
            "costo_promedio_viaje": _r(v["suma_costo"] / v["viajes"]) if v["viajes"] else None,
        })

    # Ordenar periodos cronológicamente (meses en español)
    _orden_mes = ["enero","febrero","marzo","abril","mayo","junio",
                  "julio","agosto","septiembre","octubre","noviembre","diciembre"]
    def _sort_periodo(k):
        k_lower = k.lower()
        for i, m in enumerate(_orden_mes):
            if m in k_lower:
                # extraer año si existe
                partes = k_lower.split()
                anio = next((p for p in partes if p.isdigit()), "9999")
                return (anio, i)
        return ("9999", 99)

    por_periodo = [
        {
            "periodo":      k,
            "viajes":       v["viajes"],
            "costo":        _r(v["suma_costo"]),
        }
        for k, v in sorted(periodos.items(), key=lambda x: _sort_periodo(x[0]))
    ]

    return {
        "columnas_detectadas": {
            "costo":  col_costo,
            "km":     col_km,
            "ruta":   col_ruta or (f"{col_origen}+{col_dest}" if col_origen and col_dest else None),
            "unidad": col_unidad,
            "mes":    col_mes,
            "anio":   col_anio,
        },
        "totales": {
            "total_filas":           total_filas,
            "suma_costos":           _r(suma_costos) if n_costo else None,
            "suma_km":               _r(suma_km)     if n_km    else None,
            "promedio_costo":        promedio_costo,
            "promedio_km":           promedio_km,
            "promedio_costo_por_km": promedio_costo_por_km,
        },
        "por_ruta":    por_ruta,
        "por_unidad":  por_unidad,
        "por_periodo": por_periodo,
    }


PROMPT_TEMPLATE = """Eres un analista experto en logística y transporte en México.
Responde ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después, sin backticks.

=== VALORES EXACTOS CALCULADOS POR PYTHON - NO MODIFICAR ===
{calculos}

=== INSTRUCCIONES ===
Usa los valores anteriores para llenar resumen y kpis literalmente.
Tu ÚNICO trabajo creativo es: rutas_eficiencia (eficiencia relativa entre rutas),
unidades_rendimiento (eficiencia_score relativo), alertas y recomendaciones.

Responde con esta estructura exacta:

{{
  "resumen": {{
    "total_viajes": <calculos.totales.total_filas — usa este número exacto>,
    "periodo":      "No especificado",
    "flota_activa": <número de entradas en calculos.por_unidad, o null si está vacío>,
    "costo_total":  <calculos.totales.suma_costos — usa este número exacto o null>,
    "km_totales":   <calculos.totales.suma_km — usa este número exacto o null>
  }},
  "kpis": [
    {{"label": "Costo Promedio por Viaje", "valor": <calculos.totales.promedio_costo>,    "unidad": "MXN",    "tendencia": "neutro"}},
    {{"label": "Costo por Km",             "valor": <calculos.totales.promedio_costo_por_km>, "unidad": "MXN/km", "tendencia": "neutro"}},
    {{"label": "Km Promedio por Viaje",    "valor": <calculos.totales.promedio_km>,       "unidad": "km",     "tendencia": "neutro"}},
    {{"label": "Total Viajes",             "valor": <calculos.totales.total_filas>,        "unidad": "viajes", "tendencia": "neutro"}}
  ],
  "rutas_eficiencia": [
    <una entrada por cada ruta en calculos.por_ruta — copia costo_por_km y viajes exactos,
     asigna eficiencia "alta"/"media"/"baja" según costo_por_km relativo al promedio general>
  ],
  "unidades_rendimiento": [
    <una entrada por cada unidad en calculos.por_unidad — copia viajes y costo_total exactos,
     asigna eficiencia_score 0-100 según costo_promedio_viaje relativo al promedio general>
  ],
  "costos_por_categoria": [],
  "viajes_por_periodo": <COPIA EXACTA de calculos.por_periodo como lista de objetos con "periodo","viajes","costo">,
  "alertas": [
    {{
      "tipo":        "<exactamente uno de: 'critica', 'advertencia', 'info'>",
      "titulo":      "<título corto de la alerta>",
      "descripcion": "<descripción detallada con números reales>",
      "accion":      "<acción recomendada para resolver>"
    }}
  ],
  "recomendaciones": [
    {{
      "prioridad":       "<exactamente uno de: 'alta', 'media', 'baja'>",
      "titulo":          "<título corto de la recomendación>",
      "descripcion":     "<descripción detallada con contexto>",
      "ahorro_estimado": "<monto estimado en MXN o porcentaje, ej: '$12,000 MXN/mes'>"
    }}
  ],
  "conclusiones": "<párrafo ejecutivo de 3-4 oraciones con los números reales>"
}}

REGLAS CRÍTICAS:
1) Copia los números de calculos exactamente. NUNCA recalcules ni redondees diferente.
2) Si un valor en calculos es null, escribe null en el JSON.
3) costos_por_categoria siempre [].
4) viajes_por_periodo: copia calculos.por_periodo tal cual. Si por_periodo está vacío, devuelve [].
5) En rutas_eficiencia usa el campo "costo_km" (NO "costo_por_km") con el valor de costo_por_km de cada ruta.

MUESTRA DE DATOS (primeras filas, solo para contexto):
{muestra}"""


def procesar(job_id: str, datos: str, col_map: dict = None):
    jobs[job_id]["status"] = "processing"
    try:
        calculos = precalcular(datos, col_map)

        lines = datos.splitlines()
        muestra = "\n".join(lines[:min(20, len(lines))])

        prompt = PROMPT_TEMPLATE.format(
            calculos=json.dumps(calculos, ensure_ascii=False, indent=2),
            muestra=muestra,
        )

        message = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        if raw.endswith("```"):
            raw = raw[:-3]
        analisis = json.loads(raw.strip())
        jobs[job_id]["status"] = "done"
        jobs[job_id]["analisis"] = analisis
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.route("/")
def index():
    return send_file("index.html")


@app.route("/analizar", methods=["POST"])
def analizar():
    body  = request.json or {}
    datos = body.get("datos")
    if not datos:
        return jsonify({"ok": False, "error": "No se recibió el campo 'datos'"}), 400

    # col_map es opcional; si viene del frontend se pasa directo a precalcular()
    col_map = body.get("col_map") or {}
    # Sanear: solo conservar claves válidas con valores string no vacíos
    col_map = {k: v for k, v in col_map.items()
               if k in ("costo", "km", "ruta", "unidad", "mes") and isinstance(v, str) and v.strip()}

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "analisis": None, "error": None}

    thread = threading.Thread(target=procesar, args=(job_id, datos, col_map), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id}), 202


@app.route("/resultado/<job_id>", methods=["GET"])
def resultado(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "Job no encontrado"}), 404
    return jsonify({"ok": True, **job})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(port=port)
