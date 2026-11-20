"""Datenbank-Schicht: SQLite-Datei (lokal) oder Postgres (wenn DATABASE_URL gesetzt ist)."""
import atexit
import os
import sqlite3

DB_PATH = os.getenv("LUMORA_DB", "lumora.db")
DATABASE_URL = os.getenv("DATABASE_URL")
PG = bool(DATABASE_URL)
if PG:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    UNIQUE_ERRORS = (psycopg.errors.UniqueViolation,)
    # Verbindungen wiederverwenden: eine neue Verbindung zu Neon kostet (TLS + Anmeldung, evtl. Aufwachen) oft
    # 1 Sekunde oder mehr. Ob eine ausgeliehene Verbindung noch lebt, prüft Conn selbst (Neon schließt ungenutzte
    # Verbindungen); die eingebaute Prüfung wartet bei vielen toten Verbindungen zu lange.
    # prepare_threshold=None: nötig für den Neon-Pooler (pgbouncer).
    POOL = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=int(os.getenv("DB_POOL_MAX", "10")),
        max_idle=60,
        timeout=20,
        kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 10},
        open=False,
    )
    POOL.open()  # wartet nicht: der Dienst startet sofort, Verbindungen entstehen im Hintergrund
    atexit.register(lambda: POOL.close(timeout=2))  # sauber aufräumen, sonst dauert das Beenden ~15 s
else:
    UNIQUE_ERRORS = (sqlite3.IntegrityError,)


class Conn:
    """Kleine Hülle, damit derselbe Code mit SQLite und Postgres läuft."""

    def __init__(self):
        if PG:
            last = None
            for _ in range(POOL.max_size + 2):
                c = POOL.getconn()
                try:
                    c.execute("SELECT 1")  # lebt die Verbindung noch? (Teil der späteren Transaktion)
                    self.c = c
                    break
                except psycopg.OperationalError as e:
                    last = e
                    POOL.putconn(c)  # kaputt: der Pool verwirft sie und legt Ersatz an
            else:
                raise last
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
            if PG:
                POOL.putconn(self.c)
            else:
                self.c.close()


def db():
    return Conn()


def close_pool():
    """Beim Herunterfahren des Servers (Neustart/Deploy) alle Verbindungen sauber schließen."""
    if PG:
        POOL.close(timeout=2)


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
    # --- Rangliste & Belohnung: pro_first = Beginn des ersten Pro, pro_gap = Zeit ohne Pro dazwischen,
    # hide_board = nicht in der Rangliste zeigen, reward_pending = 1 Kauf-Belohnung / 2 vom Admin ausgelöst
    "CREATE TABLE IF NOT EXISTS pro_stats (user_id INTEGER NOT NULL PRIMARY KEY, "
    "pro_first REAL NOT NULL DEFAULT 0, pro_gap REAL NOT NULL DEFAULT 0, "
    "hide_board INTEGER NOT NULL DEFAULT 0, reward_pending INTEGER NOT NULL DEFAULT 0)",
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
