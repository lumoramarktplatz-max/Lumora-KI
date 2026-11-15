"""Lumora-KI: ein allgemeiner KI-Assistent mit Streaming-Chat."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

MODEL = os.getenv("LUMORA_MODEL", "claude-sonnet-5-5")
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


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, "
            "username TEXT UNIQUE NOT NULL COLLATE NOCASE, salt BLOB NOT NULL, "
            "pw_hash BLOB NOT NULL, created REAL NOT NULL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, "
            "user_id INTEGER NOT NULL, expires REAL NOT NULL)"
        )


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


init_db()
client = AsyncAnthropic()  # liest ANTHROPIC_API_KEY aus der Umgebung
app = FastAPI(title="Lumora-KI")


class Message(BaseModel):
    role: str  # "user" oder "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]


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
            cur = con.execute(
                "INSERT INTO users (username, salt, pw_hash, created) VALUES (?, ?, ?, ?)",
                (creds.username, salt, hash_pw(creds.password, salt), time.time()),
            )
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Dieser Benutzername ist schon vergeben")
    start_session(response, user_id)
    return {"username": creds.username}


@app.post("/api/login")
async def login(creds: Credentials, response: Response):
    with db() as con:
        row = con.execute(
            "SELECT id, username, salt, pw_hash FROM users WHERE username = ?",
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


@app.post("/api/chat")
async def chat(req: ChatRequest, user=Depends(current_user)):
    async def stream():
        try:
            async with client.messages.stream(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[m.model_dump() for m in req.messages],
            ) as s:
                async for text in s.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as e:  # Fehler an die UI weiterreichen
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
