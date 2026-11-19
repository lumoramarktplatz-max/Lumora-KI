"""Lumora-KI: KI-Assistent mit Streaming-Chat, Konten, Gratis-/Pro-Plan und Admin-Bereich."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from anthropic import AnthropicError, AsyncAnthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

import billing  # noqa: E402  (nach load_dotenv, weil dort Umgebungsvariablen gelesen werden)
from database import PG, UNIQUE_ERRORS, close_pool, db, init_db  # noqa: E402

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
# Gratis-Plan -> Gemini, Pro-Plan -> Claude
FREE_MODEL = os.getenv("LUMORA_FREE_MODEL") or os.getenv("LUMORA_MODEL") or "gemini-4-hydrogen"
PRO_MODEL = os.getenv("LUMORA_PRO_MODEL") or "claude-sonnet-5-5"
FREE_HISTORY, PRO_HISTORY = 16, 60  # wie viele frühere Nachrichten mitgeschickt werden
SYSTEM_PROMPT = (
    "Du bist Lumora, eine freundliche und hilfreiche KI. "
    "Du beantwortest Fragen zu allen Themen klar, ehrlich und verständlich. "
    "Wichtig: Antworte immer in der Sprache, in der der Nutzer seine letzte Nachricht "
    "geschrieben hat (z. B. Englisch, Spanisch, Türkisch, Slowenisch …), und bleibe in "
    "dieser Sprache, bis der Nutzer die Sprache wechselt. Bei unklarer Sprache nimm Deutsch. "
    "Wenn du etwas nicht weißt, sag es offen. "
    "Du bist Lumora. Welches Modell oder welcher Anbieter hinter dir steckt, nennst du nicht; "
    "wenn jemand danach fragt, sagst du freundlich, dass du Lumora bist, eine KI."
)

SESSION_SECONDS = 30 * 24 * 3600
COOKIE = "lumora_session"
ADMIN_NAME = os.getenv("ADMIN_USERNAME", "Matic")
DEFAULT_ADMIN_PW = "VIP"
LOCK_AFTER, LOCK_SECONDS = 5, 300


# ============================================================ Passwörter & Sitzungen
def hash_pw(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "not_logged_in")
    with db() as con:
        row = con.execute(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires > ?",
            (token_hash(token), time.time()),
        ).fetchone()
    if not row:
        raise HTTPException(401, "not_logged_in")
    return dict(row)


def require_admin(user=Depends(current_user)):
    with db() as con:
        p = billing.get_profile(con, user["id"])
    if not p["is_admin"]:
        raise HTTPException(403, "Nur für Admins")
    if p["pw_default"]:
        raise HTTPException(403, "Bitte ändere zuerst dein Start-Passwort (Konto → Passwort ändern).")
    return user


def start_session(response: Response, user_id: int):
    token = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            (token_hash(token), user_id, time.time() + SESSION_SECONDS),
        )
    response.set_cookie(
        COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax",
        secure=os.getenv("LUMORA_SECURE_COOKIE") == "1",
    )


def ensure_admin():
    """Legt beim Start den Admin-Account an (Name Matic, Start-Passwort VIP), falls es noch keinen gibt.

    Das Start-Passwort kann per ADMIN_PASSWORD gesetzt werden. Solange es "VIP" (oder kürzer als 8
    Zeichen) ist, hat der Account noch keine Admin-Rechte, bis das Passwort geändert wurde."""
    with db() as con:
        if con.execute("SELECT 1 FROM profiles WHERE is_admin = 1").fetchone():
            return
        pw = os.getenv("ADMIN_PASSWORD") or DEFAULT_ADMIN_PW
        weak = 1 if (pw == DEFAULT_ADMIN_PW or len(pw) < 8) else 0
        salt = secrets.token_bytes(16)
        row = con.execute(
            "SELECT id FROM users WHERE lower(username) = lower(?)", (ADMIN_NAME,)
        ).fetchone()
        if row:  # Name schon vergeben: Account übernehmen, Passwort zurücksetzen, abmelden
            uid = row["id"]
            con.execute("UPDATE users SET salt = ?, pw_hash = ? WHERE id = ?", (salt, hash_pw(pw, salt), uid))
            con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        else:
            uid = con.insert(
                "INSERT INTO users (username, salt, pw_hash, created) VALUES (?, ?, ?, ?)",
                (ADMIN_NAME, salt, hash_pw(pw, salt), time.time()),
            )
        con.execute(
            "INSERT INTO profiles (user_id, is_admin, pw_default) VALUES (?, 1, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET is_admin = 1, pw_default = excluded.pw_default",
            (uid, weak),
        )


ON_RENDER = bool(os.getenv("RENDER"))
if ON_RENDER and not PG:
    print("!!! WARNUNG: DATABASE_URL fehlt. Konten, Chats und Abos liegen in einer Datei, die bei jedem "
          "Deploy gelöscht wird. Bitte DATABASE_URL (Neon) auf Render eintragen.", file=sys.stderr, flush=True)
init_db()
ensure_admin()


# ============================================================ KI-Anbieter
class UserError(Exception):
    """Fehler mit Code, den die Oberfläche in der Sprache des Nutzers anzeigt."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


