"""Pläne (Gratis/Pro), Token-Limit, Aktionen, Abos und Stripe-Anbindung."""
import hashlib
import hmac
import os
import time
from datetime import datetime, timezone

import httpx

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Zurich")
except Exception:  # tzdata fehlt -> UTC
    TZ = timezone.utc

MONTH = 30 * 86400
DEFAULTS = {"price_cents": 500, "currency": "CHF", "free_daily_tokens": 30000}
INT_SETTINGS = {"price_cents", "free_daily_tokens"}
STRIPE_API = "https://api.stripe.com/v1"
STRIPE_TRANSPORT = None  # nur für Tests austauschbar
# Sprachen, die die Stripe-Bezahlseite kennt (sonst "auto" = Browser-Sprache des Kunden)
STRIPE_LOCALES = {"bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fil", "fr", "hr", "hu", "id", "it",
                  "ja", "ko", "lt", "lv", "ms", "mt", "nb", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv",
                  "th", "tr", "vi", "zh"}


def payments_mode() -> str:
    """off = keine Bezahlung (nur Admin kann Pro vergeben), test = simuliert, stripe = echt."""
    m = os.getenv("PAYMENTS_MODE", "off").lower()
    return m if m in ("off", "test", "stripe") else "off"


# ---------------------------------------------------------------- Einstellungen
def get_settings(con) -> dict:
    s = dict(DEFAULTS)
    for r in con.execute("SELECT name, value FROM settings").fetchall():
        s[r["name"]] = int(r["value"]) if r["name"] in INT_SETTINGS else r["value"]
    return s


def set_setting(con, name: str, value):
    con.execute(
        "INSERT INTO settings (name, value) VALUES (?, ?) "
        "ON CONFLICT (name) DO UPDATE SET value = excluded.value",
        (name, str(value)),
    )


