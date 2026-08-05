"""Minimal Flask demo for Multilingual Reference Need scoring.

Run: python app/app.py
Open: http://localhost:8765
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

# Allow `python app/app.py` from repo root or app/
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from scoring import score_batch  # noqa: E402

STATIC_DIR = APP_DIR / "static"
PORT = int(os.environ.get("PORT", "8765"))

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.post("/api/score-batch")
def api_score_batch():
    body = request.get_json(silent=True) or {}
    titles = body.get("titles")
    lang = body.get("lang", "en")

    if isinstance(titles, str):
        titles = [line.strip() for line in titles.splitlines() if line.strip()]
    if not isinstance(titles, list):
        return jsonify({"error": "titles must be a list or newline-separated string"}), 400

    try:
        payload = score_batch(titles, lang=lang)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502

    return jsonify(payload)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