client = None  # Claude-Client, wird erst bei Bedarf erstellt
client_key = None
GEMINI_TRANSPORT = None  # nur für Tests austauschbar


def anthropic_key() -> str:
    """Claude-Schlüssel aus der Umgebung, ohne unsichtbare Leerzeichen/Zeilenumbrüche (passiert beim Einfügen leicht)."""
    return "".join((os.getenv("ANTHROPIC_API_KEY") or "").split())


def claude_client():
    global client, client_key
    key = anthropic_key()
    if not key:
        raise UserError("pro_not_configured")
    if client is None or client_key != key:
        client, client_key = AsyncAnthropic(api_key=key), key
    return client


def log_claude_error(e: Exception):
    print(f"Claude-Fehler: {type(e).__name__}: {str(e)[:300]} | Ursache: {e.__cause__!r}"[:600], file=sys.stderr, flush=True)


async def generate_claude(history, usage):
    c = claude_client()
    try:
        async with c.messages.stream(
            model=PRO_MODEL, max_tokens=4096, system=SYSTEM_PROMPT, messages=history
        ) as s:
            async for chunk in s.text_stream:
                yield chunk
            try:
                final = await s.get_final_message()
                usage["tokens"] = final.usage.input_tokens + final.usage.output_tokens
            except Exception:
                pass
    except AnthropicError as e:  # Verbindung, Schlüssel, Guthaben, Überlastung …: Nutzer bekommt eine verständliche Meldung
        log_claude_error(e)
        raise UserError("ai_unavailable")


