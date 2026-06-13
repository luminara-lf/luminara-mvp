"""
Luminara WhatsApp webhook — receives vendor messages from the Twilio WhatsApp
sandbox, runs them through the shared NL parser, and stores a SINGLE pending
update for the operator to approve inside the Streamlit dashboard.

Nothing is applied automatically. The webhook only:
  1. parses the incoming message,
  2. writes data/pending_updates.json (one pending update at a time),
  3. replies to the sender (in their own language) that it is pending review.

Run: flask --app webhook run --port 5000      (or via run_with_whatsapp.sh)

Credentials are loaded from .env via python-dotenv — never hardcoded.
"""

import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, request

from nl_parser import compute_new_date, parse_message

load_dotenv()

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_PATH = os.path.join(_THIS_DIR, "data", "pending_updates.json")

app = Flask(__name__)


# ── Bilingual canned replies ──────────────────────────────────────────────────

REPLIES = {
    "received": {
        "en": (
            "✅ Got it! Your delivery update was received and is *pending review* "
            "by the Luminara operations team. We'll apply it once an operator "
            "approves. Thank you!"
        ),
        "es": (
            "✅ ¡Recibido! Tu actualización de entrega fue recibida y está "
            "*pendiente de revisión* por el equipo de operaciones de Luminara. "
            "La aplicaremos cuando un operador la apruebe. ¡Gracias!"
        ),
    },
    "clarify": {
        "en": (
            "🤔 Thanks for your message, but we couldn't identify which install or "
            "delivery date it refers to. Please include the *customer or location* "
            "and the *new delivery date or delay*, e.g.:\n"
            "\"The inverters for the Ponce job are delayed 3 days.\""
        ),
        "es": (
            "🤔 Gracias por tu mensaje, pero no pudimos identificar a qué instalación "
            "o fecha de entrega se refiere. Por favor incluye el *cliente o la "
            "ubicación* y la *nueva fecha de entrega o el retraso*, por ejemplo:\n"
            "\"Los inversores para el trabajo de Ponce están retrasados 3 días.\""
        ),
    },
    "busy": {
        "en": (
            "⏳ Thanks! There's already an update waiting for operator review. "
            "Please resend this once the previous one has been approved or rejected."
        ),
        "es": (
            "⏳ ¡Gracias! Ya hay una actualización esperando revisión del operador. "
            "Por favor reenvía este mensaje cuando la anterior haya sido aprobada o "
            "rechazada."
        ),
    },
}


# ── Twilio address normalization ──────────────────────────────────────────────

def normalize_whatsapp_from(raw: str | None) -> str | None:
    """
    Normalize a Twilio WhatsApp 'From' address to canonical 'whatsapp:+<digits>'.

    URL-encoding can turn the '+' in a phone number into a space, so a sender
    like 'whatsapp:+18163798090' may arrive mangled as 'whatsapp: 18163798090'.
    This strips whitespace after the 'whatsapp:' prefix and re-adds the leading
    '+' on the number if it is missing. Returns None/"" unchanged so callers can
    still detect a genuinely missing sender.
    """
    if not raw:
        return raw

    text = raw.strip()
    prefix = ""
    number = text
    if ":" in text:
        head, number = text.split(":", 1)
        prefix = head.strip() + ":"  # e.g. "whatsapp:"

    # Remove ALL whitespace inside the number part (handles the '+'→space case).
    number = "".join(number.split())

    # Ensure the phone number starts with '+'.
    if number and not number.startswith("+"):
        number = "+" + number

    return prefix + number


# ── Twilio reply ──────────────────────────────────────────────────────────────

def send_whatsapp_reply(to: str, body: str) -> None:
    """
    Send a WhatsApp reply via the Twilio REST API. Credentials come from .env.

    `to` is the sender address EXACTLY as Twilio delivered it
    (e.g. "whatsapp:+18163798090") — it is never reformatted, trimmed, or
    defaulted. A missing `to` is a hard error, never a fallback number.
    """
    # No fallback recipient — refuse to invent a number.
    if not to:
        raise ValueError(
            "Cannot send WhatsApp reply: 'to' (sender) is missing. "
            "The Twilio 'From' field was empty."
        )

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")

    if not (account_sid and auth_token and from_number):
        print("[webhook] Twilio credentials missing — skipping reply. "
              "Set TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM in .env.")
        return

    # Log the EXACT addresses (repr reveals any stray spaces or a missing '+').
    print(f"[webhook] Sending reply -> To: {to!r} | From: {from_number!r}")

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        client.messages.create(from_=from_number, to=to, body=body)
        print(f"[webhook] Reply sent to {to!r}")
    except Exception as exc:  # noqa: BLE001 — never let a reply failure 500 the webhook
        print(f"[webhook] Failed to send WhatsApp reply: {exc}")


# ── Pending-update storage ────────────────────────────────────────────────────

def pending_exists() -> bool:
    return os.path.exists(PENDING_PATH)


def write_pending_update(raw_message: str, item: dict, language: str, from_number: str) -> None:
    """Persist exactly one pending update for the operator to approve."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_message": raw_message,
        "customer": item.get("customer"),
        "equipment": item.get("item"),
        "new_delivery_date": compute_new_date(item),
        "vendor": item.get("vendor"),
        "language": language,
        "from_number": from_number,  # sender, so app.py can notify them on approve/reject
    }
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    with open(PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[webhook] Wrote pending update: {payload}")


def is_actionable(item: dict) -> bool:
    """
    True only if we extracted enough to be useful: at least one way to identify
    the install (customer / location / equipment / vendor) AND a concrete new
    delivery date (absolute or from a relative delay). Otherwise we ask for
    clarification rather than store a useless pending update.
    """
    has_identity = any(item.get(k) for k in ("customer", "location", "item", "vendor"))
    return bool(has_identity and compute_new_date(item))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    body = (request.form.get("Body") or "").strip()
    # Read the sender, then normalize immediately — URL-encoding can mangle
    # 'whatsapp:+18163798090' into 'whatsapp: 18163798090' (space, no '+').
    # Everything downstream uses the normalized value.
    raw_from = request.form.get("From")
    sender = normalize_whatsapp_from(raw_from)
    print(f"[webhook] Incoming -> From(raw): {raw_from!r} -> From: {sender!r} | Body: {body!r}")

    if not sender:
        # No fallback sender — fail loudly so the misconfiguration is visible
        # rather than replying to some default/placeholder number.
        print("[webhook] ERROR: missing 'From' in payload — cannot reply.")
        return ("Missing 'From' field in webhook payload", 400)

    if not body:
        # Empty message — ask for clarification in English by default.
        send_whatsapp_reply(sender, REPLIES["clarify"]["en"])
        return ("", 200)

    parsed = parse_message(body)
    language = parsed.get("language", "en")
    items = parsed.get("items", [])

    # Enforce "one pending update at a time".
    if pending_exists():
        send_whatsapp_reply(sender, REPLIES["busy"][language])
        return ("", 200)

    # Pick the first actionable item (one pending update at a time for now).
    actionable = next((it for it in items if is_actionable(it)), None)

    if actionable is None:
        send_whatsapp_reply(sender, REPLIES["clarify"][language])
        return ("", 200)

    write_pending_update(body, actionable, language, sender)
    send_whatsapp_reply(sender, REPLIES["received"][language])
    return ("", 200)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "pending": pending_exists()}, 200


if __name__ == "__main__":
    # Port 5000 is reserved by macOS AirPlay Receiver (returns 403); use 5050.
    # run_with_whatsapp.sh launches this via the flask CLI on the same port.
    app.run(host="127.0.0.1", port=5050)
