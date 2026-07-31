import os
import re
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
        return float(str(val).replace(",", "").replace("$", "").replace(" ", "").strip())
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

    print(f"[DEBUG] auto-detección: costo={_auto_costo!r}, km={_auto_km!r}, "
          f"ruta={_auto_ruta!r}, unidad={_auto_unidad!r}, mes={_auto_mes!r}", flush=True)

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

    print(f"[DEBUG] columnas finales: costo={col_costo!r}, km={col_km!r}, "
          f"ruta={col_ruta!r}, origen={col_origen!r}, dest={col_dest!r}, "
          f"unidad={col_unidad!r}, mes={col_mes!r}", flush=True)

    suma_costos = 0.0
    suma_km     = 0.0
    n_costo     = 0
    n_km        = 0
    total_filas = 0

    rutas    = defaultdict(lambda: {"viajes": 0, "suma_costo": 0.0, "suma_km": 0.0})
    unidades = defaultdict(lambda: {"viajes": 0, "suma_costo": 0.0, "suma_km": 0.0})
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
                if km is not None:
                    unidades[uid]["suma_km"] += km

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
            "km_total":     _r(v["suma_km"]),
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
    <una entrada por cada ruta en calculos.por_ruta — copia costo_por_km, viajes, km_total y costo_total exactos,
     asigna eficiencia "alta"/"media"/"baja" según costo_por_km relativo al promedio general>
  ],
  "unidades_rendimiento": [
    <una entrada por cada unidad en calculos.por_unidad — copia viajes, costo_total y km_total exactos,
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

@app.route("/landing")
def landing():
    return send_file("landing.html")


@app.route("/analizar", methods=["POST"])
def analizar():
    body  = request.json or {}
    datos = body.get("datos")
    if not datos:
        return jsonify({"ok": False, "error": "No se recibió el campo 'datos'"}), 400

    # col_map es opcional; si viene del frontend se pasa directo a precalcular()
    col_map_raw = body.get("col_map") or {}
    # Sanear: solo conservar claves válidas con valores string no vacíos
    col_map = {k: v for k, v in col_map_raw.items()
               if k in ("costo", "km", "ruta", "unidad", "mes") and isinstance(v, str) and v.strip()}

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "analisis": None, "error": None}

    thread = threading.Thread(target=procesar, args=(job_id, datos, col_map), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id}), 202


_MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


@app.route("/analizar_whatsapp", methods=["POST"])
def analizar_whatsapp():
    """Analiza los viajes capturados por WhatsApp usando el mismo pipeline
    que el flujo de Excel: los convierte a CSV y reutiliza procesar()."""
    try:
        conn = _db_conn()
        if conn is None:
            return jsonify({"ok": False, "error": "Sin base de datos configurada"}), 500
        _db_init(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT fecha, telefono, origen, destino, km, costo, clave FROM viajes_whatsapp "
                        "WHERE origen IS NOT NULL AND km IS NOT NULL AND costo IS NOT NULL "
                        "ORDER BY fecha")
            filas = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if not filas:
        return jsonify({"ok": False, "error": "Aún no hay viajes capturados por WhatsApp"}), 400

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Origen", "Destino", "Km", "Costo Total", "Unidad", "Mes"])
    for fecha, telefono, origen, destino, km, costo, clave in filas:
        mes = f"{_MESES_ES[fecha.month - 1]} {fecha.year}" if fecha else ""
        unidad = clave or (telefono or "").replace("whatsapp:", "")
        writer.writerow([origen, destino, km, costo, unidad, mes])

    col_map = {"costo": "Costo Total", "km": "Km",
               "ruta": "__combo__Origen||Destino", "unidad": "Unidad", "mes": "Mes"}

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "analisis": None, "error": None}
    thread = threading.Thread(target=procesar, args=(job_id, buf.getvalue(), col_map), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id, "viajes": len(filas)}), 202