async def generate_gemini(history, usage):
    if not GEMINI_KEY:
        raise UserError("gemini_not_configured")
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in history
        ],
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{FREE_MODEL}:streamGenerateContent?alt=sse"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), transport=GEMINI_TRANSPORT) as http:
        async with http.stream("POST", url, json=body, headers={"x-goog-api-key": GEMINI_KEY}) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode(errors="replace")
                try:
                    detail = json.loads(detail)["error"]["message"]
                except Exception:
                    pass
                raise RuntimeError(f"Gemini-Fehler {r.status_code}: {detail[:300]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip() or "{}")
                total = (data.get("usageMetadata") or {}).get("totalTokenCount")
                if total:
                    usage["tokens"] = int(total)
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if part.get("text"):
                            yield part["text"]


@asynccontextmanager
async def lifespan(_app):
    yield
    close_pool()


app = FastAPI(title="Lumora-KI", lifespan=lifespan)


# ============================================================ Konto
class SendRequest(BaseModel):
    content: str
    model: str | None = None  # "pro" oder "free"; Pro-Nutzer dürfen wählen, alle anderen bekommen "free"


class Credentials(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    old: str
    new: str


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
DUMMY_SALT = b"\0" * 16


@app.post("/api/register")
def register(creds: Credentials, response: Response):
    if not USERNAME_RE.match(creds.username):
        raise HTTPException(400, "bad_username")
    if not 8 <= len(creds.password) <= 200:
        raise HTTPException(400, "pw_short")
    salt = secrets.token_bytes(16)
    try:
        with db() as con:
            user_id = con.insert(
                "INSERT INTO users (username, salt, pw_hash, created) VALUES (?, ?, ?, ?)",
                (creds.username, salt, hash_pw(creds.password, salt), time.time()),
            )
    except UNIQUE_ERRORS:
        raise HTTPException(409, "name_taken")
    start_session(response, user_id)
    return {"username": creds.username}


@app.post("/api/login")
def login(creds: Credentials, response: Response):
    name = creds.username.strip().lower()[:64]
    now = time.time()
    with db() as con:
        fail = con.execute("SELECT fails, locked_until FROM login_fails WHERE username = ?", (name,)).fetchone()
        if fail and fail["locked_until"] > now:
            wait = int((fail["locked_until"] - now) // 60) + 1
            raise HTTPException(429, f"locked:{wait}")
        row = con.execute(
            "SELECT id, username, salt, pw_hash FROM users WHERE lower(username) = lower(?)",
            (creds.username,),
        ).fetchone()
        salt = row["salt"] if row else DUMMY_SALT
        ok = hmac.compare_digest(hash_pw(creds.password[:200], salt), row["pw_hash"] if row else b"x")
        if not row or not ok:
            fails = (fail["fails"] if fail else 0) + 1
            locked = now + LOCK_SECONDS if fails >= LOCK_AFTER else 0
            con.execute(
                "INSERT INTO login_fails (username, fails, locked_until) VALUES (?, ?, ?) "
                "ON CONFLICT (username) DO UPDATE SET fails = excluded.fails, locked_until = excluded.locked_until",
                (name, 0 if locked else fails, locked),
            )
            # Fehler committen, bevor wir die Ausnahme werfen
            con.c.commit()
            raise HTTPException(401, "login_failed")
        con.execute("DELETE FROM login_fails WHERE username = ?", (name,))
    start_session(response, row["id"])
    return {"username": row["username"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    with db() as con:
        return billing.account_info(con, user)


@app.post("/api/password")
def change_password(req: PasswordChange, request: Request, user=Depends(current_user)):
    if not 8 <= len(req.new) <= 200:
        raise HTTPException(400, "pw_short")
    if req.new == req.old or req.new == DEFAULT_ADMIN_PW:
        raise HTTPException(400, "pw_same")
    with db() as con:
        row = con.execute("SELECT salt, pw_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not hmac.compare_digest(hash_pw(req.old[:200], row["salt"]), row["pw_hash"]):
            raise HTTPException(403, "old_wrong")
        salt = secrets.token_bytes(16)
        con.execute(
            "UPDATE users SET salt = ?, pw_hash = ? WHERE id = ?", (salt, hash_pw(req.new, salt), user["id"])
        )
        billing.get_profile(con, user["id"])
        con.execute("UPDATE profiles SET pw_default = 0 WHERE user_id = ?", (user["id"],))
        # alle anderen Geräte abmelden
        con.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user["id"], token_hash(request.cookies.get(COOKIE, ""))),
        )
    return {"ok": True}


# ============================================================ Chats
def owned_chat(con, chat_id: int, user_id: int):
    row = con.execute(
        "SELECT id, title FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "chat_not_found")
    return row


@app.get("/api/chats")
def list_chats(user=Depends(current_user)):
    with db() as con:
        rows = con.execute(
            "SELECT id, title FROM chats WHERE user_id = ? ORDER BY updated DESC, id DESC",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/chats")
def create_chat(user=Depends(current_user)):
    now = time.time()
    with db() as con:
        new_id = con.insert(
            "INSERT INTO chats (user_id, title, created, updated) VALUES (?, ?, ?, ?)",
            (user["id"], "Neuer Chat", now, now),
        )
    return {"id": new_id, "title": "Neuer Chat"}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: int, user=Depends(current_user)):
    with db() as con:
        chat = owned_chat(con, chat_id, user["id"])
        msgs = con.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
    return {"id": chat["id"], "title": chat["title"], "messages": [dict(m) for m in msgs]}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: int, user=Depends(current_user)):
    with db() as con:
        owned_chat(con, chat_id, user["id"])
        con.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return {"ok": True}


def save_answer(chat_id: int, uid: int, answer: str, used: int, pro: bool):
    with db() as con:
        con.execute(
            "INSERT INTO messages (chat_id, role, content, created) VALUES (?, 'assistant', ?, ?)",
            (chat_id, answer, time.time()),
        )
        con.execute("UPDATE chats SET updated = ? WHERE id = ?", (time.time(), chat_id))
        if not pro:  # Limit gilt nur im Gratis-Plan
            billing.add_tokens(con, uid, used)


@app.post("/api/chats/{chat_id}/messages")
def send_message(chat_id: int, req: SendRequest, user=Depends(current_user)):
    text = req.content.strip()
    if not text:
        raise HTTPException(400, "msg_empty")
    if len(text) > 20000:
        raise HTTPException(400, "msg_long")
    uid = user["id"]
    with db() as con:
        chat = owned_chat(con, chat_id, uid)
        now = time.time()
        profile = billing.get_profile(con, uid, now)
        pro = billing.is_pro(profile, now)
        use_pro = pro and req.model != "free"  # Pro-Nutzer können auch das normale Modell nutzen, ohne Limit
        if not pro:
            limit = billing.get_settings(con)["free_daily_tokens"]
            if billing.tokens_used(con, uid) >= limit:
                raise HTTPException(429, "limit_reached")
        con.execute(
            "INSERT INTO messages (chat_id, role, content, created) VALUES (?, 'user', ?, ?)",
            (chat_id, text, now),
        )
        if chat["title"] == "Neuer Chat":
            title = " ".join(text.split())[:40] or "Neuer Chat"
            con.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))
        con.execute("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))
        rows = con.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, PRO_HISTORY if pro else FREE_HISTORY),
        ).fetchall()
    history = [dict(r) for r in reversed(rows)]
    while history and history[0]["role"] != "user":
        history.pop(0)
    generate = generate_claude if use_pro else generate_gemini

    async def stream():
        answer, usage = "", {}
        try:
            async for chunk in generate(history, usage):
                answer += chunk
                yield f"data: {json.dumps({'text': chunk})}\n\n"
        except UserError as e:
            yield f"data: {json.dumps({'error_code': e.code})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if answer:
                used = usage.get("tokens") or (sum(len(m["content"]) for m in history) + len(answer)) // 3
                await run_in_threadpool(save_answer, chat_id, uid, answer, used, pro)
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ============================================================ Abo
async def in_db(fn, *args):
    """Führt fn(con, *args) mit einer Datenbankverbindung in einem Thread aus."""
    def run():
        with db() as con:
            return fn(con, *args)
    return await run_in_threadpool(run)


def public_base(request: Request) -> str:
    return (os.getenv("PUBLIC_URL") or str(request.base_url)).rstrip("/")


@app.post("/api/subscription/checkout")
async def subscribe(request: Request, user=Depends(current_user)):
    try:
        lang = str((await request.json()).get("lang", ""))[:5]
    except Exception:
        lang = ""
    mode = billing.payments_mode()
    if mode == "off":
        raise HTTPException(503, "pay_off")
    def prepare(con):
        p = billing.get_profile(con, user["id"])
        if not billing.get_settings(con)["sales_open"]:
            raise HTTPException(403, "sales_closed")
        reason = billing.subscribe_block_reason(p)
        if reason:
            raise HTTPException(400, reason)
        s = billing.get_settings(con)
        promo = billing.eligible_promo(con, p)
        if mode == "test":
            billing.activate_test(con, user["id"])
            return None
        return s, promo

    prep = await in_db(prepare)
    if prep is None:
        return {"ok": True}
    s, promo = prep
    try:
        url = await billing.stripe_checkout(
            user, s["price_cents"], s["currency"], promo["free_days"] if promo else 0, public_base(request), lang
        )
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"url": url}


