"""
Quick smoke test for risk_engine against the fake dataset (currently 15 rows).
Run: python test_engine.py
"""

import sys
import os
from datetime import date, timedelta
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from engine.risk_engine import (
    score_all,
    summary,
    upcoming,
    score_event,
    compute_delay_probability,
    vendor_scorecard,
    vendor_level,
    DEFAULT_RELIABILITY,
)

TODAY = date(2026, 6, 10)   # pin today so results are deterministic

events = pd.read_csv("data/install_schedule.csv")
orders = pd.read_csv("data/purchase_orders.csv")
vendors = pd.read_csv("data/vendor_list.csv")

# Map solar column names to engine names
events = events.rename(columns={"install_date": "event_date"})
orders = orders.rename(columns={
    "equipment_item": "item",
    "expected_delivery_date": "order_date",
    "delivery_confirmed": "confirmed",
})

scored = score_all(events, orders, vendors, today=TODAY)
stats = summary(scored)
window = upcoming(scored, days=30, today=TODAY)

print(f"\n=== Luminara Risk Engine — test run (today={TODAY}) ===\n")
print(f"Total installs scored : {stats['total']}")
print(f"  Green               : {stats['green']}")
print(f"  Yellow              : {stats['yellow']}")
print(f"  Red                 : {stats['red']}")

print("\n--- Upcoming installs (next 30 days) ---\n")
display_cols = ["customer", "location", "event_date", "item", "vendor",
                "order_date", "days_until_event", "score", "reason"]
print(window[display_cols].to_string(index=False))

# Basic assertions
assert stats["total"] == len(events), f"Expected {len(events)} scored rows, got {stats['total']}"
assert stats["red"] > 0, "Expected at least one red"
assert stats["yellow"] > 0, "Expected at least one yellow"
assert stats["green"] > 0, "Expected at least one green"

print("\nAll assertions passed.")


# ══════════════════════════════════════════════════════════════════════════════
# Level 1 Predictive Intelligence — delay_probability + reliability-aware scoring
# ══════════════════════════════════════════════════════════════════════════════

print("\n=== Level 1 Predictive Intelligence tests ===\n")

T = date(2026, 6, 10)


def days_out(n: int) -> date:
    """An event date n days after the pinned today."""
    return T + timedelta(days=n)


passed = 0


def check(label, got, expected):
    global passed
    assert got == expected, f"FAIL [{label}]: expected {expected}, got {got}"
    passed += 1
    print(f"  ✓ {label}")


# 1. Confirmed delivery → always GREEN, delay_probability = 0%
score, _reason, prob = score_event(
    event_date=days_out(2), confirmed=True, expected_order_date=days_out(1),
    today=T, reliability_rating=0.30,  # terrible vendor, but confirmed wins
)
check("confirmed → GREEN", score, "green")
check("confirmed → 0% delay probability", prob, 0.0)

# Example from the spec: 0.82 reliability, unconfirmed → 18% delay probability
check("0.82 reliability unconfirmed → 18%",
      compute_delay_probability(False, 0.82), 18.0)

# 2. Unconfirmed, reliable vendor (>= 0.80), > 14 days out → GREEN
score, _r, prob = score_event(
    event_date=days_out(20), confirmed=False, expected_order_date=days_out(18),
    today=T, reliability_rating=0.90,
)
check("unconfirmed, reliable, >14d → GREEN", score, "green")
check("  ↳ delay_probability = 10%", prob, 10.0)

# 3. Unconfirmed, unreliable vendor (< 0.75), > 14 days out → YELLOW
score, _r, prob = score_event(
    event_date=days_out(20), confirmed=False, expected_order_date=days_out(18),
    today=T, reliability_rating=0.70,
)
check("unconfirmed, unreliable, >14d → YELLOW", score, "yellow")
check("  ↳ delay_probability = 30%", prob, 30.0)

# 4. Unconfirmed, reliable vendor, 7–14 days out → YELLOW
score, _r, _p = score_event(
    event_date=days_out(10), confirmed=False, expected_order_date=days_out(9),
    today=T, reliability_rating=0.90,
)
check("unconfirmed, reliable, 7–14d → YELLOW", score, "yellow")

# 5. Unconfirmed, unreliable vendor, 7–14 days out → RED
score, _r, _p = score_event(
    event_date=days_out(10), confirmed=False, expected_order_date=days_out(9),
    today=T, reliability_rating=0.70,
)
check("unconfirmed, unreliable, 7–14d → RED", score, "red")

# 6. Unconfirmed, any vendor, < 7 days out → RED (even a near-perfect vendor)
score, _r, _p = score_event(
    event_date=days_out(3), confirmed=False, expected_order_date=days_out(2),
    today=T, reliability_rating=0.99,
)
check("unconfirmed, <7d → RED regardless of vendor", score, "red")

