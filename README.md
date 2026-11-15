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

## Deployment auf Render

1. Repository auf GitHub pushen, dann in Render: **New → Blueprint** und dieses Repo wählen (`render.yaml` wird erkannt).
2. Beim Anlegen `ANTHROPIC_API_KEY` eintragen.
3. Konten und Chats liegen in SQLite auf einer persistenten Disk (`/var/data`). Disks gibt es bei Render nur in bezahlten Plänen (hier `starter`); ohne Disk gingen die Daten bei jedem Deploy verloren.
