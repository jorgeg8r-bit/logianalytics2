from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import anthropic
import os
import json

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


@app.route('/')
def index():
    return send_file('index.html')


@app.route('/analizar', methods=['POST'])
def analizar():
    datos = (request.json or {}).get('datos')
    if not datos:
        return jsonify({"error": "No se recibió el campo 'datos'"}), 400

    # Limit data to avoid exceeding input token limits
    if len(datos) > 40000:
        datos = datos[:40000] + '\n[... datos truncados por tamaño ...]'

    prompt = f"""Eres un analista experto en logística y transporte en México.
Analiza estos datos de viajes de flota y devuelve ÚNICAMENTE un JSON válido con la estructura exacta que se muestra a continuación.
No incluyas texto fuera del JSON, ni bloques markdown, ni explicaciones. Solo el JSON puro.

Estructura requerida:
{{
  "resumen": "Párrafo breve con el resumen ejecutivo del análisis",
  "kpis": [
    {{"titulo": "Total Viajes", "valor": 0, "unidad": "viajes", "tendencia": "positiva", "cambio": "+5%"}},
    {{"titulo": "Costo Total", "valor": 0, "unidad": "MXN", "tendencia": "neutral", "cambio": "0%"}},
    {{"titulo": "Km Totales", "valor": 0, "unidad": "km", "tendencia": "positiva", "cambio": "+3%"}},
    {{"titulo": "Costo/Km Prom.", "valor": 0, "unidad": "MXN/km", "tendencia": "negativa", "cambio": "-2%"}}
  ],
  "rutas_eficiencia": [
    {{"ruta": "Nombre Ruta", "viajes": 0, "km_promedio": 0, "costo_promedio": 0, "costo_por_km": 0, "eficiencia": "alta"}}
  ],
  "unidades_rendimiento": [
    {{"unidad": "ID o Placa", "viajes": 0, "km_total": 0, "costo_total": 0, "eficiencia_score": 80}}
  ],
  "costos_por_categoria": [
    {{"categoria": "Combustible", "monto": 0, "porcentaje": 0}}
  ],
  "viajes_por_periodo": [
    {{"periodo": "Ene", "viajes": 0, "costo": 0, "km": 0}}
  ],
  "alertas": [
    {{"tipo": "critica", "titulo": "Título alerta", "descripcion": "Descripción detallada", "unidad": "ID opcional"}}
  ],
  "recomendaciones": [
    {{"prioridad": "alta", "titulo": "Título recomendación", "descripcion": "Descripción concreta y accionable", "ahorro_estimado": "$X,XXX MXN/mes"}}
  ],
  "conclusiones": "Párrafo con las conclusiones finales y próximos pasos sugeridos"
}}

Reglas:
- Todos los valores numéricos deben ser números, no strings.
- "tendencia" solo puede ser: "positiva", "negativa" o "neutral".
- "eficiencia" en rutas_eficiencia solo puede ser: "alta", "media" o "baja".
- "tipo" en alertas solo puede ser: "critica", "advertencia" o "info".
- "prioridad" en recomendaciones solo puede ser: "alta", "media" o "baja".
- "eficiencia_score" es un número de 0 a 100.
- Si faltan datos para una sección, usa arrays con al menos un elemento con valores estimados razonables.
- Sé específico con los números reales del dataset.

Datos del Excel:
{datos}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )
    except Exception as e:
        return jsonify({"error": f"Error al llamar a la API: {str(e)}"}), 502

    try:
        raw = message.content[0].text.strip()
        # Strip markdown code fences robustly
        if '```' in raw:
            raw = raw.split('```')[1]
            if raw.lower().startswith('json'):
                raw = raw[4:]
        # Find the JSON object boundaries
        start = raw.find('{')
        end = raw.rfind('}')
        if start == -1 or end == -1:
            raise ValueError("No se encontró un objeto JSON en la respuesta")
        raw = raw[start:end + 1]
        result = json.loads(raw)
    except Exception as e:
        return jsonify({"error": f"Respuesta de IA no válida: {str(e)}"}), 500

    return jsonify(result)


if __name__ == '__main__':
    app.run(port=3000, debug=True)