# ---------------------------------------------------------------- Profil / Plan
def get_profile(con, user_id: int, now: float | None = None) -> dict:
    now = now or time.time()
    row = con.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        con.execute(
            "INSERT INTO profiles (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING", (user_id,)
        )
        row = con.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    p = dict(row)
    # Test-Modus: automatische Verlängerung simulieren (ohne echte Zahlung)
    if payments_mode() == "test" and p["auto_renew"] and 0 < p["pro_until"] <= now:
        p["pro_until"] += (int((now - p["pro_until"]) // MONTH) + 1) * MONTH
        con.execute("UPDATE profiles SET pro_until = ? WHERE user_id = ?", (p["pro_until"], user_id))
    return p


def is_admin(p: dict) -> bool:
    """Admin-Rechte gibt es erst, wenn das Start-Passwort geändert wurde."""
    return bool(p["is_admin"]) and not p["pw_default"]


def is_pro(p: dict, now: float | None = None) -> bool:
    now = now or time.time()
    return is_admin(p) or p["pro_until"] > now


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def tokens_used(con, user_id: int) -> int:
    r = con.execute(
        "SELECT tokens FROM token_usage WHERE user_id = ? AND day = ?", (user_id, today())
    ).fetchone()
    return r["tokens"] if r else 0


def add_tokens(con, user_id: int, n: int):
    con.execute(
        "INSERT INTO token_usage (user_id, day, tokens) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, day) DO UPDATE SET tokens = token_usage.tokens + excluded.tokens",
        (user_id, today(), int(n)),
    )


# ---------------------------------------------------------------- Aktionen
def active_promo(con, now: float | None = None):
    now = now or time.time()
    r = con.execute(
        "SELECT id, name, free_days, ends_at FROM promos WHERE active = 1 "
        "AND (ends_at = 0 OR ends_at > ?) ORDER BY free_days DESC, id DESC LIMIT 1",
        (now,),
    ).fetchone()
    return dict(r) if r else None


def eligible_promo(con, p: dict, now: float | None = None):
    """Aktionen gelten nur für Nutzer, die noch nie eine Gratis-Phase hatten."""
    if p["trial_used"] or is_admin(p):
        return None
    return active_promo(con, now)


def subscribe_block_reason(p: dict, now: float | None = None) -> str | None:
    now = now or time.time()
    if is_admin(p):
        return "admin_free"
    if p["pro_until"] > now and p["auto_renew"]:
        return "already_active"
    if p["pro_until"] > now:
        return "still_pro"
    return None


def account_info(con, user: dict, now: float | None = None) -> dict:
    now = now or time.time()
    p = get_profile(con, user["id"], now)
    s = get_settings(con)
    admin, pro = is_admin(p), is_pro(p, now)
    promo = eligible_promo(con, p, now)
    used = tokens_used(con, user["id"])
    return {
        "username": user["username"],
        "is_admin": admin,
        "admin_needs_password_change": bool(p["is_admin"] and p["pw_default"]),
        "plan": "pro" if pro else "free",
        "pro_source": "admin" if admin else ("subscription" if pro else None),
        "pro_until": p["pro_until"] if p["pro_until"] > now else 0,
        "auto_renew": bool(p["auto_renew"]) and p["pro_until"] > now,
        "ended_reason": p["ended_reason"],
        "sub_price_cents": p["price_cents"],
        "price_cents": s["price_cents"],
        "currency": s["currency"],
        "promo": {"name": promo["name"], "free_days": promo["free_days"]} if promo else None,
        "free": {
            "limit": s["free_daily_tokens"],
            "used": used,
            "remaining": max(0, s["free_daily_tokens"] - used),
        },
        "payments": payments_mode(),
        "can_portal": payments_mode() == "stripe" and bool(p["stripe_customer"]),
        "subscribe_block": subscribe_block_reason(p, now),
    }


# ---------------------------------------------------------------- Abo-Aktionen
def activate_test(con, user_id: int, now: float | None = None):
    """Test-Modus: Abo ohne echte Zahlung starten."""
    now = now or time.time()
    p = get_profile(con, user_id, now)
    promo = eligible_promo(con, p, now)
    s = get_settings(con)
    seconds = promo["free_days"] * 86400 if promo else MONTH
    con.execute(
        "UPDATE profiles SET pro_until = ?, auto_renew = 1, price_cents = ?, ended_reason = '', "
        "trial_used = ? WHERE user_id = ?",
        (now + seconds, s["price_cents"], 1 if promo else p["trial_used"], user_id),
    )


def cancel_local(con, user_id: int):
    con.execute(
        "UPDATE profiles SET auto_renew = 0, ended_reason = 'cancelled' WHERE user_id = ?", (user_id,)
    )


def change_price(con, new_cents: int) -> list[str]:
    """Preis ändern. Alle laufenden Abos enden zum Ende ihres bezahlten Monats.

    Gibt die Stripe-Abo-IDs zurück, die dort noch gekündigt werden müssen."""
    old = get_settings(con)["price_cents"]
    set_setting(con, "price_cents", new_cents)
    if new_cents == old:
        return []
    subs = [
        r["stripe_sub"]
        for r in con.execute(
            "SELECT stripe_sub FROM profiles WHERE auto_renew = 1 AND is_admin = 0 AND stripe_sub != ''"
        ).fetchall()
    ]
    con.execute(
        "UPDATE profiles SET auto_renew = 0, ended_reason = 'price_change' "
        "WHERE auto_renew = 1 AND is_admin = 0"
    )
    return subs


def grant_days(con, user_id: int, days: int, now: float | None = None):
    now = now or time.time()
    p = get_profile(con, user_id, now)
    base = max(p["pro_until"], now)
    con.execute(
        "UPDATE profiles SET pro_until = ?, ended_reason = CASE WHEN auto_renew = 1 THEN ended_reason "
        "ELSE 'gift' END WHERE user_id = ?",
        (base + days * 86400, user_id),
    )


def revoke_pro(con, user_id: int) -> str:
    p = get_profile(con, user_id)
    con.execute(
        "UPDATE profiles SET pro_until = 0, auto_renew = 0, ended_reason = 'revoked' WHERE user_id = ?",
        (user_id,),
    )
    return p["stripe_sub"]


# ---------------------------------------------------------------- Stripe
async def stripe_call(method: str, path: str, data: dict | None = None) -> dict:
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY ist nicht gesetzt")
    async with httpx.AsyncClient(timeout=30, transport=STRIPE_TRANSPORT) as h:
        r = await h.request(method, STRIPE_API + path, data=data, auth=(key, ""))
    j = r.json()
    if r.status_code >= 400:
        raise RuntimeError("Stripe: " + str(j.get("error", {}).get("message", r.status_code)))
    return j


async def stripe_checkout(user: dict, price_cents: int, currency: str, trial_days: int, base_url: str,
                          lang: str = ""):
    data = {
        "mode": "subscription",
        "client_reference_id": str(user["id"]),
        "success_url": base_url + "/?checkout=success",
        "cancel_url": base_url + "/?checkout=cancel",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency.lower(),
        "line_items[0][price_data][unit_amount]": str(price_cents),
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": "Lumora Pro",
        "subscription_data[metadata][user_id]": str(user["id"]),
        "subscription_data[metadata][price_cents]": str(price_cents),
    }
    data["locale"] = lang if lang in STRIPE_LOCALES else "auto"
    if trial_days:
        data["subscription_data[trial_period_days]"] = str(trial_days)
    session = await stripe_call("POST", "/checkout/sessions", data)
    return session["url"]


async def stripe_portal(customer_id: str, return_url: str) -> str:
    """Stripe-Kundenportal: Karte ändern, Rechnungen ansehen."""
    s = await stripe_call("POST", "/billing_portal/sessions", {"customer": customer_id, "return_url": return_url})
    return s["url"]


async def stripe_cancel_at_period_end(sub_id: str):
    await stripe_call("POST", f"/subscriptions/{sub_id}", {"cancel_at_period_end": "true"})


async def stripe_cancel_now(sub_id: str):
    await stripe_call("DELETE", f"/subscriptions/{sub_id}")


def verify_webhook(payload: bytes, header: str, secret: str, now: float | None = None,
                   tolerance: int = 300) -> bool:
    now = now or time.time()
    try:
        parts = [x.split("=", 1) for x in header.split(",")]
        t = next(v for k, v in parts if k == "t")
        sigs = [v for k, v in parts if k == "v1"]
        if abs(now - int(t)) > tolerance:
            return False
        expected = hmac.new(secret.encode(), t.encode() + b"." + payload, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, s) for s in sigs)
    except Exception:
        return False


def _period_end(sub: dict) -> int:
    if sub.get("current_period_end"):
        return int(sub["current_period_end"])
    items = (sub.get("items") or {}).get("data") or []  # neuere Stripe-API-Versionen
    return int(items[0]["current_period_end"]) if items and items[0].get("current_period_end") else 0


def apply_subscription(con, sub: dict, adopt: bool = False):
    """Stripe-Abo-Zustand in unser Profil übernehmen.

    Ereignisse zu einem anderen als dem gespeicherten Abo (z. B. ein altes, beendetes) werden ignoriert;
    nur ein neu angelegtes Abo (adopt=True) wird übernommen."""
    meta = sub.get("metadata") or {}
    uid = meta.get("user_id")
    if uid:
        uid = int(uid)
    else:
        r = con.execute("SELECT user_id FROM profiles WHERE stripe_sub = ?", (sub.get("id", ""),)).fetchone()
        uid = r["user_id"] if r else None
    if not uid:
        return
    row = get_profile(con, uid)
    if row["stripe_sub"] and row["stripe_sub"] != sub.get("id") and not adopt:
        return
    status = sub.get("status", "")
    cancelling = bool(sub.get("cancel_at_period_end") or sub.get("cancel_at"))
    if status in ("active", "trialing", "past_due"):
        end = _period_end(sub)
        reason = row["ended_reason"]
        if cancelling and reason == "":
            reason = "cancelled"
        elif not cancelling:
            reason = ""
        con.execute(
            "UPDATE profiles SET pro_until = ?, auto_renew = ?, stripe_sub = ?, stripe_customer = ?, "
            "price_cents = ?, trial_used = ?, ended_reason = ? WHERE user_id = ?",
            (
                end if (status != "past_due" and end) else row["pro_until"],
                0 if cancelling else 1,
                sub.get("id", ""),
                sub.get("customer") or "",
                int(meta.get("price_cents") or 0) or row["price_cents"],
                1 if status == "trialing" else row["trial_used"],
                reason,
                uid,
            ),
        )
    else:  # canceled, unpaid, incomplete_expired …
        con.execute(
            "UPDATE profiles SET auto_renew = 0, ended_reason = ? WHERE user_id = ?",
            (row["ended_reason"] or "cancelled", uid),
        )


def handle_event(con, event: dict):
    t, obj = event.get("type", ""), (event.get("data") or {}).get("object") or {}
    if t in ("customer.subscription.created", "customer.subscription.updated",
             "customer.subscription.deleted"):
        apply_subscription(con, obj, adopt=(t == "customer.subscription.created"))
    elif t == "checkout.session.completed" and obj.get("client_reference_id"):
        uid = int(obj["client_reference_id"])
        p = get_profile(con, uid)
        new_sub = obj.get("subscription") or ""
        active = p["auto_renew"] and p["pro_until"] > time.time()
        keep_old = bool(p["stripe_sub"]) and p["stripe_sub"] != new_sub and active
        con.execute(
            "UPDATE profiles SET stripe_customer = ?, stripe_sub = ? WHERE user_id = ?",
            (obj.get("customer") or p["stripe_customer"], p["stripe_sub"] if (keep_old or not new_sub) else new_sub, uid),
        )
