# AIVOA Sentinel — Level 2: QMS Buying-Signal Engine

**PRD · Project Level 2** (separate project from **Level 1 — CDSCO Scraper**)

- **Status:** Draft
- **Owner:** AIVOA
- **Document date:** 2026-08-05
- **Related:** `QMS_BUYING_SIGNAL.md` (L1 signal taxonomy), `realdata_CDSCO.txt` (L1 sample data)

---

## 1. Executive summary

Level 1 (CDSCO Scraper) answers **WHO fails and WHEN.** Level 2 answers the
question L1 cannot: **WHY — is the failure a paper-based QMS problem?**

The NSQ/spurious reason text is chemistry (assay, dissolution, description) and
never mentions paper, QR, serialization, or documentation. So L1's `is_paper_failure`
flag correctly stays 0 — the public Indian dataset has no QMS signal.

Level 2 solves this by enriching L1 leads with **manufacturing-side inspection data**
from the export markets that audit Indian plants: US FDA 483s / Warning Letters,
MHRA, TGA, Health Canada, and WHO Prequalification reports. Those documents
explicitly cite documentation and records failures — the literal paper-QMS pain that
an eQMS fixes.

**Outcome:** an account-level queue of Indian pharma manufacturers who (a) are on
paper/manual QMS, and (b) have a live regulatory failure or a 2026 mandate deadline —
a short-cycle, forced-digitization buyer for AIVOA's QMS.

---

## 2. Problem statement

1. **No direct Indian signal.** CDSCO notices describe physical/chemical failures
   only; nothing reveals whether a plant runs paper batch records or an eQMS.
2. **The mandate clock is ticking.** Rule 96 (API QR/serialization), Sub-Rule 7
   (excipient controls), and Schedule H2 (track-and-trace) take effect in 2026 and
   **cannot be met on paper.** A plant that isn't digital is a deadline-driven buyer —
   but L1 only flags the failures, not the readiness gap.
3. **Cold outreach is the status quo.** Without evidence, SDRs pitch "maybe you need
   QMS." L2 supplies the proof and the opening line.

---

## 3. Target ICP (ideal customer profile)

| Attribute | Criteria |
|---|---|
| Geography | India (API + FDF manufacturers), exporting to US/EU/AU/CA/WHO markets |
| QMS state | Paper / manual systems (no eQMS, no EDMS, no LIMS audit trail) |
| Trigger | ≥1 NSQ/spurious failure in last 12 months, **or** a Rule 96 / Sub-Rule 7 / Schedule H2 mandate flag (L1) |
| Signal | An FDA 483 / Warning Letter / MHRA / TGA / HC / WHO finding citing records, documentation, or data-integrity issues |
| Buyer | QA / RA / Compliance head; sign-off from plant director |

---

## 4. Signal taxonomy (L2 flags, per inspection document)

LLM-classified from the inspection text, same structured-flag pattern as L1.

| Flag | Meaning |
|---|---|
| `paper_qms_evidence` | Text cites missing/incomplete records, manual/handwritten records, uncontrolled spreadsheets |
| `records_incomplete` | Batch production / QC records incomplete or unavailable |
| `no_audit_trail` | No electronic audit trail for changes (21 CFR 11) |
| `data_integrity_findings` | Deleted/backdated data, shared credentials, manual data entry without verification (ALCOA+) |
| `written_procedures_missing` | "No written procedures" / "procedures not in writing" |
| `electronic_records_absent` | Explicit absence of electronic records / reliance on paper |
| `mandate_non_compliant` | Serialization / track-and-trace not implemented or not verifiable |
| `evidence_quote` | Exact sentence backing the flags (outreach hook) |
| `source` / `doc_url` | Which regulator + document + date (provenance) |

**Keyword library (v0):**
`failed to maintain complete records`, `batch production records incomplete`,
`manual`, `handwritten`, `no written procedures`, `procedures not in writing`,
`no audit trail`, `data integrity`, `deleted data`, `backdating`,
`electronic records`, `serialization`, `track-and-trace`, `ALCOA+`.

---

## 5. Data sources (ranked by signal value for Indian pharma)