# 7. Delivery date passed, not confirmed → RED
score, _r, _p = score_event(
    event_date=days_out(20), confirmed=False, expected_order_date=days_out(-1),
    today=T, reliability_rating=0.99,
)
check("delivery date passed, unconfirmed → RED", score, "red")

# 7b. Exact 25% threshold boundary — VoltCore (0.75) is the demo's edge case.
#     0.75 → 25.0% delay risk, and 25.0 >= DELAY_PROB_THRESHOLD must escalate.
check("0.75 reliability → exactly 25%",
      compute_delay_probability(False, 0.75), 25.0)
score, _r, _p = score_event(
    event_date=days_out(20), confirmed=False, expected_order_date=days_out(18),
    today=T, reliability_rating=0.75,
)
check("0.75 vendor, >14d → YELLOW (25% is elevated)", score, "yellow")
score, _r, _p = score_event(
    event_date=days_out(10), confirmed=False, expected_order_date=days_out(9),
    today=T, reliability_rating=0.75,
)
check("0.75 vendor, 7–14d → RED (25% is elevated)", score, "red")

# 8. Missing reliability_rating → uses 0.80 default, marked estimated
#    Exercised through score_all so the merge + estimation path is covered.
ev = pd.DataFrame([{"customer": "Acme", "location": "Ponce", "event_date": days_out(20)}])
od = pd.DataFrame([{
    "customer": "Acme", "item": "Inverter", "vendor": "GhostVendor",
    "order_date": days_out(18), "confirmed": "no",
}])
vn = pd.DataFrame([{"vendor_name": "OtherVendor", "reliability_rating": 0.95}])  # no GhostVendor
scored_missing = score_all(ev, od, vn, today=T)
row = scored_missing.iloc[0]
check("missing rating → estimated flag set", bool(row["reliability_estimated"]), True)
check("missing rating → uses 0.80 default (20% delay)",
      row["delay_probability"], round((1 - DEFAULT_RELIABILITY) * 100, 1))
# 0.80 default → 20% delay (< 25% threshold), >14d out → GREEN
check("missing rating, >14d → GREEN", row["score"], "green")

# 9. Missing order/event date (left-join miss) → RED, delay_probability = None.
#    Both format_delay_risk and delay_risk_color in app.py guard on this.
ev2 = pd.DataFrame([{"customer": "Orphan", "location": "Ponce", "event_date": days_out(20)}])
od2 = pd.DataFrame([{
    "customer": "Nobody", "item": "Inverter", "vendor": "SunTech PR",
    "order_date": days_out(18), "confirmed": "no",
}])  # no order joins to "Orphan" → order_date is NaT after the left join
vn2 = pd.DataFrame([{"vendor_name": "SunTech PR", "reliability_rating": 0.82}])
scored_orphan = score_all(ev2, od2, vn2, today=T)
orphan = scored_orphan.iloc[0]
check("missing order date → RED", orphan["score"], "red")
check("missing order date → reason", orphan["reason"], "Missing order or event date")
check("missing order date → delay_probability is None", orphan["delay_probability"] is None, True)

print(f"\nAll {passed} predictive-intelligence assertions passed.")


# ══════════════════════════════════════════════════════════════════════════════
# Vendor Scorecard — per-vendor aggregation over score_all() output
# ══════════════════════════════════════════════════════════════════════════════

print("\n=== Vendor Scorecard tests ===\n")

sc_passed = 0


def sc_check(label, got, expected):
    global sc_passed
    assert got == expected, f"FAIL [{label}]: expected {expected!r}, got {got!r}"
    sc_passed += 1
    print(f"  ✓ {label}")


def build_scored(events_rows, orders_rows, vendors_rows):
    return score_all(
        pd.DataFrame(events_rows), pd.DataFrame(orders_rows),
        pd.DataFrame(vendors_rows), today=T,
    )


# --- vendor_level() rule (RED=any red or avg>=50; YELLOW=any yellow or avg>=25) ---
sc_check("vendor_level: any red → red", vendor_level(True, False, 0.0), "red")
sc_check("vendor_level: avg>=50 → red", vendor_level(False, False, 50.0), "red")
sc_check("vendor_level: any yellow → yellow", vendor_level(False, True, 0.0), "yellow")
sc_check("vendor_level: avg>=25 → yellow", vendor_level(False, False, 25.0), "yellow")
sc_check("vendor_level: clean → green", vendor_level(False, False, 10.0), "green")
sc_check("vendor_level: None avg, no flags → green", vendor_level(False, False, None), "green")

