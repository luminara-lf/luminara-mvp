"""
Shared natural-language vendor-message parser — NO Streamlit dependency.

This mirrors the extraction logic embedded in app.py (build_llm_prompt /
parse_vendor_message) but is import-safe from any context: the Streamlit app,
the standalone test harness, and the WhatsApp webhook all reuse the same prompt
and the same Anthropic call so message parsing stays consistent everywhere.

Returns a dict:
    {"language": "en" | "es", "items": [ {extracted fields...}, ... ]}

`language` is always populated (even when no items can be extracted) so callers
can reply to the sender in the language they wrote in.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta

import anthropic
import pandas as pd

# Same model the Streamlit app uses — keep these in sync.
MODEL = "claude-sonnet-4-6"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_THIS_DIR, "data")


# ── Known-value lists (drive fuzzy matching in the prompt) ────────────────────

def load_known_lists(data_dir: str = DATA_DIR) -> dict:
    """Read the CSVs and return the known customers / vendors / items / locations."""
    events = pd.read_csv(os.path.join(data_dir, "install_schedule.csv"))
    orders = pd.read_csv(os.path.join(data_dir, "purchase_orders.csv"))
    return {
        "customers": sorted(events["customer"].dropna().unique().tolist()),
        "vendors": sorted(orders["vendor"].dropna().unique().tolist()),
        "items": sorted(orders["equipment_item"].dropna().unique().tolist()),
        "locations": sorted(events["location"].dropna().unique().tolist()),
    }


# ── Date helper (mirrors app.py.compute_new_date) ─────────────────────────────

def compute_new_date(extracted: dict) -> str | None:
    """Return a YYYY-MM-DD string for the new delivery date, absolute or relative."""
    if extracted.get("new_delivery_date"):
        return str(extracted["new_delivery_date"])
    delay = extracted.get("delay_days")
    if delay and isinstance(delay, (int, float)) and delay > 0:
        return (date.today() + timedelta(days=int(delay))).strftime("%Y-%m-%d")
    return None


# ── Vendor inference (fallback when the message didn't name a vendor) ─────────

def _keywords(text: str) -> set[str]:
    """Lowercased alphanumeric tokens (len >= 2) — e.g. 'Inverter 7kW' → {inverter, 7kw}."""
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if len(w) >= 2}


def infer_vendor(customer, equipment=None, data_dir: str = DATA_DIR) -> str | None:
    """
    Look up the vendor for a purchase order from data/purchase_orders.csv when the
    message didn't mention one.

    1. Match rows whose `customer` equals the parsed customer (case-insensitive,
       whitespace-stripped).
    2. If `equipment` is given, prefer rows whose `equipment_item` shares the most
       keywords with it; fall back to the customer-only matches if none overlap.
    3. Return the chosen row's vendor, or None if there is no customer match
       (or the vendor cell is blank).
    """
    if not customer:
        return None
    try:
        orders = pd.read_csv(os.path.join(data_dir, "purchase_orders.csv"))
    except Exception:  # noqa: BLE001 — missing/unreadable CSV → just skip inference
        return None
    if "customer" not in orders.columns or "vendor" not in orders.columns:
        return None

    cust = str(customer).strip().lower()
    matches = orders[orders["customer"].astype(str).str.strip().str.lower() == cust]
    if matches.empty:
        return None

    if equipment and "equipment_item" in matches.columns:
        kws = _keywords(equipment)
        if kws:
            overlap = matches["equipment_item"].apply(lambda e: len(kws & _keywords(e)))
            best = overlap.max()
            if best > 0:
                matches = matches[overlap == best]

    vendor = matches.iloc[0]["vendor"]
    if pd.isna(vendor) or str(vendor).strip() == "":
        return None
    return str(vendor)


# ── Prompt (mirrors app.py.build_llm_prompt + a top-level language field) ─────

def build_system_prompt(known: dict) -> str:
    today_str = date.today().isoformat()
    return f"""You are a logistics assistant for a solar installation company in Puerto Rico.
