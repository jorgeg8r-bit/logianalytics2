import os
import uuid
import json
import io
import threading
import math
import pandas as pd
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

jobs = {}

# ── Column detection helpers ──────────────────────────────────────────────────

def _find_col(df, patterns):
    """Return the first column whose name matches any pattern (case-insensitive)."""
    cols_lower = {c.lower(): c for c in df.columns}
    for pat in patterns:
        for lc, orig in cols_lower.items():
            if pat in lc:
                return orig
    return None


def _r(val):
    """Round to 2 decimals; return None if NaN/inf."""
    try:
        if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
            return None
        return round(float(val), 2)
    except Exception:
        return None


# ── Pre-calculation with pandas ───────────────────────────────────────────────

def precalcular(csv_str: str) -> dict:
    df = pd.read_csv(io.StringIO(csv_str))
    df.columns = df.columns.str.strip()

    col_costo  = _find_col(df, ["costo", "cost", "importe", "monto", "precio", "tarifa", "flete"])
    col_km     = _find_col(df, ["km", "kilo", "distancia", "distance"])
    col_ruta   = _find_col(df, ["ruta", "route", "corredor"])
    col_origen = _find_col(df, ["origen", "origin", "salida"])
    col_dest   = _find_col(df, ["destino", "destination", "llegada"])
    col_unidad = _find_col(df, ["unidad", "vehiculo", "vehicle", "placa", "economico", "tracto", "camion", "operador"])
    col_fecha  = _find_col(df, ["fecha", "date", "dia", "mes", "periodo"])

    total_viajes = len(df)

    # Numeric coercion
    s_costo = pd.to_numeric(df[col_costo], errors="coerce") if col_costo else None
    s_km    = pd.to_numeric(df[col_km],    errors="coerce") if col_km    else None

    costo_total         = _r(s_costo.sum())         if s_costo is not None else None
    km_totales          = _r(s_km.sum())             if s_km    is not None else None
    costo_promedio_viaje= _r(s_costo.mean())         if s_costo is not None else None
    km_promedio_viaje   = _r(s_km.mean())            if s_km    is not None else None
    costo_promedio_km   = _r(costo_total / km_totales) if (costo_total and km_totales) else None

    # Route label: prefer explicit "ruta", else "origen-destino"
    if col_ruta:
        df["_ruta"] = df[col_ruta].astype(str).str.strip()
    elif col_origen and col_dest:
        df["_ruta"] = df[col_origen].astype(str).str.strip() + " → " + df[col_dest].astype(str).str.strip()
    else:
        df["_ruta"] = None

    rutas = []
    if df["_ruta"].notna().any():
        grp = df.groupby("_ruta")
        for ruta, g in grp:
            sc = pd.to_numeric(g[col_costo], errors="coerce") if col_costo else None
            sk = pd.to_numeric(g[col_km],    errors="coerce") if col_km    else None
            c_sum = _r(sc.sum())  if sc is not None else None
            k_sum = _r(sk.sum())  if sk is not None else None
            c_avg = _r(sc.mean()) if sc is not None else None
            k_avg = _r(sk.mean()) if sk is not None else None
            c_km  = _r(c_sum / k_sum) if (c_sum and k_sum) else None
            rutas.append({
                "ruta":          str(ruta),
                "viajes":        int(len(g)),
                "costo_total":   c_sum,
                "km_total":      k_sum,
                "costo_promedio":c_avg,
                "km_promedio":   k_avg,
                "costo_por_km":  c_km,
            })
        rutas.sort(key=lambda r: r["viajes"], reverse=True)

    unidades = []
    if col_unidad:
        grp = df.groupby(df[col_unidad].astype(str).str.strip())
        for uid, g in grp:
            sc = pd.to_numeric(g[col_costo], errors="coerce") if col_costo else None
            sk = pd.to_numeric(g[col_km],    errors="coerce") if col_km    else None
            unidades.append({
                "unidad":     str(uid),
                "viajes":     int(len(g)),
                "km_total":   _r(sk.sum())  if sk is not None else None,
                "costo_total":_r(sc.sum())  if sc is not None else None,
                "costo_promedio_viaje": _r(sc.mean()) if sc is not None else None,
            })
        unidades.sort(key=lambda u: u["viajes"], reverse=True)

    columnas_disponibles = list(df.columns)

    return {
        "columnas_detectadas": {
            "costo":  col_costo,
            "km":     col_km,
            "ruta":   col_ruta or (f"{col_origen}+{col_dest}" if col_origen and col_dest else None),
            "unidad": col_unidad,
            "fecha":  col_fecha,
        },
        "totales": {
            "total_viajes":          total_viajes,
            "costo_total":           costo_total,
            "km_totales":            km_totales,
            "costo_promedio_viaje":  costo_promedio_viaje,
            "costo_promedio_km":     costo_promedio_km,
            "km_promedio_viaje":     km_promedio_viaje,
        },
        "por_ruta":   rutas,
        "por_unidad": unidades,
        "hay_fecha":  col_fecha is not None,
        "todas_las_columnas": columnas_disponibles,
    }