@app.post("/api/subscription/cancel")
async def cancel_subscription(user=Depends(current_user)):
    def check(con):
        p = billing.get_profile(con, user["id"])
        if not (p["auto_renew"] and p["pro_until"] > time.time()):
            raise HTTPException(400, "no_sub")
        return p["stripe_sub"]

    sub = await in_db(check)
    if sub and billing.payments_mode() == "stripe":
        try:
            await billing.stripe_cancel_at_period_end(sub)
        except Exception as e:
            raise HTTPException(502, str(e))
    await in_db(billing.cancel_local, user["id"])
    return {"ok": True}


@app.post("/api/subscription/portal")
async def subscription_portal(request: Request, user=Depends(current_user)):
    if billing.payments_mode() != "stripe":
        raise HTTPException(503, "pay_off")
    p = await in_db(billing.get_profile, user["id"])
    if not p["stripe_customer"]:
        raise HTTPException(400, "no_sub")
    try:
        return {"url": await billing.stripe_portal(p["stripe_customer"], public_base(request) + "/")}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    payload = await request.body()
    if not secret or not billing.verify_webhook(payload, request.headers.get("stripe-signature", ""), secret):
        raise HTTPException(400, "Ungültige Signatur")
    event = json.loads(payload)
    typ, obj = event.get("type", ""), (event.get("data") or {}).get("object") or {}
    if typ.startswith("customer.subscription.") and obj.get("id"):
        ours = await in_db(billing.is_ours, obj)
        if not ours:  # Abo einer anderen Website im selben Stripe-Konto
            return {"received": True, "ignored": True}
        # Stripe liefert Ereignisse nicht immer in der richtigen Reihenfolge: lieber den aktuellen Stand holen.
        try:
            fresh = await billing.stripe_call("GET", f"/subscriptions/{obj['id']}")
            if fresh.get("id") == obj["id"] and fresh.get("status"):
                obj = fresh
                event = {"type": typ, "data": {"object": fresh}}
        except Exception:
            pass  # Stand aus dem Ereignis verwenden
        if typ == "customer.subscription.created":
            meta = obj.get("metadata") or {}
            uid = int(meta["user_id"]) if meta.get("app") == billing.APP_TAG and str(meta.get("user_id", "")).isdigit() else 0
            p = await in_db(billing.get_profile, uid) if uid else None
            if p and p["stripe_sub"] and p["stripe_sub"] != obj["id"] and p["auto_renew"] and p["pro_until"] > time.time():
                # Doppeltes Abo (z. B. zweimal bezahlt): das neue sofort beenden, damit nicht doppelt abgebucht wird
                try:
                    await billing.stripe_cancel_now(obj["id"])
                except Exception:
                    pass
                return {"received": True, "duplicate_cancelled": obj["id"]}
    await in_db(billing.handle_event, event)
    return {"received": True}


