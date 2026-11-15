# Lumora-KI

Ein allgemeiner KI-Assistent im Browser, gebaut auf der Claude API.

## Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # dann ANTHROPIC_API_KEY eintragen
uvicorn app:app --reload
```

Dann http://localhost:8000 öffnen.

Persönlichkeit anpassen: `SYSTEM_PROMPT` in `app.py`. Modell ändern: `LUMORA_MODEL` in `.env`.