PROMPT_TEMPLATE = """Eres un analista experto en logística y transporte en México.
Responde ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después, sin backticks.

=== VALORES PRE-CALCULADOS POR PYTHON (pandas) ===
ESTOS SON LOS VALORES EXACTOS CALCULADOS POR PYTHON.
Úsalos literalmente en el JSON sin modificar ni recalcular ningún número.

{calculos}

=== INSTRUCCIONES ===
Con base en los valores anteriores, genera el JSON con esta estructura exacta:

{{
  "resumen": {{
    "total_viajes":  <usar calculos.totales.total_viajes>,
    "periodo":       <inferir del nombre de columna de fecha si existe, sino "No especificado">,
    "flota_activa":  <número de unidades únicas en calculos.por_unidad, o null si no hay columna de unidad>,
    "costo_total":   <usar calculos.totales.costo_total>,
    "km_totales":    <usar calculos.totales.km_totales>
  }},
  "kpis": [
    {{"label": "Costo Promedio por Viaje", "valor": <calculos.totales.costo_promedio_viaje>, "unidad": "MXN", "tendencia": "neutro"}},
    {{"label": "Costo por Km",             "valor": <calculos.totales.costo_promedio_km>,    "unidad": "MXN/km", "tendencia": "neutro"}},
    {{"label": "Km Promedio por Viaje",    "valor": <calculos.totales.km_promedio_viaje>,    "unidad": "km", "tendencia": "neutro"}},
    {{"label": "Total Viajes",             "valor": <calculos.totales.total_viajes>,          "unidad": "viajes", "tendencia": "neutro"}}
  ],
  "rutas_eficiencia": [
    {{
      "ruta":       <calculos.por_ruta[i].ruta>,
      "costo_km":   <calculos.por_ruta[i].costo_por_km>,
      "viajes":     <calculos.por_ruta[i].viajes>,
      "eficiencia": <"alta" si costo_por_km < promedio, "media" si cerca, "baja" si mayor>
    }}
  ],
  "unidades_rendimiento": [
    {{
      "unidad":         <calculos.por_unidad[i].unidad>,
      "viajes":         <calculos.por_unidad[i].viajes>,
      "km_total":       <calculos.por_unidad[i].km_total>,
      "costo_total":    <calculos.por_unidad[i].costo_total>,
      "eficiencia_score": <0-100 basado en costo_promedio_viaje relativo al promedio general>
    }}
  ],
  "costos_por_categoria": [],
  "viajes_por_periodo": [],
  "alertas": [
    <genera 3-5 alertas inteligentes basadas en los datos: rutas caras, unidades ineficientes, datos faltantes, etc.>
  ],
  "recomendaciones": [
    <genera 3-5 recomendaciones accionables con ahorro_estimado realista basado en los números>
  ],
  "conclusiones": <párrafo ejecutivo de 3-4 oraciones usando los números reales>
}}

REGLAS CRÍTICAS:
1) Copia los números de calculos exactamente. NUNCA recalcules ni estimes.
2) Si calculos.totales.X es null, usa null en el JSON (no inventes).
3) costos_por_categoria: siempre [] (no hay columnas de desglose detectadas automáticamente).
4) viajes_por_periodo: siempre [] (el frontend lo maneja por separado).
5) Tu único trabajo creativo: eficiencia relativa, alertas y recomendaciones.

DATOS ORIGINALES (primeras filas para contexto):
{muestra}"""


def procesar(job_id: str, datos: str):
    jobs[job_id]["status"] = "processing"
    try:
        calculos = precalcular(datos)

        # Send only a sample of raw rows to Claude for context (avoid huge prompts)
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
    datos = (request.json or {}).get("datos")
    if not datos:
        return jsonify({"ok": False, "error": "No se recibió el campo 'datos'"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "analisis": None, "error": None}

    thread = threading.Thread(target=procesar, args=(job_id, datos), daemon=True)
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