@app.route("/resultado/<job_id>", methods=["GET"])
def resultado(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "Job no encontrado"}), 404
    return jsonify({"ok": True, **job})


# ---------- WhatsApp: captura de viajes ----------

def _db_conn():
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    return psycopg2.connect(url)


# Catálogo por defecto de mantenimiento (editable). Intervalos en km.
# importancia: 'critico' (seguridad) o 'rutina'.
_CATALOGO_DEFAULT = [
    ("Cambio de aceite y filtro", 15000, "rutina"),
    ("Rotación / revisión de llantas", 20000, "critico"),
    ("Balatas y frenos", 40000, "critico"),
    ("Afinación mayor", 60000, "rutina"),
    ("Revisión de suspensión", 50000, "critico"),
]


def _db_init(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS viajes_whatsapp (
                id SERIAL PRIMARY KEY,
                fecha TIMESTAMP DEFAULT NOW(),
                telefono TEXT,
                mensaje TEXT,
                origen TEXT,
                destino TEXT,
                km REAL,
                costo REAL
            )
        """)
        # Clave de unidad en cada viaje (nuevo modelo: unidad = clave, no teléfono)
        cur.execute("ALTER TABLE viajes_whatsapp ADD COLUMN IF NOT EXISTS clave TEXT")

        # Registro de unidades (camiones)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS unidades (
                clave TEXT PRIMARY KEY,
                tipo TEXT,
                anio INTEGER,
                km_actual REAL DEFAULT 0,
                creado TIMESTAMP DEFAULT NOW()
            )
        """)

        # Catálogo de tipos de mantenimiento (intervalos e importancia)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mantenimiento_catalogo (
                id SERIAL PRIMARY KEY,
                nombre TEXT UNIQUE,
                intervalo_km REAL,
                importancia TEXT
            )
        """)
        cur.execute("SELECT COUNT(*) FROM mantenimiento_catalogo")
        if cur.fetchone()[0] == 0:
            for nombre, intervalo, imp in _CATALOGO_DEFAULT:
                cur.execute(
                    "INSERT INTO mantenimiento_catalogo (nombre, intervalo_km, importancia) "
                    "VALUES (%s, %s, %s) ON CONFLICT (nombre) DO NOTHING",
                    (nombre, intervalo, imp),
                )

        # Historial: último servicio de cada pieza por unidad
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mantenimiento_historial (
                id SERIAL PRIMARY KEY,
                clave TEXT,
                tipo_id INTEGER,
                ultimo_km REAL,
                ultima_fecha DATE,
                UNIQUE (clave, tipo_id)
            )
        """)
    conn.commit()


def _es_clave(token):
    """Una clave de unidad tiene letras y al menos un dígito (ej. C40T, C240T2, U12)."""
    token = token.strip().strip(",")
    return bool(re.match(r'^[A-Za-z]+\d+[A-Za-z0-9]*$', token))