# --- Distinct installs, NOT raw join rows: one job, 3 items, same vendor ---
multi = build_scored(
    events_rows=[{"customer": "BigJob", "location": "Ponce", "event_date": days_out(20)}],
    orders_rows=[
        {"customer": "BigJob", "item": "Panels", "vendor": "Acme",
         "order_date": days_out(18), "confirmed": "no"},
        {"customer": "BigJob", "item": "Inverter", "vendor": "Acme",
         "order_date": days_out(18), "confirmed": "no"},
        {"customer": "BigJob", "item": "Battery", "vendor": "Acme",
         "order_date": days_out(18), "confirmed": "no"},
    ],
    vendors_rows=[{"vendor_name": "Acme", "reliability_rating": 0.90}],
)
card = vendor_scorecard(multi)
acme = card[card["vendor"] == "Acme"].iloc[0]
sc_check("multi-item job → installs_affected counts 1 distinct install",
         int(acme["installs_affected"]), 1)
sc_check("multi-item job → unconfirmed_count counts all 3 rows",
         int(acme["unconfirmed_count"]), 3)

# --- All-confirmed vendor → avg 0%, GREEN ---
allconf = build_scored(
    events_rows=[{"customer": "C1", "location": "X", "event_date": days_out(20)}],
    orders_rows=[{"customer": "C1", "item": "Panels", "vendor": "Steady",
                  "order_date": days_out(18), "confirmed": "yes"}],
    vendors_rows=[{"vendor_name": "Steady", "reliability_rating": 0.70}],
)
steady = vendor_scorecard(allconf).iloc[0]
sc_check("all-confirmed vendor → avg delay 0%", steady["avg_delay_probability"], 0.0)
sc_check("all-confirmed vendor → GREEN", steady["vendor_level"], "green")

# --- Shaky unconfirmed vendor (0.40 reliability → 60% delay) → RED via avg ---
shaky = build_scored(
    events_rows=[{"customer": "C2", "location": "X", "event_date": days_out(20)}],
    orders_rows=[{"customer": "C2", "item": "Panels", "vendor": "Risky",
                  "order_date": days_out(18), "confirmed": "no"}],
    vendors_rows=[{"vendor_name": "Risky", "reliability_rating": 0.40}],
)
risky = vendor_scorecard(shaky).iloc[0]
sc_check("shaky vendor → avg delay 60%", risky["avg_delay_probability"], 60.0)
sc_check("shaky vendor → RED", risky["vendor_level"], "red")

# --- Missing-date-only vendor → avg None, no crash, level rests on score ---
nodate = build_scored(
    events_rows=[{"customer": "Orphan", "location": "X", "event_date": days_out(20)}],
    orders_rows=[{"customer": "Nobody", "item": "Panels", "vendor": "GhostV",
                  "order_date": days_out(18), "confirmed": "no"}],
    vendors_rows=[{"vendor_name": "GhostV", "reliability_rating": 0.50}],
)
# "Orphan" has no order → its row has no vendor and is dropped; assert no crash
card_nodate = vendor_scorecard(nodate)
sc_check("missing-date scorecard does not crash", isinstance(card_nodate, pd.DataFrame), True)

# --- Estimated-rating vendor flagged (vendor absent from vendor_list) ---
est = build_scored(
    events_rows=[{"customer": "C3", "location": "X", "event_date": days_out(20)}],
    orders_rows=[{"customer": "C3", "item": "Panels", "vendor": "Unrated",
                  "order_date": days_out(18), "confirmed": "no"}],
    vendors_rows=[{"vendor_name": "SomeoneElse", "reliability_rating": 0.95}],
)
unrated = vendor_scorecard(est).iloc[0]
sc_check("unrated vendor → reliability_estimated True",
         bool(unrated["reliability_estimated"]), True)
sc_check("unrated vendor → rating defaulted to 0.80",
         unrated["reliability_rating"], DEFAULT_RELIABILITY)

# --- YELLOW via the avg-delay band (25 <= avg < 50) with NO red/yellow installs ---
#     End-to-end through vendor_scorecard(): the yellow must come from the avg
#     clause alone, not from an any_yellow install. Hand-build a scored_df whose
#     installs are individually GREEN but whose delay probabilities average 35%.
avg_band = pd.DataFrame([
    {"vendor": "AvgBand", "customer": "AB-1", "event_date": days_out(20),
     "confirmed": False, "score": "green", "delay_probability": 30.0,
     "reliability_rating": 0.65, "reliability_estimated": False},
    {"vendor": "AvgBand", "customer": "AB-2", "event_date": days_out(25),
     "confirmed": False, "score": "green", "delay_probability": 40.0,
     "reliability_rating": 0.65, "reliability_estimated": False},
])
avg_row = vendor_scorecard(avg_band).iloc[0]
sc_check("avg-band: no red and no yellow installs",
         bool((avg_band["score"].isin(["red", "yellow"])).any()), False)
sc_check("avg-band: avg_delay_probability is 35%", avg_row["avg_delay_probability"], 35.0)
sc_check("avg-band: vendor_scorecard → YELLOW from avg alone", avg_row["vendor_level"], "yellow")

print(f"\nAll {sc_passed} vendor-scorecard assertions passed.")
