# Lumora-KI

Ein allgemeiner KI-Assistent im Browser. Anbieter wählbar: Google Gemini (kostenlose Stufe) oder Claude.

## Sprachen

Die Oberfläche gibt es in 22 Sprachen (Deutsch, English, Français, Español, Italiano, Português, Nederlands,
Dansk, Slovenščina, Svenska, Norsk, Suomi, Polski, Čeština, Hrvatski, Magyar, Türkçe, Русский, Українська,
العربية, 日本語, 中文). Die Sprache wird beim ersten Besuch aus dem Browser erkannt und lässt sich über das
Weltkugel-Symbol oben ändern. Die KI antwortet immer in der Sprache, in der du schreibst. Neue Sprache: in
`static/i18n.js` einen Block kopieren und übersetzen. Der Admin-Bereich ist nur auf Deutsch.

## Pläne

| | Gratis | Pro |
|---|---|---|
| KI | Gemini | Claude |
| Limit | 30 000 Tokens/Tag (Admin ändert das) | unbegrenzt |
| Preis | – | 5.00 CHF/Monat (Admin ändert das) |

- Chats gehören dem Nutzer, nicht dem Plan: Man kann jeden Chat mit Gemini und mit Claude weiterführen.
- Kündigen ist jederzeit möglich, Pro läuft bis zum Ende des bezahlten Zeitraums.
- **Admin** (`Matic`, Start-Passwort `VIP`): gratis Pro, Preis ändern (laufende Abos enden dann zum Monatsende),
  Aktionen wie „Erster Monat gratis“, Gratis-Limit, Pro vergeben/entziehen. Admin-Rechte gibt es erst,
  nachdem das Start-Passwort geändert wurde (Konto → Passwort ändern).
- **Bezahlung:** `PAYMENTS_MODE=off` (Standard, nur der Admin kann Pro vergeben), `test` (simuliert, ohne Zahlung –
  nur zum Ausprobieren!) oder `stripe` (echt, braucht `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` und einen Webhook
  auf `/api/stripe/webhook` mit den Ereignissen `customer.subscription.created|updated|deleted` und
  `checkout.session.completed`).

## Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # dann GEMINI_API_KEY (gratis, aistudio.google.com) oder ANTHROPIC_API_KEY eintragen
uvicorn app:app --reload
```

Dann http://localhost:8000 öffnen.

Persönlichkeit anpassen: `SYSTEM_PROMPT` in `app.py`. Anbieter: `LUMORA_PROVIDER` (`gemini` oder `claude`), Modell ändern: `LUMORA_MODEL`.

## Bezahlung mit Stripe einrichten

Stripe zieht das Geld ein und schickt Lumora per Webhook Bescheid. Starte immer im **Testmodus** (kein echtes Geld).

1. **Konto:** auf stripe.com ein Konto anlegen und oben den **Testmodus** einschalten.
2. **Schlüssel:** Entwickler → API-Schlüssel → *Geheimer Schlüssel* (`sk_test_…`) kopieren.
3. **Webhook:** Entwickler → Webhooks → *Endpoint hinzufügen*
   - URL: `https://DEINE-SEITE.onrender.com/api/stripe/webhook`
   - Ereignisse: `customer.subscription.created`, `customer.subscription.updated`,
     `customer.subscription.deleted`, `checkout.session.completed`
   - danach das *Signing secret* (`whsec_…`) kopieren.
4. **Kundenportal** (Karte ändern, Rechnungen): Einstellungen → Billing → Kundenportal → aktivieren und speichern.
   Außerdem unter Einstellungen → Billing → Abos und E-Mails „Zahlung fehlgeschlagen“-Mails einschalten.
5. **Render → Environment:** `PAYMENTS_MODE=stripe`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
   `PUBLIC_URL=https://DEINE-SEITE.onrender.com`, `ANTHROPIC_API_KEY` eintragen → neu deployen.
6. **Testen:** „Pro holen“ → Testkarte `4242 4242 4242 4242`, beliebiges Ablaufdatum in der Zukunft und CVC.
   Nach wenigen Sekunden steht das Konto auf Pro. Dann kündigen und im Admin-Bereich den Preis ändern ausprobieren.
7. **Live gehen:** Stripe-Konto aktivieren, den Testmodus ausschalten und im Live-Modus **neu** einen Schlüssel
   (`sk_live_…`), einen Webhook (neues `whsec_…`) und das Kundenportal einrichten. Die Werte auf Render ersetzen.

Was die App dabei selbst erledigt: Abo nach der Zahlung freischalten, Gratis-Phase der Aktion (`trial_period_days`),
Kündigung zum Ende des bezahlten Zeitraums, Beenden aller Abos bei einer Preisänderung, doppelte Abos sofort
beenden und veraltete Webhook-Ereignisse ignorieren (der aktuelle Stand wird bei Stripe nachgefragt).
Nicht eingebaut: Mehrwertsteuer-Berechnung und Rückerstattungen (beides im Stripe-Dashboard).

## Deployment auf Render (kostenlos)

Der Webdienst läuft auf Renders Gratis-Plan, die Daten (Konten & Chats) liegen in einer kostenlosen Postgres-Datenbank bei Neon, weil der Gratis-Plan von Render keinen dauerhaften Speicher hat.

1. Auf neon.tech ein kostenloses Konto und ein Projekt anlegen, die **Connection string** (`postgresql://…`) kopieren.
2. Auf Render: **New → Blueprint**, dieses Repo (und den passenden Branch) wählen.
3. `GEMINI_API_KEY` (kostenlos auf aistudio.google.com → Get API key) und `DATABASE_URL` eintragen. Für den Pro-Plan zusätzlich `ANTHROPIC_API_KEY`., **Apply** klicken.

Hinweise: Der Gratis-Dienst schläft nach ca. 15 Minuten ohne Besucher ein; die erste Anfrage danach dauert ca. 30–60 Sekunden. Ohne `DATABASE_URL` nutzt die App lokal eine SQLite-Datei (`LUMORA_DB`).