def parsear_viaje(texto):
    """Extrae clave, origen, destino, km y costo de un mensaje tipo
    'C40T Monterrey-Saltillo, 320km, $2500'. La clave al inicio es opcional."""
    clave = None
    origen = destino = None
    km = costo = None

    # Clave de unidad = primer token si tiene forma de clave (letras + dígitos)
    tokens = texto.strip().split()
    if tokens and _es_clave(tokens[0]):
        clave = tokens[0].strip().strip(",").upper()
        texto = texto.strip()[len(tokens[0]):].strip(" ,")

    km_match = re.search(r'(\d[\d,]*(?:\.\d+)?)\s*km', texto, re.IGNORECASE)
    if km_match:
        km = float(km_match.group(1).replace(",", ""))

    texto_sin_km = texto[:km_match.start()] + texto[km_match.end():] if km_match else texto
    costo_match = re.search(r'\$\s*([\d,]+(?:\.\d+)?)', texto_sin_km)
    if not costo_match:
        nums = re.findall(r'(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w])', texto_sin_km)
        if nums:
            costo = float(nums[-1].replace(",", ""))
    else:
        costo = float(costo_match.group(1).replace(",", ""))

    primera_parte = texto.split(",")[0].strip()
    primera_parte = re.sub(r'(\d[\d,]*(?:\.\d+)?)\s*km', "", primera_parte, flags=re.IGNORECASE)
    primera_parte = re.sub(r'\$\s*[\d,]*(?:\.\d+)?', "", primera_parte)
    primera_parte = re.sub(r'\d[\d,]*(?:\.\d+)?\s*$', "", primera_parte).strip(" -,")
    if "-" in primera_parte:
        partes = [p.strip() for p in primera_parte.split("-", 1)]
        origen, destino = partes[0], partes[1]
    elif " a " in primera_parte.lower():
        idx = primera_parte.lower().index(" a ")
        origen, destino = primera_parte[:idx].strip(), primera_parte[idx + 3:].strip()
    else:
        palabras = primera_parte.split()
        if len(palabras) >= 2:
            origen = palabras[0]
            destino = " ".join(palabras[1:])

    return clave, origen, destino, km, costo


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    from twilio.twiml.messaging_response import MessagingResponse

    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "")
    print(f"DEBUG WHATSAPP - {from_number}: {incoming_msg}")

    clave, origen, destino, km, costo = parsear_viaje(incoming_msg)

    guardado = False
    try:
        conn = _db_conn()
        if conn is not None:
            _db_init(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO viajes_whatsapp (telefono, mensaje, origen, destino, km, costo, clave) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (from_number, incoming_msg, origen, destino, km, costo, clave),
                )
                # Si hay clave y km, acumular kilometraje en la unidad (crearla si no existe)
                if clave and km:
                    cur.execute(
                        "INSERT INTO unidades (clave, km_actual) VALUES (%s, %s) "
                        "ON CONFLICT (clave) DO UPDATE SET km_actual = unidades.km_actual + %s",
                        (clave, km, km),
                    )
            conn.commit()
            conn.close()
            guardado = True
    except Exception as e:
        print(f"DEBUG WHATSAPP DB ERROR - {e}")

    resp = MessagingResponse()
    if origen and destino and km and costo:
        encabezado = f"Viaje registrado ✅ (unidad {clave})" if clave else "Viaje registrado ✅"
        texto = f"{encabezado}\n{origen} → {destino}\n{km:,.0f} km | ${costo:,.0f}"
        if not guardado:
            texto += "\n(⚠️ sin base de datos conectada)"
    else:
        texto = ("No pude leer todos los datos 🤔\n"
                 "Mándalo así: C40T Origen-Destino, 320km, $2500")
    resp.message(texto)
    return str(resp), 200, {"Content-Type": "application/xml"}


