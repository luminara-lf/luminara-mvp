# Luminara MVP

Solar installation operations intelligence tool for solar operators in the United States, with Puerto Rico as the first pilot market.

## Purpose

Reads a solar operator's install schedule and purchase orders, scores each upcoming install as green/yellow/red based on equipment delivery status and days until install, and flags at-risk installs before it's too late to act.

## Tech Stack

- Python 3.11
- pandas
- Streamlit
- Anthropic API — model: `claude-sonnet-4-6`
- python-dotenv

## Project Structure

```
luminara-mvp/
├── engine/
│   └── risk_engine.py       # Standalone, industry-agnostic scoring engine
├── app.py                   # Streamlit interface (solar-specific language)
├── data/
│   ├── install_schedule.csv
│   ├── purchase_orders.csv
│   └── vendor_list.csv
├── .env                     # ANTHROPIC_API_KEY — never commit this file
├── .gitignore
├── requirements.txt
└── CLAUDE.md
```

## Architecture Constraint — Generic Engine, Industry-Specific UI

The risk scoring engine (`engine/risk_engine.py`) must stay industry-agnostic so it can be reused with different input schemas without rewriting. Use generic variable names in the engine:

| Engine name      | Solar UI label       |
|------------------|----------------------|
| `event_date`     | `install_date`       |
| `order_date`     | `purchase_date`      |
| `item`           | `equipment_item`     |
| `confirmed`      | `delivery_confirmed` |

The Streamlit interface (`app.py`) maps solar-specific column names to engine names before calling the engine, and maps them back for display.

## Input Data — CSV Schemas

**install_schedule.csv**
| Column       | Type   | Notes                    |
|--------------|--------|--------------------------|
| customer     | string |                          |
| location     | string |                          |
| install_date | date   | YYYY-MM-DD               |

**purchase_orders.csv**
| Column                  | Type   | Notes                              |
|-------------------------|--------|------------------------------------|
| customer                | string | joins to install_schedule.customer |
| equipment_item          | string |                                    |
| vendor                  | string | joins to vendor_list.vendor_name   |
| expected_delivery_date  | date   | YYYY-MM-DD                         |
| delivery_confirmed      | string | "yes" or "no"                      |

**vendor_list.csv**
| Column           | Type   | Notes               |
|------------------|--------|---------------------|
| vendor_name      | string |                     |
| reliability_rating | float | 0.0–1.0 scale       |

## Risk Scoring Logic (Rules-Based — No ML)

As of Level 1 Predictive Intelligence, scoring factors in **vendor delay probability**
alongside days-until-install. `delay_probability = 0%` when delivery is confirmed,
otherwise `(1 - reliability_rating) × 100`. A delay probability `>= 25%`
(`DELAY_PROB_THRESHOLD`) is "elevated" and escalates the score one level within the
day-based bands. Missing `reliability_rating` falls back to `0.80` (`DEFAULT_RELIABILITY`).

| Score  | Condition                                                                                          |
|--------|----------------------------------------------------------------------------------------------------|
| Green  | delivery_confirmed = "yes"  OR  (unconfirmed AND install > 14 days away AND delay_probability < 25%) |
| Yellow | (unconfirmed AND install > 14 days away AND delay_probability >= 25%)  OR  (unconfirmed AND install 7–14 days away AND delay_probability < 25%) |
| Red    | (unconfirmed AND install 7–14 days away AND delay_probability >= 25%)  OR  (unconfirmed AND install under 7 days away)  OR  expected_delivery_date has passed |

Version 1 remains purely rules-based and deterministic. No machine learning — the
delay probability is a direct arithmetic function of the vendor's reliability rating.

## AI Natural Language Feature

A text input in the Streamlit UI accepts a plain-text vendor message (e.g., pasted WhatsApp message). The Anthropic API (`claude-sonnet-4-6`) parses the message and extracts:

- Which equipment item is referenced
- Which vendor sent the message
- New expected delivery date or delay duration

The app then updates the relevant purchase order record(s) in memory and recalculates all affected risk scores. The structured extraction prompt must request JSON output.

## Streamlit Dashboard — Four Elements

1. **Weekly summary header** — total installs, count of green / yellow / red
2. **Color-coded risk table** — every install in the next 30 days with risk score and reason string
3. **Natural language input** — vendor message text box → parse → update POs → recalculate
4. **Download button** — export risk report as CSV

## Internationalization

All interface text defaults to English. Add a Spanish translation option (toggle or sidebar selector) to support Puerto Rico operators. Store translation strings in a dict or simple i18n module — do not duplicate UI code.

## Environment Variables

```
ANTHROPIC_API_KEY=sk-...
```

Loaded via `python-dotenv`. Never hardcode. Never commit `.env`.

## Deployment

Streamlit Cloud, connected to the Luminara GitHub account. The `requirements.txt` must pin all dependencies.

## Constraints

- Works on MacBook Air 2018 running macOS Sonoma — avoid heavy dependencies
- Interface must be usable by a non-technical solar operator without training
- Keep dashboard to a single scrollable page
- No ML in version 1 — all scoring is deterministic rules

## Development Order

1. `engine/risk_engine.py` — standalone scoring engine, tested with 20 rows of fake data
2. `app.py` — Streamlit interface wired to the engine
3. AI parsing feature
4. Spanish translation layer
5. Streamlit Cloud deployment