# ============================================================ Support (Mensch antwortet)
SUPPORT_MAX, SUPPORT_PER_HOUR = 2000, 20


class SupportMsg(BaseModel):
    body: str


@app.get("/api/support")
def support_thread(user=Depends(current_user)):
    with db() as con:
        rows = con.execute(
            "SELECT id, sender, body, created FROM support_messages WHERE user_id = ? ORDER BY id DESC LIMIT 200",
            (user["id"],),
        ).fetchall()
        con.execute(
            "UPDATE support_messages SET seen = 1 WHERE user_id = ? AND sender = 'admin' AND seen = 0", (user["id"],)
        )
    return [dict(r) for r in reversed(rows)]


@app.post("/api/support")
def support_send(req: SupportMsg, user=Depends(current_user)):
    body = req.body.strip()
    if not body:
        raise HTTPException(400, "msg_empty")
    if len(body) > SUPPORT_MAX:
        raise HTTPException(400, "msg_long")
    now = time.time()
    with db() as con:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM support_messages WHERE user_id = ? AND sender = 'user' AND created > ?",
            (user["id"], now - 3600),
        ).fetchone()["n"]
        if n >= SUPPORT_PER_HOUR:
            raise HTTPException(429, "support_rate")
        con.execute(
            "INSERT INTO support_messages (user_id, sender, body, created, seen) VALUES (?, 'user', ?, ?, 0)",
            (user["id"], body, now),
        )
    return {"ok": True}


