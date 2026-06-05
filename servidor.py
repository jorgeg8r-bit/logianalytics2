from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import os
import json
import threading

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

jobs = {}


def procesar(job_id, datos):
    try:
        prompt = f"""Eres un analista experto en logística y transporte en México.
Analiza los siguientes datos de viajes de una empresa transportista.
Responde ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después, sin backticks.

{{
  "resumen": {{"total_viajes": null, "periodo": "No especificado", "flota_activa": null, "costo_total": null, "km_totales": null}},
  "kpis": [{{"label": "Costo Promedio por Viaje", "valor": "0", "unidad": "MXN", "tendencia": "neutro"}}],
  "rutas_eficiencia": [{{"ruta": "origen-destino", "costo_km": 0, "viajes": 0, "eficiencia": "alta"}}],
  "unidades_rendimiento": [{{"unidad": "ID", "viajes": 0, "km_total": 0, "costo_total": 0, "eficiencia_score": 80}}],
  "costos_por_categoria": [{{"categoria": "Combustible", "monto": 0, "porcentaje": 0}}],
  "viajes_por_periodo": [{{"periodo": "Ene", "viajes": 0, "costo": 0}}],
  "alertas": [{{"tipo": "info", "titulo": "titulo", "descripcion": "descripcion", "accion": "accion"}}],
  "recomendaciones": [{{"prioridad": "alta", "titulo": "titulo", "descripcion": "descripcion", "ahorro_estimado": "0"}}],
  "conclusiones": "resumen ejecutivo aqui"
}}

REGLA IMPORTANTE para costos_por_categoria:
- Úsalo SOLO si el Excel contiene columnas explícitas de desglose de costos (diesel, casetas, mantenimiento, sueldo, etc.).
- Si el Excel solo tiene un costo total por viaje sin desglose, devuelve "costos_por_categoria": [].
- NO inventes ni estimes proporciones.

DATOS:
{datos}"""

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        if raw.endswith("```"):
            raw = raw[:-3]
        jobs[job_id] = {"status": "listo", "analisis": json.loads(raw.strip())}
    except Exception as e:
        jobs[job_id] = {"status": "error", "error": str(e)}


@app.route('/')
def index():
    return send_file('index.html')


@app.route('/analizar', methods=['POST'])
def analizar():
    datos = (request.json or {}).get('datos')
    if not datos:
        return jsonify({"error": "No se recibió el campo 'datos'"}), 400
    job_id = os.urandom(8).hex()
    jobs[job_id] = {"status": "procesando"}
    threading.Thread(target=procesar, args=(job_id, datos), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route('/resultado/<job_id>')
def resultado(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "error", "error": "Job no encontrado"}), 404
    return jsonify(job)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 3000))
    app.run(port=port)
