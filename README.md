# Lumora-KI

Ein allgemeiner KI-Assistent im Browser. Anbieter wählbar: Google Gemini (kostenlose Stufe) oder Claude.

## Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # dann GEMINI_API_KEY (gratis, aistudio.google.com) oder ANTHROPIC_API_KEY eintragen
uvicorn app:app --reload
```

Dann http://localhost:8000 öffnen.

Persönlichkeit anpassen: `SYSTEM_PROMPT` in `app.py`. Anbieter: `LUMORA_PROVIDER` (`gemini` oder `claude`), Modell ändern: `LUMORA_MODEL`.

## Deployment auf Render (kostenlos)

Der Webdienst läuft auf Renders Gratis-Plan, die Daten (Konten & Chats) liegen in einer kostenlosen Postgres-Datenbank bei Neon, weil der Gratis-Plan von Render keinen dauerhaften Speicher hat.

1. Auf neon.tech ein kostenloses Konto und ein Projekt anlegen, die **Connection string** (`postgresql://…`) kopieren.
2. Auf Render: **New → Blueprint**, dieses Repo (und den passenden Branch) wählen.
3. `GEMINI_API_KEY` (kostenlos auf aistudio.google.com → Get API key) und `DATABASE_URL` eintragen (`ANTHROPIC_API_KEY` bleibt leer), **Apply** klicken.

Hinweise: Der Gratis-Dienst schläft nach ca. 15 Minuten ohne Besucher ein; die erste Anfrage danach dauert ca. 30–60 Sekunden. Ohne `DATABASE_URL` nutzt die App lokal eine SQLite-Datei (`LUMORA_DB`).
