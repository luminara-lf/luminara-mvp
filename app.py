"""
Luminara AI — Solar Installation Risk Dashboard
Run: streamlit run app.py
"""

import json
import os
import sys
from datetime import date, timedelta

import anthropic
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from engine.risk_engine import (
    score_all, summary, upcoming, vendor_scorecard, DELAY_PROB_THRESHOLD,
)

load_dotenv()

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Luminara AI", page_icon="☀️", layout="wide")

# ── Session state defaults ────────────────────────────────────────────────────
_DEFAULTS = {
    "events_df": None,
    "orders_df": None,
    "vendors_df": None,
    "scored_df": None,
    "files_hash": None,
    "update_result": None,          # {changes: [...], no_matches: [...]}
    "pending_confirmation": None,   # list of candidates needing user confirmation
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── In-memory data store that survives browser refreshes ──────────────────────
# A browser refresh starts a NEW Streamlit session, which wipes session_state — so
# session_state alone cannot survive a refresh. st.cache_resource is a single,
# process-wide object shared across all sessions: it persists the uploaded data
# across refreshes (new sessions) yet is lost when the app process restarts
# (Ctrl+C + rerun) — exactly the required lifetime.
DATA_KEYS = ("events_df", "orders_df", "vendors_df", "scored_df", "files_hash")


@st.cache_resource
def _data_store() -> dict:
    """Process-global store of the loaded dataframes (one per server process)."""
    return {k: None for k in DATA_KEYS}


def _persist_to_store() -> None:
    """Copy the current session's dataframes into the process-global store."""
    store = _data_store()
    for k in DATA_KEYS:
        store[k] = st.session_state.get(k)


def _hydrate_from_store() -> None:
    """On a fresh session (e.g. after a browser refresh), restore data from the store."""
    store = _data_store()
    for k in DATA_KEYS:
        if st.session_state.get(k) is None and store.get(k) is not None:
            st.session_state[k] = store[k]


_hydrate_from_store()

# ── Column name maps (solar CSV names → engine names) ─────────────────────────
EVENTS_MAP = {"install_date": "event_date"}
ORDERS_MAP = {
    "equipment_item": "item",
    "expected_delivery_date": "order_date",
    "delivery_confirmed": "confirmed",
}

# Required columns per uploaded file (solar CSV names).
REQUIRED_EVENT_COLS  = {"customer", "install_date", "location"}
REQUIRED_ORDER_COLS  = {"customer", "equipment_item", "vendor", "expected_delivery_date", "delivery_confirmed"}
REQUIRED_VENDOR_COLS = {"vendor_name", "reliability_rating"}

# ── Risk score color palette ──────────────────────────────────────────────────
SCORE_BG = {"GREEN": "#c6efce", "YELLOW": "#ffeb9c", "RED": "#ffc7ce"}

# ── Pending WhatsApp update file (written by webhook.py) ───────────────────────
# The uploaded dashboard data is held in memory only (session_state + a process-
# global st.cache_resource store) — never written to disk. It survives browser
# refreshes but resets when the app process restarts. See _data_store() above.
PENDING_PATH = os.path.join(os.path.dirname(__file__), "data", "pending_updates.json")


# ═════════════════════════════════════════════════════════════════════════════
# Pure helper functions (no Streamlit dependencies)
# ═════════════════════════════════════════════════════════════════════════════

def partial_match(needle, haystack) -> bool:
    """Case-insensitive substring match; needle must be at least 3 chars."""
    if not needle or not haystack:
        return False
    n = str(needle).strip().lower()
    h = str(haystack).strip().lower()
    if len(n) < 3:
        return n == h
    return n in h or h in n


def compute_new_date(extracted: dict) -> str | None:
    """
    Return a YYYY-MM-DD string for the new expected delivery date.
    Handles both absolute dates and relative delays from today.
    """
    if extracted.get("new_delivery_date"):
        return str(extracted["new_delivery_date"])
    delay = extracted.get("delay_days")
    if delay and isinstance(delay, (int, float)) and delay > 0:
        return (date.today() + timedelta(days=int(delay))).strftime("%Y-%m-%d")
    return None


def find_matching_rows(
    extracted: dict,
    orders: pd.DataFrame,
    events: pd.DataFrame,
) -> tuple[list, list[str], str | None]:
    """
    Find purchase order rows matching the extracted fields.

    Returns (original_index_list, matched_fields, fail_reason).

    Hard fields (customer, location): if mentioned they MUST match at least one
    row. If they match nothing, the search stops immediately and returns
    ([], [], "<field>_not_found") so the caller can show a specific message.

    Soft fields (item, vendor): applied only if they narrow without eliminating
    all remaining candidates; otherwise silently skipped.
    """
    cust_loc: dict[str, str] = {}
    if "location" in events.columns:
        cust_loc = dict(zip(events["customer"], events["location"]))

    # Only unconfirmed rows need operator action — exclude already-confirmed rows.
    work = orders[orders["confirmed"].str.strip().str.lower() != "yes"].copy()
    work["_location"] = work["customer"].map(cust_loc)

    active = pd.Series([True] * len(work), index=work.index)
    matched_fields: list[str] = []

    # (field_name, column_in_work, extracted_value, is_hard)
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
                # Hard field mentioned but no rows match — stop immediately
                return [], [], f"{field_name}_not_found"
            active = narrowed
            matched_fields.append(field_name)
        else:
            if narrowed.any():
                active = narrowed
                matched_fields.append(field_name)

    return work[active].index.tolist(), matched_fields, None


def build_llm_prompt(known_customers, known_vendors, known_items, known_locations) -> str:
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


# ── Scoring / display helpers ─────────────────────────────────────────────────

def recompute() -> None:
    scored = score_all(
        st.session_state.events_df,
        st.session_state.orders_df,
        st.session_state.vendors_df,
    )
    st.session_state.scored_df = scored
    # recompute() is the funnel for every data change (upload, approve, manual
    # apply) — mirror the fresh data into the refresh-surviving store.
    _persist_to_store()


# ── Upload ingestion (data lives in session_state + a process-global store) ───

def schema_errors(events_raw, orders_raw, vendors_raw) -> list[str]:
    """Return a list of human-readable missing-column messages (empty if valid)."""
    msgs = []
    miss_ev = REQUIRED_EVENT_COLS  - set(events_raw.columns)
    miss_po = REQUIRED_ORDER_COLS  - set(orders_raw.columns)
    miss_vn = REQUIRED_VENDOR_COLS - set(vendors_raw.columns)
    if miss_ev: msgs.append(f"Install Schedule is missing columns: {miss_ev}")
    if miss_po: msgs.append(f"Purchase Orders is missing columns: {miss_po}")
    if miss_vn: msgs.append(f"Vendor List is missing columns: {miss_vn}")
    return msgs


def ingest_dataframes(events_raw, orders_raw, vendors_raw) -> None:
    """Rename raw (solar) columns to engine names, store in session, and score."""
    st.session_state.events_df  = events_raw.rename(columns=EVENTS_MAP)
    st.session_state.orders_df  = orders_raw.rename(columns=ORDERS_MAP)
    st.session_state.vendors_df = vendors_raw
    recompute()


# Text colors for the Delay Risk column.
DELAY_TEXT_GREEN = "#1a7f37"   # confirmed → 0% ✓
DELAY_TEXT_AMBER = "#b35900"   # elevated risk that warns (yellow)
DELAY_TEXT_RED   = "#d1242f"   # elevated risk driving a red score


def format_delay_risk(row) -> str:
    """Render the delay probability cell: '0% ✓' when confirmed, else '25%'."""
    if bool(row.get("confirmed")):
        return "0% ✓"
    prob = row.get("delay_probability")
    if prob is None or pd.isna(prob):
        return ""
    txt = f"{float(prob):g}%"
    # Flag probabilities derived from a missing vendor rating (default 0.80).
    if row.get("reliability_estimated") and float(prob) > 0:
        txt += " (est.)"
    return txt


def delay_risk_color(confirmed, prob, score) -> str | None:
    """Pick the Delay Risk text color, or None for normal text."""
    if confirmed:
        return DELAY_TEXT_GREEN
    if prob is None or pd.isna(prob):
        return None
    if float(prob) >= DELAY_PROB_THRESHOLD:
        return DELAY_TEXT_RED if str(score).lower() == "red" else DELAY_TEXT_AMBER
    return None  # under threshold → normal text


def build_display_df(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["Delivery Confirmed"] = d["confirmed"].map({True: "Yes", False: "No"})
    d["Risk Score"] = d["score"].str.upper()
    d["Delay Risk"] = d.apply(format_delay_risk, axis=1)
    d["Reason"] = d["reason"]
    col_map = {
        "customer": "Customer",
        "location": "Location",
        "event_date": "Install Date",
        "item": "Equipment Item",
        "vendor": "Vendor",
        "order_date": "Expected Delivery",
        "Delivery Confirmed": "Delivery Confirmed",
        "Risk Score": "Risk Score",
        "Delay Risk": "Delay Risk",
        "Reason": "Reason",
    }
    available = [c for c in col_map if c in d.columns]
    return d[available].rename(columns=col_map)


def style_table(display_df: pd.DataFrame, source_df: pd.DataFrame):
    """Color each row by risk score; color the Delay Risk text by probability."""
    def _highlight(row):
        bg = SCORE_BG.get(row["Risk Score"], "#ffffff")
        styles = [f"background-color: {bg}"] * len(row)
        if "Delay Risk" in row.index:
            src = source_df.loc[row.name]
            color = delay_risk_color(
                bool(src.get("confirmed")),
                src.get("delay_probability"),
                src.get("score"),
            )
            cell = f"background-color: {bg}"
            if color:
                cell += f"; color: {color}; font-weight: 600"
            styles[row.index.get_loc("Delay Risk")] = cell
        return styles
    return display_df.style.apply(_highlight, axis=1)


def build_vendor_scorecard_df(sc: pd.DataFrame) -> pd.DataFrame:
    """Map the engine's vendor scorecard to solar-labeled display columns."""
    d = sc.copy()

    def _avg_cell(row):
        avg = row["avg_delay_probability"]
        if avg is None or pd.isna(avg):
            return "—"
        txt = f"{float(avg):g}%"
        return txt

    def _reliability_cell(row):
        txt = f"{round(float(row['reliability_rating']) * 100)}%"
        if row["reliability_estimated"]:
            txt += " (est.)"
        return txt

    out = pd.DataFrame({
        "Vendor":            d["vendor"],
        "Installs Affected": d["installs_affected"],
        "Unconfirmed":       d["unconfirmed_count"],
        "Avg Delay Risk":    d.apply(_avg_cell, axis=1),
        "Reliability":       d.apply(_reliability_cell, axis=1),
        "Vendor Risk":       d["vendor_level"].str.upper(),
    })
    return out


def style_vendor_scorecard(display_df: pd.DataFrame):
    """Color each scorecard row by its Vendor Risk level (reuses SCORE_BG)."""
    def _highlight(row):
        color = SCORE_BG.get(row["Vendor Risk"], "#ffffff")
        return [f"background-color: {color}"] * len(row)
    return display_df.style.apply(_highlight, axis=1)


# ── Parser / updater ──────────────────────────────────────────────────────────

def parse_vendor_message(message: str) -> list[dict] | None:
    """
    Call Anthropic API; return a list of extracted items (one per equipment mentioned).
    Returns None on API or parse failure.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY is not set. Add it to your .env file and restart the app.")
        return None

    orders = st.session_state.orders_df
    events = st.session_state.events_df

    known_customers = sorted(events["customer"].dropna().unique().tolist()) if events is not None else []
    known_vendors   = sorted(orders["vendor"].dropna().unique().tolist())   if orders is not None else []
    known_items     = sorted(orders["item"].dropna().unique().tolist())     if orders is not None else []
    known_locations = sorted(events["location"].dropna().unique().tolist()) if events is not None and "location" in events.columns else []

    system = build_llm_prompt(known_customers, known_vendors, known_items, known_locations)

    try:
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
        return parsed.get("items", [parsed])  # tolerate flat dict fallback
    except json.JSONDecodeError as exc:
        st.error(f"Could not parse the AI response as JSON: {exc}")
        return None
    except anthropic.APIError as exc:
        st.error(f"Anthropic API error: {exc}")
        return None
    except Exception as exc:
        st.error(f"Unexpected error during message parsing: {exc}")
        return None


def apply_update(extracted: dict, matched_indices: list) -> list[dict]:
    """
    Apply a single extracted item's update to the given order row indices.

    - Delay message: updates order_date; never sets confirmed.
    - Delivery confirmation: sets confirmed="yes"; date update is optional.
    """
    orders = st.session_state.orders_df
    is_confirmation = bool(extracted.get("delivery_confirmed"))
    new_date = compute_new_date(extracted)

    # A confirmation with no new date still has work to do (set confirmed=yes).
    # A delay with no new date has nothing to write.
    if not is_confirmation and not new_date:
        return []

    records = []
    for idx in matched_indices:
        old_date = str(orders.at[idx, "order_date"])

        if new_date:
            orders.at[idx, "order_date"] = new_date

        if is_confirmation:
            orders.at[idx, "confirmed"] = "yes"

        records.append({
            "customer":       orders.at[idx, "customer"],
            "item":           orders.at[idx, "item"],
            "old_date":       old_date,
            "new_date":       new_date or old_date,
            "is_confirmation": is_confirmation,
            "old_score":      "",
            "new_score":      "",
            "reason":         "",
        })

    st.session_state.orders_df = orders
    return records


def enrich_with_scores(records: list[dict], before_scored: pd.DataFrame | None) -> None:
    """Mutate records in place: add old_score from before_scored, new_score from current scored_df."""
    after_scored = st.session_state.scored_df
    for rec in records:
        cust = rec["customer"]
        if before_scored is not None:
            row = before_scored[before_scored["customer"] == cust]
            if not row.empty:
                rec["old_score"] = row.iloc[0]["score"].upper()
        if after_scored is not None:
            row = after_scored[after_scored["customer"] == cust]
            if not row.empty:
                rec["new_score"] = row.iloc[0]["score"].upper()
                rec["reason"]    = row.iloc[0]["reason"]


def dispatch_extracted_items(items: list[dict]) -> None:
    """
    For each extracted item: find matches and route to confirmation or no-match.
    Nothing is applied automatically — every change requires operator approval.
    """
    orders = st.session_state.orders_df
    events = st.session_state.events_df

    pending_batch: list[dict] = []
    no_match_list: list[dict] = []

    for item in items:
        matched_indices, matched_fields, fail_reason = find_matching_rows(item, orders, events)
        has_any_extracted = any(item.get(f) for f in ("customer", "item", "location", "vendor"))

        if not matched_fields:
            if not has_any_extracted:
                item["_no_match_reason"] = "too_generic"
            elif fail_reason == "location_not_found":
                item["_no_match_reason"] = "location_not_found"
            elif fail_reason == "customer_not_found":
                item["_no_match_reason"] = "customer_not_found"
            else:
                item["_no_match_reason"] = "not_found"
            no_match_list.append(item)
        else:
            pending_batch.append({
                "extracted":       item,
                "matched_indices": matched_indices,
                "matched_fields":  matched_fields,
            })

    # Nothing is applied yet — store everything for operator review
    st.session_state.update_result = {
        "changes":    [],
        "no_matches": no_match_list,
    }
    st.session_state.pending_confirmation = pending_batch if pending_batch else None


# ── Plain-language UI helpers ─────────────────────────────────────────────────

def friendly_match_summary(matched_fields: list[str]) -> str:
    labels = {
        "customer": "customer name",
        "item":     "equipment type",
        "location": "location",
        "vendor":   "vendor name",
    }
    readable = [labels.get(f, f) for f in matched_fields]
    if not readable:
        return "We couldn't find a specific match."
    if len(readable) == 1:
        return (
            f"We found a potential match based on the {readable[0]} mentioned. "
            "Please review and confirm before we make any changes."
        )
    if len(readable) == 2:
        return (
            f"We found a likely match based on the {readable[0]} and {readable[1]} mentioned. "
            "Please review and confirm before we make any changes."
        )
    last   = readable[-1]
    others = ", ".join(readable[:-1])
    return (
        f"We found a strong match based on the {others}, and {last} mentioned. "
        "Please review and confirm before we make any changes."
    )


def friendly_detected_lines(ext: dict) -> list[str]:
    parts = []
    if ext.get("customer"):
        parts.append(f"**Customer:** {ext['customer']}")
    if ext.get("location"):
        parts.append(f"**Location:** {ext['location']}")
    if ext.get("item"):
        parts.append(f"**Equipment:** {ext['item']}")
    if ext.get("vendor"):
        parts.append(f"**Vendor:** {ext['vendor']}")
    if ext.get("delay_days"):
        days = int(ext["delay_days"])
        parts.append(f"**Delay:** {days} day{'s' if days != 1 else ''}")
    if ext.get("new_delivery_date"):
        parts.append(f"**New delivery date:** {ext['new_delivery_date']}")
    return parts


# ── WhatsApp pending-update helpers ───────────────────────────────────────────

def load_pending_update() -> dict | None:
    """Read data/pending_updates.json if a WhatsApp update is awaiting review."""
    if not os.path.exists(PENDING_PATH):
        return None
    try:
        with open(PENDING_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def delete_pending_update() -> None:
    try:
        os.remove(PENDING_PATH)
    except OSError:
        pass


# ── WhatsApp outbound notification (operator decision → original sender) ───────

def _send_whatsapp(to: str, body: str) -> tuple[bool, str]:
    """
    Send a WhatsApp message via Twilio. Credentials are loaded from .env
    (python-dotenv ran at startup). Returns (ok, detail) — never raises.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token  = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")

    if not (account_sid and auth_token and from_number):
        return False, "Twilio credentials are not set in .env."
    if not to:
        return False, "No sender number on file for this update."
    try:
        from twilio.rest import Client
        Client(account_sid, auth_token).messages.create(from_=from_number, to=to, body=body)
        return True, to
    except Exception as exc:  # noqa: BLE001 — a send failure must not break the dashboard
        return False, str(exc)


def notify_sender(pending: dict, action: str) -> tuple[bool, str]:
    """
    Message the original WhatsApp sender that the operator approved/rejected
    their update. `action` is "approved" or "rejected".
    """
    to        = pending.get("from_number")
    language  = pending.get("language", "en")
    customer  = pending.get("customer") or "the customer"
    new_date  = pending.get("new_delivery_date") or "—"

    if action == "approved":
        if language == "es":
            body = (f"✅ Actualización aprobada por el equipo de operaciones de Luminara. "
                    f"La fecha de entrega de {customer} ha sido actualizada al {new_date}.")
        else:
            body = (f"✅ Update approved by the Luminara operations team. "
                    f"{customer}'s delivery date has been updated to {new_date}.")
    else:  # rejected
        if language == "es":
            body = ("❌ Actualización rechazada por el equipo de operaciones. "
                    "No se realizaron cambios.")
        else:
            body = ("❌ Update rejected by the operations team. No changes were made.")

    return _send_whatsapp(to, body)


def render_pending_banner() -> None:
    """
    Prominent orange banner shown at the very top whenever a WhatsApp message is
    waiting. The operator must Approve or Reject — nothing is applied automatically.
    """
    pending = load_pending_update()
    if not pending:
        return

    raw_msg   = pending.get("raw_message", "")
    customer  = pending.get("customer") or "—"
    equipment = pending.get("equipment") or "—"
    new_date  = pending.get("new_delivery_date") or "—"
    vendor    = pending.get("vendor") or "—"

    st.markdown(
        """
        <div style="background-color:#ffedcc;border-left:8px solid #ff8c00;
                    padding:14px 18px;border-radius:8px;margin-bottom:10px;">
          <h3 style="margin:0;color:#7a3e00;">
            📲 New WhatsApp Update — Pending Your Review
          </h3>
          <div style="color:#7a3e00;font-size:0.9rem;">
            Nueva actualización de WhatsApp — Pendiente de su revisión
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        st.markdown("**📨 Message received  (Mensaje recibido):**")
        st.markdown(f"> {raw_msg}")

        st.markdown("**🔎 What we extracted  (Lo que extrajimos):**")
        ec1, ec2, ec3, ec4 = st.columns(4)
        ec1.markdown(f"**Customer**\n\n{customer}")
        ec2.markdown(f"**Equipment**\n\n{equipment}")
        ec3.markdown(f"**New delivery date**\n\n{new_date}")
        ec4.markdown(f"**Vendor**\n\n{vendor}")

        if st.session_state.scored_df is None:
            st.info(
                "Upload your data below to apply this update.  "
                "(Suba sus datos abajo para aplicar esta actualización.)"
            )

        bcol1, bcol2 = st.columns(2)
        approve = bcol1.button(
            "✅ Approve  (Aprobar)", type="primary", use_container_width=True,
            key="approve_pending",
        )
        reject = bcol2.button(
            "❌ Reject  (Rechazar)", use_container_width=True, key="reject_pending",
        )

        if reject:
            ok, detail = notify_sender(pending, "rejected")
            delete_pending_update()
            st.session_state["_pending_flash"] = ("warning", "rejected", ok, detail)
            st.rerun()

        if approve:
            if st.session_state.scored_df is None:
                st.warning(
                    "Please upload your data first, then approve.  "
                    "(Por favor suba sus datos primero, luego apruebe.)"
                )
            else:
                extracted = {
                    "customer":          pending.get("customer"),
                    "item":              pending.get("equipment"),
                    "vendor":            pending.get("vendor"),
                    "new_delivery_date": pending.get("new_delivery_date"),
                    "delivery_confirmed": False,  # WhatsApp updates are delivery-date changes
                }
                before = st.session_state.scored_df.copy()
                matched_indices, _fields, _fail = find_matching_rows(
                    extracted, st.session_state.orders_df, st.session_state.events_df
                )
                if not matched_indices:
                    st.error(
                        f"Couldn't match a purchase order for **{extracted.get('customer') or 'this message'}**. "
                        "Check that your uploaded data matches, then Reject and re-send.\n\n"
                        f"No se pudo encontrar una orden de compra para **{extracted.get('customer') or 'este mensaje'}**."
                    )
                else:
                    records = apply_update(extracted, matched_indices)
                    if records:
                        recompute()
                        enrich_with_scores(records, before)
                        existing = st.session_state.update_result or {"changes": [], "no_matches": []}
                        existing["changes"].extend(records)
                        st.session_state.update_result = existing
                    ok, detail = notify_sender(pending, "approved")
                    delete_pending_update()
                    st.session_state["_pending_flash"] = ("success", "approved", ok, detail)
                    st.rerun()


# ── Upload section (always visible at the top — replace data anytime) ─────────

def render_upload_section() -> None:
    """
    Render the three CSV uploaders. When a NEW set of files is uploaded, replace
    the in-memory dataframes in session_state and rerun so the risk table shows
    the new data. Nothing is written to disk — data lives only in session_state.
    """
    st.subheader("Upload Your Data  (Subir Sus Datos)")
    st.caption(
        "Upload all three CSVs to load the dashboard. Re-upload anytime to replace "
        "the current data.  (Suba los tres CSV para cargar el tablero. Vuelva a "
        "subirlos para reemplazar los datos.)"
    )

    uc1, uc2, uc3 = st.columns(3)
    with uc1:
        schedule_file = st.file_uploader(
            "Install Schedule  (Calendario de Instalaciones)", type="csv", key="schedule_upload"
        )
    with uc2:
        po_file = st.file_uploader(
            "Purchase Orders  (Órdenes de Compra)", type="csv", key="po_upload"
        )
    with uc3:
        vendor_file = st.file_uploader(
            "Vendor List  (Lista de Proveedores)", type="csv", key="vendor_upload"
        )

    st.divider()

    if not (schedule_file and po_file and vendor_file):
        return

    new_hash = hash((
        schedule_file.name, schedule_file.size,
        po_file.name,       po_file.size,
        vendor_file.name,   vendor_file.size,
    ))
    if st.session_state.files_hash == new_hash:
        return  # these exact files are already loaded

    try:
        schedule_file.seek(0); po_file.seek(0); vendor_file.seek(0)
        events_raw  = pd.read_csv(schedule_file)
        orders_raw  = pd.read_csv(po_file)
        vendors_raw = pd.read_csv(vendor_file)
    except Exception as exc:
        st.error(f"Could not read CSV files: {exc}  (No se pudieron leer los archivos CSV.)")
        st.stop()

    errors = schema_errors(events_raw, orders_raw, vendors_raw)
    if errors:
        st.error("\n\n".join(errors) + "\n\nDid you upload the files to the correct slots?")
        st.stop()

    # Replace the current data in session_state and rescore.
    ingest_dataframes(events_raw, orders_raw, vendors_raw)
    st.session_state.files_hash = new_hash
    _persist_to_store()  # capture the new files_hash too (recompute ran before it was set)
    st.rerun()


# ── Background poller (timed fragment — does NOT re-render the page) ───────────

@st.fragment(run_every=5)
def pending_poller() -> None:
    """
    Reruns only itself every 5s. When a new WhatsApp pending_updates.json appears
    and isn't already on screen (and the operator isn't mid-review), trigger ONE
    full app rerun so the banner shows. Because only this fragment reruns on the
    timer, the rest of the page is not re-executed — so the upload section renders
    exactly once.
    """
    new_update_waiting = load_pending_update() is not None
    already_shown      = bool(st.session_state.get("_banner_shown"))
    review_in_progress = bool(st.session_state.get("pending_confirmation"))
    if new_update_waiting and not already_shown and not review_in_progress:
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# 1. HEADER
# ═════════════════════════════════════════════════════════════════════════════

st.title("☀️ Luminara AI")
st.caption(f"Solar Installation Risk Intelligence  •  {date.today().strftime('%B %d, %Y')}")
st.divider()

# ── WhatsApp pending-update banner (checked on every refresh) ─────────────────
_flash = st.session_state.pop("_pending_flash", None)
if _flash:
    _kind, _what, _notified, _detail = _flash
    if _what == "approved":
        st.success("✅ WhatsApp update approved and applied. Risk scores recalculated.  "
                   "(Actualización de WhatsApp aprobada y aplicada.)")
    else:
        st.warning("❌ WhatsApp update rejected and discarded.  "
                   "(Actualización de WhatsApp rechazada y descartada.)")
    if _notified:
        st.caption(f"📲 Sender notified on WhatsApp ({_detail}).")
    else:
        st.caption(f"⚠️ Could not notify the sender on WhatsApp: {_detail}")

render_pending_banner()
# Record whether the banner is currently on screen so the background poller knows
# not to keep triggering reruns while the operator is acting on it.
st.session_state["_banner_shown"] = load_pending_update() is not None

# ── FILE UPLOAD — always visible at the top so data can be replaced anytime ───
render_upload_section()

has_data = st.session_state.scored_df is not None

if has_data:
    _window = upcoming(st.session_state.scored_df, days=30)
    _stats  = summary(_window)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Installs — Next 30 Days", _stats["total"])
    c2.metric("🟢  On Track",  _stats["green"])
    c3.metric("🟡  At Risk",   _stats["yellow"])
    c4.metric("🔴  Critical",  _stats["red"])

    st.divider()

    # ── 2. RISK TABLE ─────────────────────────────────────────────────────────
    st.subheader("Install Risk Overview")
    st.caption("Resumen de Riesgo de Instalaciones — Próximos 30 Días")
    _delay_help = (
        "Probability of delay based on vendor's historical on-time delivery rate. "
        "0% means delivery is confirmed.\n\n"
        "Probabilidad de atraso según el historial de entregas puntuales del "
        "proveedor. 0% significa que la entrega está confirmada."
    )
    st.dataframe(
        style_table(build_display_df(_window), _window),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Customer":          st.column_config.TextColumn("Customer", width="medium"),
            "Location":          st.column_config.TextColumn("Location", width="small"),
            "Install Date":      st.column_config.DateColumn("Install Date", width="small"),
            "Equipment Item":    st.column_config.TextColumn("Equipment Item", width="medium"),
            "Vendor":            st.column_config.TextColumn("Vendor", width="small"),
            "Expected Delivery": st.column_config.DateColumn("Expected Delivery", width="small"),
            "Delivery Confirmed":st.column_config.TextColumn("Delivery Confirmed", width="small"),
            "Risk Score":        st.column_config.TextColumn("Risk Score", width="small"),
            "Delay Risk":        st.column_config.TextColumn("Delay Risk", width="small", help=_delay_help),
            "Reason":            st.column_config.TextColumn("Reason", width="large"),
        },
    )

    st.divider()

    # ── 3. VENDOR SCORECARD ────────────────────────────────────────────────────
    st.subheader("Vendor Scorecard")
    st.caption("Tarjeta de Desempeño de Proveedores — Todas las Instalaciones")
    _vendor_help = (
        "Overall vendor risk rolls up every install tied to the vendor: RED if any "
        "install is red or average delay risk is 50%+, YELLOW if any install is "
        "yellow or average delay risk is 25%+, otherwise GREEN. Covers all installs, "
        "not just the next 30 days.\n\n"
        "El riesgo general del proveedor resume todas sus instalaciones."
    )
    _scorecard = vendor_scorecard(st.session_state.scored_df)
    st.dataframe(
        style_vendor_scorecard(build_vendor_scorecard_df(_scorecard)),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Vendor":            st.column_config.TextColumn("Vendor", width="medium"),
            "Installs Affected": st.column_config.NumberColumn("Installs Affected", width="small"),
            "Unconfirmed":       st.column_config.NumberColumn("Unconfirmed", width="small"),
            "Avg Delay Risk":    st.column_config.TextColumn("Avg Delay Risk", width="small"),
            "Reliability":       st.column_config.TextColumn("Reliability", width="small"),
            "Vendor Risk":       st.column_config.TextColumn("Vendor Risk", width="small", help=_vendor_help),
        },
    )

    st.divider()

    # ── 4. VENDOR MESSAGE PARSER ──────────────────────────────────────────────
    st.subheader("Update Delivery Status via Vendor Message")
    st.caption("Actualizar Estado de Entrega por Mensaje del Proveedor")

    # ── Show update results (auto-applied, persisted through rerun) ───────────
    if st.session_state.update_result is not None:
        res = st.session_state.update_result

        if res.get("changes"):
            n_confirmed = sum(1 for c in res["changes"] if c.get("is_confirmation"))
            n_delayed   = len(res["changes"]) - n_confirmed
            parts = []
            if n_confirmed:
                parts.append(f"{n_confirmed} marked as delivery confirmed")
            if n_delayed:
                parts.append(f"{n_delayed} delivery date(s) updated")
            st.success(f"✅ {', '.join(parts)}.")

            _chg_df = pd.DataFrame(res["changes"])
            _chg_df["Update Type"] = _chg_df["is_confirmation"].apply(
                lambda v: "✅ Delivery confirmed" if v else "📅 Date updated"
            )
            _chg_df = _chg_df[
                ["customer", "item", "Update Type", "old_date", "new_date", "old_score", "new_score", "reason"]
            ].rename(columns={
                "customer":  "Customer",
                "item":      "Equipment Item",
                "old_date":  "Previous Delivery Date",
                "new_date":  "New Delivery Date",
                "old_score": "Previous Risk",
                "new_score": "New Risk",
                "reason":    "Reason",
            })

            def _style_score_cols(df):
                def _cell(val):
                    return f"background-color: {SCORE_BG.get(str(val).upper(), '#ffffff')}"
                return df.style.map(_cell, subset=["Previous Risk", "New Risk"])

            st.dataframe(_style_score_cols(_chg_df), use_container_width=True, hide_index=True)

        if res.get("no_matches"):
            for nm in res["no_matches"]:
                reason = nm.get("_no_match_reason")

                if reason == "too_generic":
                    st.warning(
                        "Your message is too generic to update any records automatically. "
                        "Please include the customer name, location, or specific equipment details "
                        "so we can identify which install is affected. "
                        "**For example:** *The SolarEdge inverters for the Ponce job are delayed 2 weeks.*\n\n"
                        "El mensaje es muy genérico para actualizar registros automáticamente. "
                        "Por favor incluye el nombre del cliente, la ubicación o los detalles específicos "
                        "del equipo para identificar cuál instalación está afectada. "
                        "**Por ejemplo:** *Los inversores de SolarEdge para el trabajo de Ponce se retrasan 2 semanas.*"
                    )

                elif reason == "location_not_found":
                    loc = nm.get("location") or "that location"
                    st.warning(
                        f"No purchase orders were found for **{loc}**. "
                        "Only installs scheduled at that location will be updated when a location is mentioned. "
                        "Check that the location in your message matches a location in your install schedule.\n\n"
                        f"No se encontraron órdenes de compra para **{loc}**. "
                        "Solo se actualizan instalaciones programadas en esa ubicación cuando se menciona una localidad. "
                        "Verifica que la ubicación en tu mensaje coincida con una ubicación en tu calendario de instalaciones."
                    )

                elif reason == "customer_not_found":
                    cust = nm.get("customer") or "that customer"
                    st.warning(
                        f"No purchase orders were found for **{cust}**. "
                        "Check that the customer name in your message matches your purchase order data exactly.\n\n"
                        f"No se encontraron órdenes de compra para **{cust}**. "
                        "Verifica que el nombre del cliente coincida exactamente con tus datos de órdenes de compra."
                    )

                else:
                    detected_lines = friendly_detected_lines(nm)
                    detected_str   = "  •  ".join(detected_lines) if detected_lines else "nothing specific"
                    st.warning(
                        f"We couldn't find a purchase order matching what was in the message. "
                        f"Here's what we detected: {detected_str}. "
                        "Check that the customer name, equipment, or location matches your purchase order data.\n\n"
                        "No encontramos una orden de compra que coincida con el mensaje. "
                        "Verifica que el nombre del cliente, el equipo o la ubicación coincidan con tus datos."
                    )

        st.session_state.update_result = None  # clear after showing once

    # ── Confirmation section (always shown — every change requires approval) ────
    if st.session_state.pending_confirmation:
        st.subheader("📋 Review & Approve Updates  (Revisar y Aprobar Actualizaciones)")

        confirm_selections: dict[int, bool] = {}
        for i, candidate in enumerate(st.session_state.pending_confirmation):
            ext      = candidate["extracted"]
            new_date = compute_new_date(ext)

            detected_lines = friendly_detected_lines(ext)
            match_sentence = friendly_match_summary(candidate["matched_fields"])

            is_confirmation = bool(ext.get("delivery_confirmed"))

            with st.container(border=True):
                # ── Message type banner ───────────────────────────────────────
                if is_confirmation:
                    st.success(
                        "**We detected this as a delivery confirmation.** "
                        "If approved, this record will be marked as delivery confirmed "
                        "and the risk score will update to GREEN.\n\n"
                        "*Detectamos esto como una confirmación de entrega. "
                        "Si aprueba, este registro se marcará como entrega confirmada "
                        "y la puntuación de riesgo se actualizará a VERDE.*"
                    )
                else:
                    st.warning(
                        "**We detected this as a delivery delay.** "
                        "If approved, the expected delivery date will be updated.\n\n"
                        "*Detectamos esto como un retraso en la entrega. "
                        "Si aprueba, se actualizará la fecha de entrega esperada.*"
                    )

                st.markdown(match_sentence)

                if detected_lines:
                    st.markdown("**From your message we identified:**  " + "  •  ".join(detected_lines))

                if ext.get("notes"):
                    st.caption(f"ℹ️ {ext['notes']}")

                if new_date and not is_confirmation:
                    try:
                        from datetime import date as _date
                        _fmt = _date.fromisoformat(new_date).strftime("%B %d, %Y")
                    except ValueError:
                        _fmt = new_date
                    st.markdown(f"**New expected delivery date:** {_fmt}")

                if candidate["matched_indices"]:
                    st.markdown("**Matching purchase orders — select the record this message refers to:**")
                    _m = st.session_state.orders_df.loc[candidate["matched_indices"]].copy()
                    _m = _m[["customer", "item", "vendor", "order_date"]].rename(columns={
                        "customer":   "Customer",
                        "item":       "Equipment Item",
                        "vendor":     "Vendor",
                        "order_date": "Current Delivery Date",
                    })
                    st.dataframe(_m, use_container_width=True, hide_index=True)
                    confirm_selections[i] = st.checkbox(
                        "Yes, update this record  (Sí, actualizar este registro)",
                        value=bool(new_date),
                        key=f"confirm_chk_{i}",
                    )
                else:
                    st.info("We couldn't find a matching purchase order for this item.")
                    confirm_selections[i] = False

        btn_col1, btn_col2 = st.columns(2)
        if btn_col1.button("✅ Apply Selected  (Aplicar Seleccionados)", type="primary"):
            before = st.session_state.scored_df.copy() if st.session_state.scored_df is not None else None
            confirmed_records: list[dict] = []
            for i, candidate in enumerate(st.session_state.pending_confirmation):
                if confirm_selections.get(i) and candidate["matched_indices"]:
                    confirmed_records.extend(
                        apply_update(candidate["extracted"], candidate["matched_indices"])
                    )
            if confirmed_records:
                recompute()
                enrich_with_scores(confirmed_records, before)

            existing = st.session_state.update_result or {"changes": [], "no_matches": []}
            existing["changes"].extend(confirmed_records)
            st.session_state.update_result = existing
            st.session_state.pending_confirmation = None
            st.rerun()

        if btn_col2.button("⏭️ Skip All  (Omitir Todos)"):
            st.session_state.pending_confirmation = None
            st.rerun()

    # ── Message input ─────────────────────────────────────────────────────────
    vendor_msg = st.text_area(
        "Paste a vendor message to update delivery status  "
        "(Pegue un mensaje del proveedor para actualizar el estado de entrega)",
        height=120,
        placeholder=(
            "e.g.  Hi! The 400W solar panels for Rivera Solar are delayed by 3 days. "
            "New expected delivery: June 17.\n\n"
            "También puede pegar mensajes en español."
        ),
    )

    if st.button("Parse & Update  (Analizar y Actualizar)", type="primary"):
        if not vendor_msg.strip():
            st.warning(
                "Please paste a vendor message first.  "
                "(Por favor pegue un mensaje del proveedor.)"
            )
        else:
            with st.spinner("Luminara AI is reading the message... (Luminara AI está leyendo el mensaje...)"):
                _items = parse_vendor_message(vendor_msg)
            if _items is not None:
                dispatch_extracted_items(_items)
                st.rerun()

    st.divider()

    # ── 5. DOWNLOAD ───────────────────────────────────────────────────────────
    _export = build_display_df(upcoming(st.session_state.scored_df, days=30))
    st.download_button(
        label="⬇️  Download Risk Report  (Descargar Reporte de Riesgo)",
        # utf-8-sig (BOM) so Excel renders the ✓ glyph and accented names
        # (e.g. "Energía") correctly instead of mojibake.
        data=_export.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"luminara_risk_report_{date.today().isoformat()}.csv",
        mime="text/csv",
    )
    st.divider()

else:
    st.info(
        "👋 **Welcome to Luminara AI!**\n\n"
        "Upload your three CSV files above to see your install risk dashboard.\n\n"
        "---\n\n"
        "**¡Bienvenido a Luminara AI!**\n\n"
        "Suba sus tres archivos CSV arriba para ver el tablero de riesgos de instalaciones."
    )
    st.divider()


# ── AUTO-REFRESH — poll for new WhatsApp pending updates ──────────────────────
# Registered last. The fragment reruns ONLY itself every few seconds (the rest of
# the page, including the upload section, is NOT re-executed — which is what fixes
# the duplicate-render bug). When a new pending_updates.json appears and isn't
# already on screen, it triggers a single full rerun so the banner shows.
pending_poller()