@app.get("/api/support/unread")
def support_unread(user=Depends(current_user)):
    with db() as con:
        mine = con.execute(
            "SELECT COUNT(*) AS n FROM support_messages WHERE user_id = ? AND sender = 'admin' AND seen = 0",
            (user["id"],),
        ).fetchone()["n"]
        admin_n = 0
        if billing.is_admin(billing.get_profile(con, user["id"])):
            admin_n = con.execute(
                "SELECT COUNT(DISTINCT user_id) AS n FROM support_messages WHERE sender = 'user' AND seen = 0"
            ).fetchone()["n"]
    return {"unread": mine, "admin_unread": admin_n}


@app.get("/api/admin/support")
def admin_support_list(admin=Depends(require_admin)):
    now = time.time()
    with db() as con:
        rows = con.execute(
            "SELECT t.user_id, u.username, m.body AS last_body, m.sender AS last_sender, m.created AS last_ts, "
            "t.unread, COALESCE(p.is_admin, 0) AS is_admin, COALESCE(p.pro_until, 0) AS pro_until "
            "FROM (SELECT user_id, MAX(id) AS last_id, CAST(SUM(CASE WHEN sender = 'user' AND seen = 0 THEN 1 ELSE 0 END) AS INTEGER) AS unread "
            "      FROM support_messages GROUP BY user_id) t "
            "JOIN support_messages m ON m.id = t.last_id JOIN users u ON u.id = t.user_id "
            "LEFT JOIN profiles p ON p.user_id = t.user_id ORDER BY t.last_id DESC LIMIT 200"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pro"] = bool(d["is_admin"]) or d["pro_until"] > now
        out.append(d)
    return out


@app.get("/api/admin/support/{uid}")
def admin_support_thread(uid: int, admin=Depends(require_admin)):
    now = time.time()
    with db() as con:
        u = con.execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "Nutzer nicht gefunden")
        p = billing.get_profile(con, uid, now)
        rows = con.execute(
            "SELECT id, sender, body, created FROM support_messages WHERE user_id = ? ORDER BY id DESC LIMIT 200", (uid,)
        ).fetchall()
        con.execute("UPDATE support_messages SET seen = 1 WHERE user_id = ? AND sender = 'user' AND seen = 0", (uid,))
    return {"username": u["username"], "pro": billing.is_pro(p, now), "pro_until": p["pro_until"],
            "messages": [dict(r) for r in reversed(rows)]}


@app.post("/api/admin/support/{uid}")
def admin_support_reply(uid: int, req: SupportMsg, admin=Depends(require_admin)):
    body = req.body.strip()
    if not body or len(body) > SUPPORT_MAX:
        raise HTTPException(400, "Nachricht leer oder zu lang (max. 2000 Zeichen).")
    with db() as con:
        if not con.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
            raise HTTPException(404, "Nutzer nicht gefunden")
        con.execute(
            "INSERT INTO support_messages (user_id, sender, body, created, seen) VALUES (?, 'admin', ?, ?, 0)",
            (uid, body, time.time()),
        )
    return {"ok": True}


# ============================================================ Admin
class PriceReq(BaseModel):
    price: float


class LimitReq(BaseModel):
    tokens: int


class PromoReq(BaseModel):
    name: str
    free_days: int
    ends: str | None = None  # Datum JJJJ-MM-TT, leer = unbegrenzt


class GrantReq(BaseModel):
    days: int


