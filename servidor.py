from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic

app = Flask(__name__)
CORS(app)

import os
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

@app.route('/analizar', methods=['POST'])
def analizar():
    datos = request.json.get('datos')
    
    prompt = f"""Eres un analista experto en logística y transporte en México.
    
Analiza estos datos de viajes. Primero identifica qué columnas hay disponibles, luego genera el análisis más útil posible.

Si hay datos de costo y kilómetros: calcula costo por km por viaje e identifica las rutas más y menos eficientes.
Si hay kilómetros acumulados: identifica unidades que puedan necesitar mantenimiento pronto.
Si hay tiempos: analiza retrasos y patrones.

Sé específico con números. Usa emojis para que sea fácil de leer. Termina con 2-3 recomendaciones concretas.

Datos:
{datos}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )
    return jsonify({"resultado": message.content[0].text})

if __name__ == '__main__':
    app.run(port=3000)