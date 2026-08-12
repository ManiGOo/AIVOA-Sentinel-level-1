# Lead Research — Progress & Next Steps

Last updated: 2026-08-11

## Goal

Make lead research actually find **decision makers (names)** and reliable company
data for pharma companies, and surface it through the sales-app chat (MCP) and
Leads page. Previously the workflow returned **0 decision makers** for
`r p biotech` and the chat hallucinated answers.

## The New Algorithm (user-approved)

> 1. Web-scrape the company's website first; store **all** the data from it
>    (financial, location, contact, team, etc.).
> 2. Use a new names algorithm based on **corporate-registry sources**
>    (MCA-derived: ZaubaCorp, Tofler, Tracxn, IndiaFilings, InstaFinancials,
>    Sensibook, Falcon Ebiz) + LinkedIn — the same sources that give the
>    authoritative director list ("Directors are X, Y, Z" / signatory tables).

This replaces the old fragile "regex over truncated Tavily snippets + flaky LLM"
approach as the primary source of names.

## What Was Built / Fixed

### DB schema (`db_setup.py`)
- Added `scraped_data` JSONB column to `company_leads` — stores scraped website
  pages, emails, phones, address, financial mentions, team members.
- Added `corporate_registry` JSONB column — stores CIN + director list + registry items.
- Safe ALTER migrations added to `init_db()` for existing tables.
- Verified columns exist in the remote DB.

### New activities (`lead_research_tasks.py`)
- **`scrape_company_website_activity`** — web-scrapes the company's own website
  with Playwright (headless Chromium, works in the worker container). Tries
  home / about / team / management / leadership / contact pages. Parses emails,
  phones, physical+registered address, financial mentions, and team-member names.
- **`search_corporate_registry_activity`** — searches MCA-derived corporate
  registry platforms and deterministically extracts directors + CIN via
  `_extract_registry_directors`:
  - "Directors of X are A, B, C" pattern
  - signatory-table rows `| Name | Director |`
  - CIN regex `U\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}` (fixed from `[A-Z]{5}` → `[A-Z]{3}`)
  - Only accepts results that demonstrably reference the target company
    (CIN present, or URL slug contains the company's name tokens) so a
    same-word relative ("Professional Biotech", "Sucantis Biotech") or a bank
    list of unrelated directors cannot leak in.
- Both wired into `LeadResearchWorkflow` (scrape website → search registry →
  extract people → evaluate) and registered in `enricher_worker.py`.
- **Root-cause fix**: `extract_people_activity` was NEVER registered in the
  worker → every workflow failed with "Activity function extract_people_activity
  is not registered". This is why all runs stored 0 decision makers.

### Names algorithm (in `_heuristic_evaluate`)
- Corporate-registry directors are folded in **first** with `confidence: high`
  (role "Director") — the most authoritative source.
- Website team members second with `confidence: medium`.
- LLM-extracted people + regex fallback last.
- ALL-CAPS director names (e.g. `RAKESH KUMAR`) normalized to Title Case.

### Fuzzy company matching (`cognitive_engine.py` `_fuzzy_company_match`)
Rewrote to be **strict**:
- Matches the distinctive run of the company name with flexible separators
  ("R.P. Biotech" → `r\s*\.?\s*p\s*\.?\s*biotech`), so `RP BIOTECH`,
  `R P BIOTECH`, `r.p.biotech` all match.
- Rejects other-biotech firms: "MAPLE BIOTECH", "SUCANTIS BIOTECH",
  "PROFESSIONAL BIOTECH" no longer match "R.P. Biotech".
- Legal suffixes (pvt/ltd/private/limited/co/corp/inc/...) dropped word-wise.

### Website picker (`_pick_best_website` / `_website_plausible`)
- Company token must appear in the **HOST**, not the path — so
  `rpgroupbd.com/industry/rp-biotech` (a Bangladesh apparel group) is no longer
  picked as R.P. Biotech's website.
- Directory hosts expanded: added `instafinancials.com`, `indiafilings.com`,
  `sensibook.com`, `falcon ebiz`, `financeninsurance.com`, `charteredone`,
  `ibphub.com`.

### Website team extraction (`_website_people`)
- Stricter: role keyword must be on the same line and near the name;
  names composed of role/descriptor words ("Lakh Directors Information",
  "Managing Director", "Your Trusted Apparel", "World Sources") are rejected.

### MCP
- `get_lead` tool now also returns `scraped_data` and `corporate_registry`.

## Verified Result (last good workflow run, `20260811121719`)

Decision makers stored for `r p biotech`:

| Name | Role | Confidence | Source |
|------|------|-----------|--------|
| Kunwar Rampal | Director | high | corporate registry |
| Sahil Rampal | Director | high | corporate registry |
| Pardeep Kumar | Director | high | corporate registry |
| Ashwani Kumar | Director | high | corporate registry |
| Rakesh Kumar | Director | high | corporate registry (signatory table) |
| Rishab Sanan | Supervisor | high | LinkedIn |

- `corporate_registry.cin = U24232PB2001PTC024580`
- `company_status = active`
- `get_lead` MCP tool returns this data end-to-end; chat can now answer honestly.
- Junk team members from rpgroupbd/instafinancials eliminated.

These match the expected names exactly (the 4 directors + QA/QC LinkedIn people
the AI-mode search surfaced).

## Still Open / To Do LATER

1. **Re-run full workflow after the final strict-fuzzy + LinkedIn-verification
   changes.** The last full run (`20260811122522`) used the old lenient fuzzy
   match and pulled in people from other biotech firms (karthi Robert, Swapnil
   Mali/Maple Biotech, Jitender Singh/Alencure, etc.) because:
   - `_regex_extract_people` LinkedIn signal accepted any `linkedin.com/in/` URL
     without checking it belongs to the company → **fixed** (now requires
     `_fuzzy_company_match`).
   - `_fuzzy_company_match` matched any page containing "biotech" → **fixed**
     with the flexible initials+core pattern.
   - `extract_people_activity` fed the LLM un-filtered combined candidates →
     **fixed** (now filters to company-relevant results first).
   Rebuild was done; registry extraction re-test was interrupted by a Tavily
   rate-limit ("exceeds your plan's set usage limit") — retry and run the
   workflow to confirm only real people are stored.

2. **Verify intent_signals / trigger_events** on the next run are still clean
   (the strict fuzzy match could change which news/hiring results pass).

3. **Email enrichment** — `decision_makers` currently have `email: ""`. Options:
   email-guessing from the scraped website domain, or a lookups service.

4. **Robustness**:
   - Tavily plan has a usage cap — consider retry/backoff on
     "exceeds your plan's set usage limit" (currently only 2 attempts).
   - Playwright scrape of a real company website (one that resolves) still
     needs an end-to-end check — rpbiotech.in does not resolve, so the scrape
     path returned 0 pages in tests. Test with a company that has a live site.

5. **Normalize director roles**: registry directors are all typed as
   `managing_director`; could distinguish `director` vs `managing_director`
   from the signatory table when available.

6. **sales-app UI**: confirm the Leads page + chat show the new decision makers
   (no code change expected — it reads via the existing endpoints, but verify).

## How to Re-run

```bash
cd /home/many-wallnut/Desktop/scrapper
docker compose build enricher && docker compose up -d enricher
curl -s -X POST http://localhost:5000/api/v1/leads/research \
  -H "Content-Type: application/json" \
  -d '{"company_keys":["r p biotech"]}'
# then poll
curl -s http://localhost:5000/api/v1/leads/status
```
