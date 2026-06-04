from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import os
import json
import threading
import uuid

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# job_id -> {"status": "procesando"|"listo"|"error", ...}
jobs = {}


@app.route('/')
def index():
    return send_file('index.html')


@app.route('/analizar', methods=['POST'])
def analizar():
    datos = (request.json or {}).get('datos')
    if not datos:
        return jsonify({"ok": False, "error": "No se recibió el campo 'datos'"}), 400

    prompt = f"""Eres un analista experto en logística y transporte en México.
Analiza los siguientes datos de viajes de una empresa transportista.

INSTRUCCIONES CRÍTICAS:
- Responde ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después, sin backticks.

El JSON debe tener EXACTAMENTE esta estructura:

{{
  "resumen": {{
    "total_viajes": null,
    "periodo": "No especificado",
    "flota_activa": null,
    "costo_total": null,
    "km_totales": null
  }},
  "kpis": [
    {{"label": "Costo Promedio por Viaje", "valor": "0", "unidad": "MXN", "tendencia": "neutro"}},
    {{"label": "Costo por Kilómetro", "valor": "0", "unidad": "MXN/km", "tendencia": "neutro"}},
    {{"label": "Eficiencia Promedio", "valor": "0", "unidad": "km/viaje", "tendencia": "neutro"}},
    {{"label": "Utilización de Flota", "valor": "0", "unidad": "%", "tendencia": "neutro"}}
  ],
  "rutas_eficiencia": [
    {{"ruta": "origen-destino", "costo_km": 0, "viajes": 0, "status": "eficiente"}}
  ],
  "unidades_rendimiento": [
    {{"unidad": "ID", "km_totales": 0, "costo_total": 0, "costo_km": 0, "alerta": false, "motivo_alerta": null}}
  ],
  "costos_por_categoria": [
    {{"categoria": "Combustible", "monto": 0, "porcentaje": 0}}
  ],
  "viajes_por_periodo": [
    {{"periodo": "Ene", "viajes": 0, "costo": 0}}
  ],
  "alertas": [
    {{"tipo": "info", "titulo": "titulo", "descripcion": "descripcion", "accion": "accion"}}
  ],
  "recomendaciones": [
    {{"prioridad": "alta", "titulo": "titulo", "descripcion": "descripcion", "ahorro_estimado": "0"}}
  ],
  "conclusiones": "resumen ejecutivo aqui"
}}

DATOS A ANALIZAR:
{datos}"""

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "procesando"}

    def call_claude():
        try:
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
            analisis = json.loads(raw.strip())
            jobs[job_id] = {"status": "listo", "analisis": analisis}
        except Exception as e:
            jobs[job_id] = {"status": "error", "error": str(e)}

    threading.Thread(target=call_claude, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route('/resultado/<job_id>', methods=['GET'])
def resultado(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"status": "error", "error": "Job no encontrado"}), 404
    return jsonify(job)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 3000))
    app.run(port=port, threaded=True)