| # | Source | Why it matters | Access |
|---|---|---|---|
| 1 | **US FDA 483s + Warning Letters** | Explicit documentation / data-integrity citations; huge India coverage | Free (fda.gov/ICECI, openFDA API, public scrapers) |
| 2 | **MHRA GMP non-compliance statements** | Free PDFs; frequently cite records/documentation for Indian sites | Free (gov.uk) |
| 3 | **TGA (Australia) GMP clearances** | Non-compliance + clearance lists per facility | Free |
| 4 | **Health Canada GMP compliance letters** | Per-license PDFs | Free |
| 5 | **WHO Prequalification inspection reports** | Structured critical/major/minor findings incl. documentation | Free PDFs |
| 6 | **EudraGMDP (EU)** | Searchable GMP/GDP inspection findings | Paid/restricted |
| 7 | **CDSCO Schedule M / GMP compliance status** | Home-market GMP compliance and licence actions | Free (varies by state) |
| 8 | **Company footprint enrichment** | ISO cert listed but no EDMS/eQMS mention; job posts for manual QA roles | Web/LinkedIn |

---

## 6. Architecture / pipeline

```
L1 CDSCO leads (existing DB)
        │  manufacturer + address + mandate flags
        ▼
L2 Collectors          FDA 483/WL · MHRA · TGA · Health Canada · WHO-PQ
        │  (reuse L1 scraping + retry/backoff infra)
        ▼
Parser / normalizer    document → evidence snippets + company + date
        ▼
LLM classification     structured L2 flags + evidence_quote
        ▼
Entity resolution      fuzzy join to L1 leads (name/address similarity)
        ▼
L2 scoring             per-account: severity × paper-signals × recency
        ▼
SDR queue              ranked accounts + evidence + outreach kit
```

- **Separate repo/service** — Level 2 is its own project; consumes the L1 database
  read-only (or a shared `leads` table).
- **Reuse:** L1's scraper scaffolding (retry/backoff, `_probe`, batch LLM analysis,
  `reanalyze_records.py`-style in-place backfill) maps 1:1 onto this pipeline.

---

## 7. L2 scoring model (draft)

| Component | Value | Notes |
|---|---|---|
| Source weight | FDA WL 40 / FDA 483 30 / MHRA 30 / TGA 20 / HC 20 / WHO-PQ 25 | Regulators that shut down exports weigh more |
| Paper-QMS flag | +20 per `paper_qms_evidence`, `no_audit_trail`, `data_integrity_findings` (cap +60) | Direct ICP fit |
| Mandate non-compliance | +20 if 2026 mandate flag from L1 **and** `mandate_non_compliant` | Deadline-driven buyer |
| Recency | 1.0 → 0.6 decay (same as L1) | Inspections >3 yrs old are cold |
| Repeat | +10/finding per company, cap +30 | Chronic = warmer |

Combined **L1 + L2 lead score** = max(L1, L2) blended for the SDR queue.

---

## 8. Product surface (what the user sees)

- **L2 Queue:** accounts ranked by L2 score; paper flags, source, doc date visible.
- **Account page:** L1 failures (from L1) + L2 inspection findings side by side.
- **Evidence panel:** the exact `evidence_quote` + regulator PDF link per finding.
- **Filters:** paper-based only, source, export market, mandate flag, recency.
- **Outreach kit:** auto-drafted opening line built from the evidence
  ("Your plant's 2026 FDA 483 cited incomplete batch records — here's how an
  electronic QMS fixes that before the next audit.").

---

## 9. Milestones (what we do next)

| Phase | Scope | Exit criteria |
|---|---|---|
| **M0 — FDA collector (proof of value)** | Build FDA 483 + Warning Letter collector (openFDA + fda.gov), keyword filter, dump for top 50 L1 manufacturers | ≥50% of known high-signal manufacturers surfaced with paper-QMS citations |
| **M1 — Multi-regulator + entity resolution** | MHRA / TGA / Health Canada / WHO-PQ collectors; fuzzy join to L1 by manufacturer name/address | Lead table links L1 failures to L2 inspections |
| **M2 — LLM classification + scoring + UI** | Structured flags, L2 scoring, dashboard queue + evidence panel | 10-test accounts scored and ranked correctly |
| **M3 — Outreach loop** | Evidence-based outreach kit + exports/CRM handoff | SDR can run a full cycle from queue to message |

---

## 10. Open questions / risks

- **Data access:** openFDA does not expose full 483 text; FOIA-backed feeds or
  scrapers (FDAzilla-style) may be needed. EudraGMDP is paid.
- **Entity matching:** manufacturer strings vary across sources → fuzzy resolution
  + manual review for the long tail.
- **Staleness:** older 483s decay; date-gate findings to ≤3 years by default.
- **Confidentiality:** all sources are public; no PII. Fine for B2B lead-gen.
- **L1 coupling:** keep L1 unchanged; L2 reads the L1 DB read-only so live L1
  workflows are never interrupted.
