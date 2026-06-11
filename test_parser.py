"""
Standalone parser test — runs against the real Anthropic API.
Does NOT require Streamlit. Uses the same prompt logic as app.py.
Run: python test_parser.py
"""

import json
import os
import sys
from datetime import date, timedelta

import anthropic
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from engine.risk_engine import score_all, summary

# ── Load data ─────────────────────────────────────────────────────────────────
events_raw  = pd.read_csv("data/install_schedule.csv")
orders_raw  = pd.read_csv("data/purchase_orders.csv")
vendors_raw = pd.read_csv("data/vendor_list.csv")

events = events_raw.rename(columns={"install_date": "event_date"})
orders = orders_raw.rename(columns={
    "equipment_item": "item",
    "expected_delivery_date": "order_date",
    "delivery_confirmed": "confirmed",
})

known_customers = sorted(events["customer"].dropna().unique().tolist())
known_vendors   = sorted(orders["vendor"].dropna().unique().tolist())
known_items     = sorted(orders["item"].dropna().unique().tolist())
known_locations = sorted(events["location"].dropna().unique().tolist())


# ── Helpers (mirrors app.py, no Streamlit) ────────────────────────────────────

def partial_match(needle, haystack) -> bool:
    if not needle or not haystack:
        return False
    n = str(needle).strip().lower()
    h = str(haystack).strip().lower()
    if len(n) < 3:
        return n == h
    return n in h or h in n


def compute_new_date(extracted: dict) -> str | None:
    if extracted.get("new_delivery_date"):
        return str(extracted["new_delivery_date"])
    delay = extracted.get("delay_days")
    if delay and isinstance(delay, (int, float)) and delay > 0:
        return (date.today() + timedelta(days=int(delay))).strftime("%Y-%m-%d")
    return None


def find_matching_rows(extracted: dict) -> tuple[list, list[str], str | None]:
    cust_loc = dict(zip(events["customer"], events["location"]))
    work = orders.copy()
    work["_location"] = work["customer"].map(cust_loc)

    active = pd.Series([True] * len(work), index=work.index)
    matched_fields: list[str] = []

    # (field_name, col, value, is_hard)
    checks = [
        ("customer", "customer",  extracted.get("customer"), True),
        ("location", "_location", extracted.get("location"), True),
        ("item",     "item",      extracted.get("item"),     False),
        ("vendor",   "vendor",    extracted.get("vendor"),   False),
    ]

    for field_name, col, value, is_hard in checks:
        if not value:
            continue
        field_mask = work[col].apply(lambda x: partial_match(value, x))
        narrowed = active & field_mask

        if is_hard:
            if not narrowed.any():
                return [], [], f"{field_name}_not_found"
            active = narrowed
            matched_fields.append(field_name)
        else:
            if narrowed.any():
                active = narrowed
                matched_fields.append(field_name)

    return work[active].index.tolist(), matched_fields, None


def call_llm(message: str) -> list[dict] | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return None

    today_str = date.today().isoformat()
    system = f"""You are a logistics assistant for a solar installation company in Puerto Rico.
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

Known customers: {known_customers}
Known vendors: {known_vendors}
Known equipment items: {known_items}
Known locations: {known_locations}

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

Return ONLY valid JSON — no markdown, no explanation:
{{
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

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
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
    return parsed.get("items", [parsed])


# ── Test runner ───────────────────────────────────────────────────────────────

TEST_MESSAGES = [
    (
        "English",
        "the solar panel 300W equipment needed for Santiago Grid will be stuck at the port for 26 days",
    ),
    (
        "Spanish",
        "Los inversores de SolarEdge para los trabajos de Ponce estan atascados en aduanas, probablemente 2 semanas mas",
    ),
    (
        "Ambiguous",
        "panels are delayed again",
    ),
]

print(f"\n{'='*70}")
print(f"  Luminara Parser Test — today: {date.today().isoformat()}")
print(f"{'='*70}")

for label, msg in TEST_MESSAGES:
    print(f"\n{'─'*70}")
    print(f"TEST: {label}")
    print(f"MESSAGE: {msg}")
    print()

    items = call_llm(msg)
    if items is None:
        print("  ✗ API call failed")
        continue

    for i, item in enumerate(items, 1):
        print(f"  Extracted item {i}:")
        for k, v in item.items():
            if v is not None and v != "" and v is not False:
                print(f"    {k:22s}: {v}")

        matched_indices, matched_fields, fail_reason = find_matching_rows(item)
        match_score = len(matched_fields)
        new_date = compute_new_date(item)
        confidence = item.get("confidence", "unknown")

        print(f"\n  Matching:")
        print(f"    Fields matched         : {matched_fields} (score={match_score})")
        print(f"    Rows matched           : {len(matched_indices)}")

        if matched_indices:
            for idx in matched_indices:
                row = orders.loc[idx]
                print(f"      → {row['customer']} | {row['item']} | vendor: {row['vendor']} | current date: {row['order_date']}")

        print(f"\n  Decision:")
        if match_score == 0:
            print(f"    ✗ NO MATCH — reason: {fail_reason or 'no fields extracted'}")
        elif match_score >= 2 and confidence != "low":
            print(f"    ✅ AUTO-UPDATE — new delivery date: {new_date}")
        else:
            reason = f"only {match_score} field(s) matched" if match_score < 2 else "confidence is low"
            print(f"    ⚠️  NEEDS CONFIRMATION — reason: {reason}")
            print(f"       Would propose new delivery date: {new_date}")

print(f"\n{'='*70}\n")