@app.get("/api/admin/overview")
def admin_overview(admin=Depends(require_admin)):
    now = time.time()
    with db() as con:
        s = billing.get_settings(con)
        promos = [dict(r) for r in con.execute(
            "SELECT id, name, free_days, active, ends_at FROM promos ORDER BY id DESC").fetchall()]
        rows = con.execute(
            "SELECT u.id, u.username, u.created, COALESCE(p.is_admin, 0) AS is_admin, "
            "COALESCE(p.pro_until, 0) AS pro_until, COALESCE(p.auto_renew, 0) AS auto_renew, "
            "COALESCE(p.ended_reason, '') AS ended_reason, COALESCE(p.price_cents, 0) AS price_cents, "
            "COALESCE((SELECT tokens FROM token_usage t WHERE t.user_id = u.id AND t.day = ?), 0) AS tokens_today "
            "FROM users u LEFT JOIN profiles p ON p.user_id = u.id ORDER BY u.id DESC LIMIT 500",
            (billing.today(),),
        ).fetchall()
    users = []
    for r in rows:
        d = dict(r)
        d["pro"] = bool(d["is_admin"]) or d["pro_until"] > now
        users.append(d)
    return {"settings": s, "promos": promos, "users": users, "payments": billing.payments_mode(),
            "db": {"persistent": PG or not ON_RENDER, "postgres": PG}}


@app.get("/api/admin/export")
def admin_export(admin=Depends(require_admin)):
    """Abo-Liste als Datei (ohne Passwörter), als Notfall-Sicherung."""
    with db() as con:
        rows = con.execute(
            "SELECT u.id, u.username, u.created, p.is_admin, p.pro_until, p.auto_renew, p.price_cents, "
            "p.ended_reason, p.trial_used, p.stripe_customer, p.stripe_sub "
            "FROM users u LEFT JOIN profiles p ON p.user_id = u.id ORDER BY u.id"
        ).fetchall()
        settings = billing.get_settings(con)
    data = {"exported": datetime.now(billing.TZ).isoformat(timespec="seconds"), "settings": settings,
            "users": [dict(r) for r in rows]}
    return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="lumora-abos.json"'})


@app.post("/api/admin/price")
async def admin_price(req: PriceReq, admin=Depends(require_admin)):
    cents = round(req.price * 100)
    if not 50 <= cents <= 100000:
        raise HTTPException(400, "Der Preis muss zwischen 0.50 und 1000 liegen.")
    subs = await in_db(billing.change_price, cents)
    failed = 0
    if subs and billing.payments_mode() == "stripe":
        results = await asyncio.gather(
            *(billing.stripe_cancel_at_period_end(sid) for sid in subs), return_exceptions=True
        )
        failed = sum(1 for r in results if isinstance(r, Exception))
    return {"ok": True, "price_cents": cents, "ended_subscriptions": len(subs), "stripe_failed": failed}


class SalesReq(BaseModel):
    open: bool


@app.post("/api/admin/test-pro")
async def admin_test_pro(admin=Depends(require_admin)):
    """Prüft den Claude-Schlüssel und die Verbindung und sagt, was nicht stimmt (ohne den Schlüssel zu zeigen)."""
    raw = os.getenv("ANTHROPIC_API_KEY") or ""
    key = anthropic_key()
    info = {
        "model": PRO_MODEL, "key_set": bool(key), "key_length": len(key),
        "key_prefix_ok": key.startswith("sk-ant-"), "had_whitespace": raw != key,
        "key_ascii": key.isascii(), "ok": False, "error": "", "reply": "",
    }
    if not key:
        info["error"] = "Kein Schlüssel gesetzt (ANTHROPIC_API_KEY fehlt auf Render)."
        return info
    try:
        r = await claude_client().messages.create(
            model=PRO_MODEL, max_tokens=20, messages=[{"role": "user", "content": "Antworte nur mit: ok"}], timeout=30
        )
        info["ok"] = True
        info["reply"] = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")[:60]
    except AnthropicError as e:
        log_claude_error(e)
        status = getattr(e, "status_code", None)
        hint = {
            401: "Der Schlüssel wird abgelehnt (falsch, abgeschnitten oder widerrufen).",
            403: "Der Schlüssel darf dieses Modell nicht benutzen.",
            404: "Das Modell wurde nicht gefunden (LUMORA_PRO_MODEL prüfen).",
            429: "Zu viele Anfragen oder Ausgabenlimit erreicht.",
        }.get(status, "")
        if status == 400 and "credit" in str(e).lower():
            hint = "Das Guthaben ist aufgebraucht oder fehlt (console.anthropic.com → Abrechnung)."
        cause = repr(e.__cause__)[:160] if e.__cause__ else ""
        info["error"] = f"{type(e).__name__}{f' {status}' if status else ''}: {str(e)[:220]}" + (f" | Ursache: {cause}" if cause else "")
        info["hint"] = hint
    return info