@app.route("/viajes", methods=["GET"])
def viajes_capturados():
    try:
        conn = _db_conn()
        if conn is None:
            return jsonify({"ok": False, "error": "Sin DATABASE_URL configurada"}), 500
        _db_init(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT fecha, telefono, origen, destino, km, costo, mensaje FROM viajes_whatsapp ORDER BY fecha DESC LIMIT 200")
            filas = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    rows_html = "".join(
        f"<tr><td>{f[0]:%d/%m/%Y %H:%M}</td><td>{f[1] or ''}</td><td>{f[2] or '?'}</td>"
        f"<td>{f[3] or '?'}</td><td>{f[4] or '?'}</td><td>{f[5] or '?'}</td><td>{f[6]}</td></tr>"
        for f in filas
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Viajes por WhatsApp</title>
<style>body{{font-family:sans-serif;margin:2rem}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:6px 10px;text-align:left}}th{{background:#f4f4f4}}</style></head>
<body><h2>Viajes capturados por WhatsApp ({len(filas)})</h2>
<table><tr><th>Fecha</th><th>Teléfono</th><th>Origen</th><th>Destino</th><th>Km</th><th>Costo</th><th>Mensaje original</th></tr>
{rows_html}</table></body></html>"""


# ---------- Mantenimiento: API ----------

def _estado_pieza(km_actual, ultimo_km, intervalo_km):
    """Devuelve (estado, faltan_km, proximo_km) para una pieza de una unidad."""
    base = ultimo_km if ultimo_km is not None else 0
    proximo = base + (intervalo_km or 0)
    faltan = proximo - (km_actual or 0)
    if intervalo_km and faltan <= 0:
        estado = "vencido"
    elif intervalo_km and faltan <= 0.15 * intervalo_km:
        estado = "proximo"
    else:
        estado = "ok"
    return estado, round(faltan, 1), round(proximo, 1)


@app.route("/api/unidades", methods=["GET"])
def api_unidades():
    try:
        conn = _db_conn()
        if conn is None:
            return jsonify({"ok": False, "error": "Sin base de datos"}), 500
        _db_init(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT clave, tipo, anio, km_actual FROM unidades ORDER BY clave")
            filas = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "unidades": [
        {"clave": f[0], "tipo": f[1], "anio": f[2], "km_actual": f[3] or 0} for f in filas
    ]})


@app.route("/api/unidad", methods=["POST"])
def api_unidad_alta():
    body = request.json or {}
    clave = (body.get("clave") or "").strip().upper()
    if not clave:
        return jsonify({"ok": False, "error": "Falta la clave"}), 400
    tipo = (body.get("tipo") or "").strip() or None
    anio = body.get("anio")
    km_inicial = body.get("km_inicial")
    try:
        anio = int(anio) if anio not in (None, "") else None
    except (ValueError, TypeError):
        anio = None
    try:
        km_inicial = float(km_inicial) if km_inicial not in (None, "") else None
    except (ValueError, TypeError):
        km_inicial = None
    try:
        conn = _db_conn()
        if conn is None:
            return jsonify({"ok": False, "error": "Sin base de datos"}), 500
        _db_init(conn)
        with conn.cursor() as cur:
            if km_inicial is not None:
                cur.execute(
                    "INSERT INTO unidades (clave, tipo, anio, km_actual) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (clave) DO UPDATE SET tipo=COALESCE(EXCLUDED.tipo, unidades.tipo), "
                    "anio=COALESCE(EXCLUDED.anio, unidades.anio), km_actual=EXCLUDED.km_actual",
                    (clave, tipo, anio, km_inicial),
                )
            else:
                cur.execute(
                    "INSERT INTO unidades (clave, tipo, anio) VALUES (%s, %s, %s) "
                    "ON CONFLICT (clave) DO UPDATE SET tipo=COALESCE(EXCLUDED.tipo, unidades.tipo), "
                    "anio=COALESCE(EXCLUDED.anio, unidades.anio)",
                    (clave, tipo, anio),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/catalogo", methods=["GET"])
def api_catalogo():
    try:
        conn = _db_conn()
        if conn is None:
            return jsonify({"ok": False, "error": "Sin base de datos"}), 500
        _db_init(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, nombre, intervalo_km, importancia FROM mantenimiento_catalogo ORDER BY id")
            filas = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "catalogo": [
        {"id": f[0], "nombre": f[1], "intervalo_km": f[2], "importancia": f[3]} for f in filas
    ]})


@app.route("/api/mantenimiento", methods=["GET"])
def api_mantenimiento():
    """Estado de mantenimiento por unidad y por pieza."""
    try:
        conn = _db_conn()
        if conn is None:
            return jsonify({"ok": False, "error": "Sin base de datos"}), 500
        _db_init(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT clave, tipo, anio, km_actual FROM unidades ORDER BY clave")
            unidades = cur.fetchall()
            cur.execute("SELECT id, nombre, intervalo_km, importancia FROM mantenimiento_catalogo ORDER BY id")
            catalogo = cur.fetchall()
            cur.execute("SELECT clave, tipo_id, ultimo_km, ultima_fecha FROM mantenimiento_historial")
            hist = {(h[0], h[1]): (h[2], h[3]) for h in cur.fetchall()}
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    resultado = []
    for clave, tipo, anio, km_actual in unidades:
        km_actual = km_actual or 0
        piezas = []
        for cid, nombre, intervalo, imp in catalogo:
            ultimo_km, ultima_fecha = hist.get((clave, cid), (None, None))
            estado, faltan, proximo = _estado_pieza(km_actual, ultimo_km, intervalo)
            piezas.append({
                "tipo_id": cid, "nombre": nombre, "importancia": imp,
                "intervalo_km": intervalo, "ultimo_km": ultimo_km,
                "ultima_fecha": ultima_fecha.isoformat() if ultima_fecha else None,
                "proximo_km": proximo, "faltan_km": faltan, "estado": estado,
                "registrado": (clave, cid) in hist,
            })
        # Prioridad de la unidad: vencido crítico > vencido > próximo > ok
        orden = {"vencido": 0, "proximo": 1, "ok": 2}
        peor = min((orden[p["estado"]] for p in piezas), default=2)
        resultado.append({
            "clave": clave, "tipo": tipo, "anio": anio,
            "km_actual": round(km_actual, 1), "piezas": piezas, "prioridad": peor,
        })
    resultado.sort(key=lambda u: u["prioridad"])
    return jsonify({"ok": True, "unidades": resultado})


@app.route("/api/mantenimiento/registrar", methods=["POST"])
def api_mant_registrar():
    """Guarda el último servicio conocido de una pieza (mapeo inicial)."""
    body = request.json or {}
    clave = (body.get("clave") or "").strip().upper()
    tipo_id = body.get("tipo_id")
    ultimo_km = body.get("ultimo_km")
    ultima_fecha = body.get("ultima_fecha") or None
    if not clave or tipo_id is None:
        return jsonify({"ok": False, "error": "Falta clave o tipo_id"}), 400
    try:
        ultimo_km = float(ultimo_km) if ultimo_km not in (None, "") else None
    except (ValueError, TypeError):
        ultimo_km = None
    try:
        conn = _db_conn()
        if conn is None:
            return jsonify({"ok": False, "error": "Sin base de datos"}), 500
        _db_init(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mantenimiento_historial (clave, tipo_id, ultimo_km, ultima_fecha) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (clave, tipo_id) "
                "DO UPDATE SET ultimo_km=EXCLUDED.ultimo_km, ultima_fecha=EXCLUDED.ultima_fecha",
                (clave, int(tipo_id), ultimo_km, ultima_fecha),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/mantenimiento/servicio", methods=["POST"])
def api_mant_servicio():
    """Marca servicio hecho HOY: reinicia el contador al km actual de la unidad."""
    body = request.json or {}
    clave = (body.get("clave") or "").strip().upper()
    tipo_id = body.get("tipo_id")
    if not clave or tipo_id is None:
        return jsonify({"ok": False, "error": "Falta clave o tipo_id"}), 400
    try:
        conn = _db_conn()
        if conn is None:
            return jsonify({"ok": False, "error": "Sin base de datos"}), 500
        _db_init(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT km_actual FROM unidades WHERE clave=%s", (clave,))
            row = cur.fetchone()
            km_actual = (row[0] if row else 0) or 0
            cur.execute(
                "INSERT INTO mantenimiento_historial (clave, tipo_id, ultimo_km, ultima_fecha) "
                "VALUES (%s, %s, %s, CURRENT_DATE) ON CONFLICT (clave, tipo_id) "
                "DO UPDATE SET ultimo_km=EXCLUDED.ultimo_km, ultima_fecha=CURRENT_DATE",
                (clave, int(tipo_id), km_actual),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "km_actual": km_actual})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(port=port)
