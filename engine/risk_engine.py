"""
Industry-agnostic risk scoring engine.

Column name contract (caller must rename before passing DataFrames):
  events     : customer, event_date
  orders     : customer, item, vendor, order_date, confirmed ("yes"/"no")
  vendors    : vendor_name, reliability_rating
"""

from __future__ import annotations

import pandas as pd
from datetime import date, timedelta


# ── Thresholds ────────────────────────────────────────────────────────────────

YELLOW_DAYS = 14            # confirmed required if install within this many days
RED_DAYS = 7               # unconfirmed + within this many days → red
DELAY_PROB_THRESHOLD = 25.0  # percent; a delay probability >= this is "elevated"
DEFAULT_RELIABILITY = 0.80   # used when a vendor has no reliability_rating on file


# ── Level 1 Predictive Intelligence ────────────────────────────────────────────

def compute_delay_probability(confirmed: bool, reliability_rating: float | None) -> float:
    """
    Predicted probability (0–100) that a delivery slips.

    Confirmed deliveries carry no delay risk. For unconfirmed deliveries the
    probability is the vendor's historical miss rate: (1 - reliability_rating).
    A missing reliability_rating falls back to DEFAULT_RELIABILITY.
    """
    if confirmed:
        return 0.0
    rating = DEFAULT_RELIABILITY if reliability_rating is None else reliability_rating
    return round((1.0 - rating) * 100.0, 1)


# ── Core scoring ─────────────────────────────────────────────────────────────

def score_event(
    event_date: date,
    confirmed: bool,
    expected_order_date: date,
    today: date,
    reliability_rating: float | None = None,
) -> tuple[str, str, float]:
    """
    Return (score, reason, delay_probability) for a single event/order pair.

    score             : "green" | "yellow" | "red"
    reason            : human-readable explanation
    delay_probability : predicted % chance the delivery slips (0–100)

    Scoring factors in both days-until-event and the vendor's delay probability:
      - confirmed                                   → green
      - unconfirmed, >14d out, low delay risk       → green
      - unconfirmed, >14d out, elevated delay risk  → yellow
      - unconfirmed, 7–14d out, low delay risk      → yellow
      - unconfirmed, 7–14d out, elevated delay risk → red
      - unconfirmed, <7d out                        → red (regardless of vendor)
      - expected delivery date already passed       → red
    """
    days_until = (event_date - today).days
    delay_prob = compute_delay_probability(confirmed, reliability_rating)
    elevated = delay_prob >= DELAY_PROB_THRESHOLD

    if confirmed:
        return "green", "Delivery confirmed", delay_prob

    if expected_order_date < today:
        return "red", f"Expected delivery {expected_order_date} has passed, not confirmed", delay_prob

    if days_until < RED_DAYS:
        return "red", f"Install in {days_until}d, delivery not confirmed", delay_prob

    if days_until < YELLOW_DAYS:
        # 7–14 days out: low delay risk holds at yellow, elevated risk escalates to red
        if elevated:
            return "red", f"Install in {days_until}d, unconfirmed with elevated delay risk", delay_prob
        return "yellow", f"Install in {days_until}d, delivery not confirmed", delay_prob

    # More than 14 days out: low delay risk stays green, elevated risk warns yellow
    if elevated:
        return "yellow", f"Install in {days_until}d, unconfirmed with elevated delay risk", delay_prob
    return "green", f"Install in {days_until}d, delivery not yet required", delay_prob


def score_all(
    events: pd.DataFrame,
    orders: pd.DataFrame,
    vendors: pd.DataFrame,
    today: date | None = None,
) -> pd.DataFrame:
    """
    Join events + orders + vendors, score every row, return enriched DataFrame.

    Required columns:
      events  : customer, event_date (datetime or date)
      orders  : customer, item, vendor, order_date (datetime or date), confirmed
      vendors : vendor_name, reliability_rating

    Returns DataFrame with original columns plus:
      score, reason, delay_probability, reliability_estimated, days_until_event
    """
    if today is None:
        today = date.today()

    # Normalise date columns to Python date
    events = events.copy()
    orders = orders.copy()
    events["event_date"] = pd.to_datetime(events["event_date"]).dt.date
    orders["order_date"] = pd.to_datetime(orders["order_date"]).dt.date

    # Normalise confirmed to bool
    orders["confirmed"] = orders["confirmed"].str.strip().str.lower() == "yes"

    merged = events.merge(orders, on="customer", how="left")
    merged = merged.merge(
        vendors.rename(columns={"vendor_name": "vendor"}),
        on="vendor",
        how="left",
    )

    has_rating_col = "reliability_rating" in merged.columns

    scores, reasons, delay_probs, estimateds = [], [], [], []
    for _, row in merged.iterrows():
        if pd.isna(row.get("order_date")) or pd.isna(row.get("event_date")):
            scores.append("red")
            reasons.append("Missing order or event date")
            delay_probs.append(None)
            estimateds.append(False)
            continue

        rating_raw = row.get("reliability_rating") if has_rating_col else None
        estimated = pd.isna(rating_raw)
        rating = DEFAULT_RELIABILITY if estimated else float(rating_raw)

        s, r, p = score_event(
            event_date=row["event_date"],
            confirmed=bool(row["confirmed"]),
            expected_order_date=row["order_date"],
            today=today,
            reliability_rating=rating,
        )
        scores.append(s)
        reasons.append(r)
        delay_probs.append(p)
        estimateds.append(bool(estimated))

    merged["score"] = scores
    merged["reason"] = reasons
    merged["delay_probability"] = delay_probs
    merged["reliability_estimated"] = estimateds
    merged["days_until_event"] = merged["event_date"].apply(
        lambda d: (d - today).days if pd.notna(d) else None
    )

    return merged


def summary(scored: pd.DataFrame) -> dict[str, int]:
    """Return {total, green, yellow, red} counts."""
    counts = scored["score"].value_counts().to_dict()
    return {
        "total": len(scored),
        "green": counts.get("green", 0),
        "yellow": counts.get("yellow", 0),
        "red": counts.get("red", 0),
    }


def upcoming(scored: pd.DataFrame, days: int = 30, today: date | None = None) -> pd.DataFrame:
    """Filter to events within the next `days` days."""
    if today is None:
        today = date.today()
    cutoff = today + timedelta(days=days)
    return scored[
        (scored["event_date"] >= today) & (scored["event_date"] <= cutoff)
    ].sort_values("event_date")
