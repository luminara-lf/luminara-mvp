"""
Quick smoke test for risk_engine against the 20-row fake dataset.
Run: python test_engine.py
"""

import sys
import os
from datetime import date
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from engine.risk_engine import score_all, summary, upcoming

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
assert stats["total"] == 20, "Expected 20 scored rows"
assert stats["red"] > 0, "Expected at least one red"
assert stats["yellow"] > 0, "Expected at least one yellow"
assert stats["green"] > 0, "Expected at least one green"

print("\nAll assertions passed.")