Today's date is {today_str}.

Extract ALL delivery update information from the vendor message. The message may be in English or Spanish.
Translate any Spanish terms to English in your output so they match the known lists below.

Spanish → English glossary:
- atrasado / retraso = delayed
- semanas = weeks (multiply by 7 for days)
- días / dias = days
- puerto = port
- aduana / aduanas = customs
- proveedor = supplier / vendor
- inversores = inverters
- paneles solares / paneles = solar panels
- baterías / baterias = batteries
- montaje = mounting hardware

Known customers: {known['customers']}
Known vendors: {known['vendors']}
Known equipment items: {known['items']}
Known locations: {known['locations']}

Rules:
1. A delay message means delivery is NOT confirmed. Always set delivery_confirmed to false.
2. Resolve relative dates to absolute YYYY-MM-DD from today ({today_str}):
   - "next week" = 7 days → {(date.today() + timedelta(days=7)).isoformat()}
   - "2 weeks" / "2 semanas" = 14 days
   - "end of month" = last day of current month
   - "X days/días" = X days from today
3. Match customer, vendor, and item names against the known lists using fuzzy reasoning.
4. If multiple equipment items or customers are mentioned, return one entry per item.
5. Confidence rules: high = 3+ fields identified; medium = 2 fields; low = 1 or fewer.
6. ALWAYS detect the language of the message and return it as the top-level "language"
   field, using "en" for English or "es" for Spanish — even when no delivery details
   can be extracted (in that case return an empty "items" list).

Return ONLY valid JSON — no markdown, no explanation:
{{
  "language": "<en|es>",
  "items": [
    {{
      "customer": "<matched customer from known list, or best guess, or null>",
      "location": "<city or address mentioned, or null>",
      "item": "<matched item from known list, or best English description, or null>",
      "vendor": "<matched vendor from known list, or null>",
      "delay_days": <integer days from today if relative delay, or null>,
      "new_delivery_date": "<YYYY-MM-DD absolute date, or null>",
      "delivery_confirmed": false,
      "confidence": "<high|medium|low>",
      "notes": "<brief note about uncertainty or what was detected>"
    }}
  ]
}}"""


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_message(
    message: str,
    known: dict | None = None,
    api_key: str | None = None,
) -> dict:
    """
    Parse a free-text vendor message via the Anthropic API.

    Returns {"language": "en"|"es", "items": [...]}.
    On any API or JSON failure, returns {"language": <best guess>, "items": []}
    so callers never crash and can still reply for clarification.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment / .env file.")

    if known is None:
        known = load_known_lists()

    system = build_system_prompt(known)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — webhook must stay alive on any failure
        print(f"[nl_parser] parse failed: {exc}")
        return {"language": _guess_language(message), "items": []}

    # Tolerate a flat dict (no "items" wrapper) for robustness.
    items = parsed.get("items")
    if items is None:
        items = [parsed] if any(parsed.get(k) for k in ("customer", "item", "vendor", "location")) else []

    language = parsed.get("language") or _guess_language(message)
    if language not in ("en", "es"):
        language = _guess_language(message)

    # Fill in the vendor ONLY when the message didn't name one — never override
    # a vendor that was explicitly mentioned and parsed.
    for it in items:
        if not it.get("vendor"):
            inferred = infer_vendor(it.get("customer"), it.get("item"))
            if inferred:
                it["vendor"] = inferred

    return {"language": language, "items": items}


# ── Cheap fallback language guess (only used when the model didn't answer) ────

_ES_HINTS = {
    "el", "la", "los", "las", "para", "está", "estan", "están", "retraso",
    "atrasado", "días", "dias", "semanas", "entrega", "aduana", "puerto",
    "proveedor", "paneles", "inversores", "baterías", "baterias",
}


def _guess_language(message: str) -> str:
    words = {w.strip(".,!¡¿?").lower() for w in message.split()}
    return "es" if words & _ES_HINTS else "en"
