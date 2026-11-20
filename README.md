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

- In der Oberfläche heißen die Pläne nur **Gratis** und **Pro**, die Anbieter (Gemini/Claude) werden nicht genannt.
- Pro-Nutzer können oben im Chat zwischen dem **Pro-Modell** und dem **normalen (Gratis-)Modell** umschalten; beides ohne Limit.
- Chats gehören dem Nutzer, nicht dem Plan: Man kann jeden Chat mit beiden Modellen weiterführen.
- **Verkauf sperren:** Im Admin-Bereich kann man neue Pro-Käufe sperren und wieder öffnen. Laufende Abos bleiben davon unberührt.
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

## Rangliste und Belohnung

- **Rangliste (🏆 oben):** Alle angemeldeten Nutzer sehen, wer am längsten Pro hatte (Summe aller Pro-Zeiten, Pausen zählen
  nicht), längste Dauer zuerst, mit Medaillen für Platz 1–3. Admins stehen nicht darin. Jeder Nutzer kann sich im Konto selbst
  ausblenden. Der Admin schaltet die Rangliste im Admin-Bereich ein/aus und versteckt einzelne Nutzer mit 🏆.
- **Belohnung:** Kauft jemand Pro, fallen Emojis, Buchstaben und Wörter über den Bildschirm. Im Admin-Bereich stellst du ein,
  was fällt (mit Komma trennen, z. B. `👍, ❤️, Danke, A, B`, mehrere Dinge gleichzeitig), wie viele, wie lange und welche
  Nachricht oben steht. **Vorschau** zeigt es sofort. Mit 🎁 in der Nutzerliste löst du sie für einen einzelnen Nutzer aus
  (er sieht sie beim nächsten Öffnen von Lumora). Das Formular dahinter lässt dich für genau dieses Konto frei wählen, was fällt, wie viele,
  wie lange und welche Nachricht, auch bei Gratis-Konten ohne Pro; alle anderen sehen nichts. Bei Nutzern mit „Bewegung reduzieren“ erscheint nur die Nachricht.

## Support (ein echter Mensch antwortet)

Nutzer öffnen im Konto **Support kontaktieren** und schreiben dem Admin. Der Admin sieht neue Nachrichten im Admin-Bereich
unter **Support-Postfach** (roter Punkt am Plan-Symbol) und antwortet dort. Die Antwort erscheint beim Nutzer im Support-Fenster.
Pro Nutzer sind höchstens 20 Nachrichten pro Stunde erlaubt, jede höchstens 2000 Zeichen.

## Schnell und stabil

Alle Datenbank-Zugriffe laufen in Threads, damit eine langsame Datenbank (z. B. Neon wacht gerade auf) nicht den ganzen
Server blockiert. Der Health-Check von Render antwortet deshalb immer sofort. Bei Postgres werden Verbindungen in einem
Pool wiederverwendet (`DB_POOL_MAX`, Standard 10). Tote Verbindungen (Neon schließt ungenutzte) werden automatisch ersetzt.
Tipp: Render und Neon sollten in derselben Region stehen (hier Neon: Frankfurt), das spart bei jeder Anfrage Zeit.

## Daten bleiben bei Updates erhalten

Konten, Abos, Chats, Einstellungen und Support-Nachrichten liegen in der Datenbank (`DATABASE_URL`, Neon). Ein Update
ersetzt nur den Code. Beim Start werden Tabellen nur angelegt, wenn sie fehlen; nichts wird gelöscht oder überschrieben.
Ohne `DATABASE_URL` läge alles in einer Datei, die Render bei jedem Deploy löscht. Darum warnt die App dann im Log und
im Admin-Bereich. Im Admin-Bereich gibt es außerdem **Abo-Liste herunterladen** (ohne Passwörter) als Notfall-Sicherung.
Neon bietet zusätzlich eine Wiederherstellung auf einen früheren Zeitpunkt (Neon → Branches → Restore).

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

**Schon ein Stripe-Konto für andere Websites?** Das geht, aber beachte:
- Ein Webhook bekommt die Ereignisse des **ganzen Kontos**. Lumora markiert deshalb ihre eigenen Abos (`metadata.app=lumora`)
  und ignoriert alles andere, auch Abos anderer Websites.
- Das **Kundenportal** und die E-Mail-Einstellungen gelten für das ganze Konto. Wenn andere Websites sie nutzen, ändern
  deine Einstellungen dort etwas mit. Sauberer ist ein eigenes Stripe-Konto für Lumora (Dashboard → Kontoname oben links →
  *Neues Konto*, geht mit demselben Login). Dann sind Kunden, Auszahlungen und Rechnungen getrennt.
- Für `STRIPE_SECRET_KEY` empfiehlt sich ein **eingeschränkter Schlüssel** (`rk_…`) mit Schreibrechten für *Checkout Sessions*,
  *Subscriptions* und *Customer portal sessions* und Leserechten für *Customers*.

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
