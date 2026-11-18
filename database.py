"""Datenbank-Schicht: SQLite-Datei (lokal) oder Postgres (wenn DATABASE_URL gesetzt ist)."""
import os
import sqlite3

DB_PATH = os.getenv("LUMORA_DB", "lumora.db")
DATABASE_URL = os.getenv("DATABASE_URL")
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
    # --- Abos, Limits, Aktionen ---
    "CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER NOT NULL PRIMARY KEY, "
    "is_admin INTEGER NOT NULL DEFAULT 0, pw_default INTEGER NOT NULL DEFAULT 0, "
    "pro_until REAL NOT NULL DEFAULT 0, auto_renew INTEGER NOT NULL DEFAULT 0, "
    "price_cents INTEGER NOT NULL DEFAULT 0, ended_reason TEXT NOT NULL DEFAULT '', "
    "trial_used INTEGER NOT NULL DEFAULT 0, stripe_customer TEXT NOT NULL DEFAULT '', "
    "stripe_sub TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS settings (name TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS promos (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
    "free_days INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
    "ends_at REAL NOT NULL DEFAULT 0, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS token_usage (user_id INTEGER NOT NULL, day TEXT NOT NULL, "
    "tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, day))",
    "CREATE TABLE IF NOT EXISTS login_fails (username TEXT NOT NULL PRIMARY KEY, "
    "fails INTEGER NOT NULL, locked_until REAL NOT NULL)",
    # --- Support (Nachrichten zwischen Nutzer und Admin); seen = vom Empfänger gelesen
    "CREATE TABLE IF NOT EXISTS support_messages (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
    "sender TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL, seen INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS idx_support_user ON support_messages(user_id, id)",
]


def init_db():
    if not PG:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db() as con:
        for stmt in SCHEMA:
            if PG:
                stmt = (
                    stmt.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY")
                    .replace("BLOB", "BYTEA")
                    .replace("REAL", "DOUBLE PRECISION")
                )
            con.execute(stmt)
