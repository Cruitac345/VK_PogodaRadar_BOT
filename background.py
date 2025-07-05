from flask import Flask, jsonify
from threading import Thread
import time
import requests
import os

app = Flask(__name__)

@app.route('/')
def home():
    return "VK Bot is running!"

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "timestamp": time.time()})

def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host="0.0.0.0", port=port)

def ping_self():
    time.sleep(60)
    app_url = os.environ.get('RENDER_EXTERNAL_URL')
    if not app_url:
        return
    
    while True:
        try:
            response = requests.get(f"{app_url}/health", timeout=30)
            print(f"Self-ping: {response.status_code}")
        except Exception as e:
            print(f"Ping error: {e}")
        time.sleep(600)

def keep_alive():
    Thread(target=run, daemon=True).start()
    Thread(target=ping_self, daemon=True).start()