@app.post("/api/admin/sales")
def admin_sales(req: SalesReq, admin=Depends(require_admin)):
    """Verkauf sperren/öffnen: neue Pro-Käufe sind gesperrt, bestehende Abos laufen normal weiter."""
    with db() as con:
        billing.set_setting(con, "sales_open", 1 if req.open else 0)
    return {"ok": True, "open": req.open}


@app.post("/api/admin/free-limit")
def admin_free_limit(req: LimitReq, admin=Depends(require_admin)):
    if not 1000 <= req.tokens <= 10_000_000:
        raise HTTPException(400, "Das Limit muss zwischen 1000 und 10 000 000 Tokens liegen.")
    with db() as con:
        billing.set_setting(con, "free_daily_tokens", req.tokens)
    return {"ok": True}


@app.post("/api/admin/promos")
def admin_add_promo(req: PromoReq, admin=Depends(require_admin)):
    name = req.name.strip()[:60]
    if not name or not 1 <= req.free_days <= 365:
        raise HTTPException(400, "Name und Gratis-Tage (1–365) angeben.")
    ends = 0.0
    if req.ends:
        try:
            ends = datetime.strptime(req.ends, "%Y-%m-%d").replace(tzinfo=billing.TZ).timestamp() + 86400
        except ValueError:
            raise HTTPException(400, "Datum bitte als JJJJ-MM-TT angeben.")
    with db() as con:
        pid = con.insert(
            "INSERT INTO promos (name, free_days, active, ends_at, created) VALUES (?, ?, 1, ?, ?)",
            (name, req.free_days, ends, time.time()),
        )
    return {"id": pid}


@app.post("/api/admin/promos/{pid}/toggle")
def admin_toggle_promo(pid: int, admin=Depends(require_admin)):
    with db() as con:
        con.execute("UPDATE promos SET active = 1 - active WHERE id = ?", (pid,))
    return {"ok": True}


@app.delete("/api/admin/promos/{pid}")
def admin_delete_promo(pid: int, admin=Depends(require_admin)):
    with db() as con:
        con.execute("DELETE FROM promos WHERE id = ?", (pid,))
    return {"ok": True}


@app.post("/api/admin/users/{uid}/grant")
def admin_grant(uid: int, req: GrantReq, admin=Depends(require_admin)):
    if not 1 <= req.days <= 3650:
        raise HTTPException(400, "Tage zwischen 1 und 3650.")
    with db() as con:
        if not con.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
            raise HTTPException(404, "Nutzer nicht gefunden")
        billing.grant_days(con, uid, req.days)
    return {"ok": True}


@app.post("/api/admin/users/{uid}/revoke")
async def admin_revoke(uid: int, admin=Depends(require_admin)):
    def revoke(con):
        if not con.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
            raise HTTPException(404, "Nutzer nicht gefunden")
        if uid == admin["id"]:
            raise HTTPException(400, "Du kannst dir selbst nicht Pro entziehen.")
        return billing.revoke_pro(con, uid)

    sub = await in_db(revoke)
    if sub and billing.payments_mode() == "stripe":
        try:
            await billing.stripe_cancel_now(sub)
        except Exception:
            pass
    return {"ok": True}


# ============================================================ Seiten
@app.get("/api/health")
async def health():
    return {"ok": True, "persistent_db": PG or not ON_RENDER}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
