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

YELLOW_DAYS = 14   # confirmed required if install within this many days
RED_DAYS = 7       # unconfirmed + within this many days → red


# ── Core scoring ─────────────────────────────────────────────────────────────

def score_event(event_date: date, confirmed: bool, expected_order_date: date, today: date) -> tuple[str, str]:
    """
    Return (score, reason) for a single event/order pair.

    score  : "green" | "yellow" | "red"
    reason : human-readable explanation
    """
    days_until = (event_date - today).days

    if confirmed:
        return "green", "Delivery confirmed"

    if expected_order_date < today:
        return "red", f"Expected delivery {expected_order_date} has passed, not confirmed"

    if days_until < RED_DAYS:
        return "red", f"Install in {days_until}d, delivery not confirmed"

    if days_until < YELLOW_DAYS:
        return "yellow", f"Install in {days_until}d, delivery not confirmed"

    return "green", f"Install in {days_until}d, delivery not yet required"


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

    Returns DataFrame with original columns plus: score, reason, days_until_event
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

    scores, reasons = [], []
    for _, row in merged.iterrows():
        if pd.isna(row.get("order_date")) or pd.isna(row.get("event_date")):
            scores.append("red")
            reasons.append("Missing order or event date")
            continue

        s, r = score_event(
            event_date=row["event_date"],
            confirmed=bool(row["confirmed"]),
            expected_order_date=row["order_date"],
            today=today,
        )
        scores.append(s)
        reasons.append(r)

    merged["score"] = scores
    merged["reason"] = reasons
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
