from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key="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")

@app.route('/analizar', methods=['POST'])
def analizar():
    datos = request.json.get('datos')
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Analiza estos datos de viajes de transporte y dame un reporte con el viaje más caro por km y el más eficiente:\n{datos}"
        }]
    )
    return jsonify({"resultado": message.content[0].text})

if __name__ == '__main__':
    app.run(port=3000)