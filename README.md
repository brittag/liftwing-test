# Reference Need + Tone Check

Tiny Flask prototype that scores Wikipedia articles for **reference need** (uncited sentences) and **tone issues** via Wikimedia Lift Wing.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python app/app.py
```

Open http://localhost:8765 — paste article titles (one per line), set the language code, and score.
