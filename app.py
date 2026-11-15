"""Lumora-KI: ein allgemeiner KI-Assistent mit Streaming-Chat."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time

import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

# Anbieter: "gemini" (hat eine kostenlose Stufe) oder "claude".
# Ohne LUMORA_PROVIDER wird Gemini genommen, falls GEMINI_API_KEY gesetzt ist, sonst Claude.
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
PROVIDER = os.getenv("LUMORA_PROVIDER") or ("gemini" if GEMINI_KEY else "claude")
MODEL = os.getenv("LUMORA_MODEL") or (
    "gemini-2.5-flash" if PROVIDER == "gemini" else "claude-sonnet-5-5"
)
SYSTEM_PROMPT = (
    "Du bist Lumora, eine freundliche und hilfreiche KI. "
    "Du beantwortest Fragen zu allen Themen klar, ehrlich und verständlich. "
    "Wichtig: Antworte immer in der Sprache, in der der Nutzer seine letzte Nachricht "
    "geschrieben hat (z. B. Englisch, Spanisch, Türkisch, Slowenisch …), und bleibe in "
    "dieser Sprache, bis der Nutzer die Sprache wechselt. Bei unklarer Sprache nimm Deutsch. "
    "Wenn du etwas nicht weißt, sag es offen."
)

DB_PATH = os.getenv("LUMORA_DB", "lumora.db")
SESSION_SECONDS = 30 * 24 * 3600
COOKIE = "lumora_session"


DATABASE_URL = os.getenv("DATABASE_URL")  # gesetzt -> Postgres, sonst SQLite-Datei
PG = bool(DATABASE_URL)
if PG:
    import psycopg
    from psycopg.rows import dict_row

    UNIQUE_ERRORS = (psycopg.errors.UniqueViolation,)
else:
    UNIQUE_ERRORS = (sqlite3.IntegrityError,)


class Conn:
    """Kleine Hülle, damit derselbe Code mit SQLite und Postgres läuft."""

    def __init__(self):
        if PG:
            self.c = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.c = sqlite3.connect(DB_PATH)
            self.c.row_factory = sqlite3.Row
            self.c.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, params=()):
        return self.c.execute(sql.replace("?", "%s") if PG else sql, params)

    def insert(self, sql, params):
        """INSERT ausführen und die neue id zurückgeben."""
        if PG:
            return self.c.execute(sql.replace("?", "%s") + " RETURNING id", params).fetchone()["id"]
        return self.c.execute(sql, params).lastrowid

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        try:
            self.c.rollback() if et else self.c.commit()
        finally:
            self.c.close()


def db():
    return Conn()


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT NOT NULL, "
    "salt BLOB NOT NULL, pw_hash BLOB NOT NULL, created REAL NOT NULL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_name ON users(lower(username))",
    "CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
    "title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL "
    "REFERENCES chats(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL, "
    "created REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_id, updated)",
    "CREATE INDEX IF NOT EXISTS idx_msgs_chat ON messages(chat_id, id)",
    "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, "
    "user_id INTEGER NOT NULL, expires REAL NOT NULL)",
]


def init_db():
    with db() as con:
        for stmt in SCHEMA:
            if PG:
                stmt = (
                    stmt.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY")
                    .replace("BLOB", "BYTEA")
                    .replace("REAL", "DOUBLE PRECISION")
                )
            con.execute(stmt)


def hash_pw(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Nicht angemeldet")
    with db() as con:
        row = con.execute(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires > ?",
            (token_hash(token), time.time()),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Nicht angemeldet")
    return dict(row)


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


if not PG:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
init_db()
client = None  # Claude-Client, wird erst bei Bedarf erstellt (liest ANTHROPIC_API_KEY)


async def generate_claude(history):
    global client
    if client is None:
        client = AsyncAnthropic()
    async with client.messages.stream(
        model=MODEL, max_tokens=4096, system=SYSTEM_PROMPT, messages=history
    ) as s:
        async for chunk in s.text_stream:
            yield chunk


async def generate_gemini(history):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY ist nicht gesetzt")
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in history
        ],
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
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
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if part.get("text"):
                            yield part["text"]


GEMINI_TRANSPORT = None  # nur für Tests austauschbar
generate = generate_gemini if PROVIDER == "gemini" else generate_claude
app = FastAPI(title="Lumora-KI")


class SendRequest(BaseModel):
    content: str


class Credentials(BaseModel):
    username: str
    password: str


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
DUMMY_SALT = b"\0" * 16


@app.post("/api/register")
async def register(creds: Credentials, response: Response):
    if not USERNAME_RE.match(creds.username):
        raise HTTPException(400, "Benutzername: 3–32 Zeichen (Buchstaben, Zahlen, _ . -)")
    if not 8 <= len(creds.password) <= 200:
        raise HTTPException(400, "Das Passwort muss mindestens 8 Zeichen lang sein")
    salt = secrets.token_bytes(16)
    try:
        with db() as con:
            user_id = con.insert(
                "INSERT INTO users (username, salt, pw_hash, created) VALUES (?, ?, ?, ?)",
                (creds.username, salt, hash_pw(creds.password, salt), time.time()),
            )
    except UNIQUE_ERRORS:
        raise HTTPException(409, "Dieser Benutzername ist schon vergeben")
    start_session(response, user_id)
    return {"username": creds.username}


@app.post("/api/login")
async def login(creds: Credentials, response: Response):
    with db() as con:
        row = con.execute(
            "SELECT id, username, salt, pw_hash FROM users WHERE lower(username) = lower(?)",
            (creds.username,),
        ).fetchone()
    salt = row["salt"] if row else DUMMY_SALT
    ok = hmac.compare_digest(hash_pw(creds.password[:200], salt), row["pw_hash"] if row else b"x")
    if not row or not ok:
        raise HTTPException(401, "Benutzername oder Passwort falsch")
    start_session(response, row["id"])
    return {"username": row["username"]}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(user=Depends(current_user)):
    return {"username": user["username"]}


def owned_chat(con, chat_id: int, user_id: int):
    row = con.execute(
        "SELECT id, title FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Chat nicht gefunden")
    return row


@app.get("/api/chats")
async def list_chats(user=Depends(current_user)):
    with db() as con:
        rows = con.execute(
            "SELECT id, title FROM chats WHERE user_id = ? ORDER BY updated DESC, id DESC",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/chats")
async def create_chat(user=Depends(current_user)):
    now = time.time()
    with db() as con:
        new_id = con.insert(
            "INSERT INTO chats (user_id, title, created, updated) VALUES (?, ?, ?, ?)",
            (user["id"], "Neuer Chat", now, now),
        )
    return {"id": new_id, "title": "Neuer Chat"}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: int, user=Depends(current_user)):
    with db() as con:
        chat = owned_chat(con, chat_id, user["id"])
        msgs = con.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
    return {"id": chat["id"], "title": chat["title"], "messages": [dict(m) for m in msgs]}


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: int, user=Depends(current_user)):
    with db() as con:
        owned_chat(con, chat_id, user["id"])
        con.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return {"ok": True}


@app.post("/api/chats/{chat_id}/messages")
async def send_message(chat_id: int, req: SendRequest, user=Depends(current_user)):
    text = req.content.strip()
    if not text:
        raise HTTPException(400, "Leere Nachricht")
    if len(text) > 20000:
        raise HTTPException(400, "Nachricht ist zu lang")
    with db() as con:
        chat = owned_chat(con, chat_id, user["id"])
        now = time.time()
        con.execute(
            "INSERT INTO messages (chat_id, role, content, created) VALUES (?, 'user', ?, ?)",
            (chat_id, text, now),
        )
        if chat["title"] == "Neuer Chat":
            title = " ".join(text.split())[:40] or "Neuer Chat"
            con.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))
        con.execute("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))
        rows = con.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 60",
            (chat_id,),
        ).fetchall()
    history = [dict(r) for r in reversed(rows)]
    while history and history[0]["role"] != "user":
        history.pop(0)

    async def stream():
        answer = ""
        try:
            async for chunk in generate(history):
                answer += chunk
                yield f"data: {json.dumps({'text': chunk})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if answer:
                with db() as con:
                    con.execute(
                        "INSERT INTO messages (chat_id, role, content, created) "
                        "VALUES (?, 'assistant', ?, ?)",
                        (chat_id, answer, time.time()),
                    )
                    con.execute("UPDATE chats SET updated = ? WHERE id = ?", (time.time(), chat_id))
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
