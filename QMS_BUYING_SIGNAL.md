# CDSCO Scraper Classifications & QMS Buying Signals

## The classifications (LLM output per record)

| Field | Meaning | QMS buying signal |
|---|---|---|
| `is_paper_failure` | Root cause points to paper-based QMS (missing signatures, manual batch records, uncontrolled spreadsheets, ALCOA+ issues) | **The core signal.** This is literally the pain your product solves — an eQMS replaces exactly what failed. A `true` here is a direct qualification: "this company needs electronic QMS." |
| `violates_rule_96` | Failure tied to API QR/serialization mandate (2026) | **Regulatory-forced buyer.** The QR/serialization mandate can't be met with paper. A failing company is under a legal deadline to go digital — high urgency, short sales cycle. |
| `violates_sub_rule_7` | FDF excipient-control compliance failure (2026) | Signals weak excipient/vendor controls → opens the door for QMS modules around supplier & material management. |
| `violates_schedule_h2` | Antimicrobial track-and-trace failure (2026) | Track-and-trace requires automated data capture — a paper shop can't comply. Same forced-digitalization urgency as Rule 96. |
| `root_cause_summary` | One-line explanation of the failure | **SDR personalization.** Lets outreach reference the specific failure ("your Telmisartan batch failed dissolution last quarter") — credible, specific, speaks to QA/Compliance. |
| `evidence_quote` | Exact CDSCO text backing the flags | The verifiable hook for email/call — shows you did the research, not spam. |
| `entity_name` / manufacturer | Account identity | Routing + entity resolution + repeat-offender tracking. |

## How the score makes it a *signal*

- **Paper failure (+30)** — biggest single flag: direct ICP fit.
- **2026 rule (+20, year-gated)** — only counts for 2026+ events. A Rule-96 failure in 2026 = an account **dealing with the mandate right now** = peak intent. A 2019 "violation" is ignored because the rule didn't exist — keeps history honest while still feeding the pattern below.
- **Repeat offender (+10 each, +30 cap)** — a firm failing in 2019 *and* 2026 has chronic QMS gaps. That's a warmer lead than a one-off: the pain is systemic, the budget conversation is easier.
- **Recency weight (1.0 → 0.6)** — queues fresh, actionable accounts first; old failures don't waste SDR calls.

## The funnel

CDSCO notices → LLM flags paper/rule failures → score ranks by (severity × freshness) + (recidivism) → SDR picks the top accounts where the failure text gives a ready-made opening line for selling an electronic QMS.

In short: `is_paper_failure` and the two serialization mandates (Rule 96 / Schedule H2) are the strongest buying signals — they're failures only an eQMS fixes, under legal deadline. The rest of the fields exist to make the outreach specific and the queue prioritized.
