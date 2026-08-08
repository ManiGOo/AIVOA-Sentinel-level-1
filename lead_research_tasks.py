import os
import asyncio
import json
import re
from datetime import timedelta, datetime
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from tavily import TavilyClient
    from db_setup import SessionLocal, CompanyLead
    from company_names import clean_company_name, PAREN
    from cognitive_engine import (
        client as groq_client,
        GROQ_API_KEY,
        classify_lead_relevance_batch,
        _fuzzy_company_match,
    )

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_LINKEDIN_DOMAINS = ["linkedin.com"]

_DIRECTORY_HOSTS = (
    "indiamart.com", "justdial", "sulekha", "tradeindia", "yellowpages",
    "exportersindia", "yelu", "indiabizclub", "zaubacorp.com", "tofler",
    "tracxn.com", "crediwatch", "registerkaro", "vakilsearch", "ebizprise",
    "emis.com", "crunchbase", "owler", "bizapedia", "opencorporates",
    "thecompanycheck", "quickcompany", "corporationwiki", "buzzfile",
    "moneycontrol", "bloomberg", "dnb.com", "kompass",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "glassdoor.com", "indeed.com", "naukri.com", "timesjobs.com", "zoominfo",
)

def _company_tokens(company_name: str) -> set:
    cleaned = clean_company_name(PAREN.sub("", company_name or ""))
    return {w for w in re.findall(r"[a-z0-9]+", cleaned.lower()) if len(w) >= 4}


def _is_clean_host(url: str) -> bool:
    host = url.split("/")[2] if "//" in url else ""
    return not host or not any(d in host.lower() for d in _DIRECTORY_HOSTS)


def _website_plausible(url: str, company_name: str) -> bool:
    if not _is_clean_host(url):
        return False
    tokens = {t for t in _company_tokens(company_name) if len(t) >= 5}
    if tokens:
        return any(t in url.lower() for t in tokens)
    clean = re.sub(r"[^a-z0-9]", "", company_name.lower())
    return len(clean) >= 5 and clean in re.sub(r"[^a-z0-9]", "", url.lower())


def _pick_best_website(items: list, company_name: str) -> str:
    for it in items:
        url = it.get("url", "")
        if _website_plausible(url, company_name):
            return url
    for it in items:
        url = it.get("url", "")
        if _is_clean_host(url):
            return url
    return ""


def _linkedin_slug(url: str) -> str:
    low = url.lower().rstrip("/")
    return low.split("linkedin.com/", 1)[-1] if "linkedin.com" in low else ""


def _pick_linkedin(items: list, company_name: str) -> str:
    tokens = {t for t in _company_tokens(company_name) if len(t) >= 5}
    # Prefer company page with a name-matching slug
    for it in items:
        url = it.get("url", "")
        slug = _linkedin_slug(url)
        if not slug or "linkedin.com/company/" not in url.lower():
            continue
        if tokens and any(t in slug for t in tokens):
            return url
    # Any company page
    for it in items:
        url = it.get("url", "")
        if "linkedin.com/company/" in url.lower():
            return url
    # Fuzzy match on title for a company page
    for it in items:
        url = it.get("url", "")
        if "linkedin.com/company/" in url.lower() and _fuzzy_company_match(company_name, it.get("title", "")):
            return url
    return ""


# ---------------------------------------------------------------------------
# Generic agentic search + relevance classify
# ---------------------------------------------------------------------------

def _search_and_classify(company_name: str, queries: list, category: str,
                         max_results: int = 8, threshold: int = 25) -> list:
    """Search Tavily for each query, classify relevance, return scored items.

    Uses deterministic heuristic as the primary classifier (consistent) and the
    LLM as a secondary signal. This avoids the non-determinism of relying on
    the LLM alone."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return []
    tavily = TavilyClient(api_key=api_key)
    seen = set()
    collected = []
    for spec in queries:
        query = spec if isinstance(spec, str) else spec.get("query", "")
        if not query:
            continue
        kwargs = dict(query=query, search_depth="advanced",
                      max_results=max_results, include_raw_content=False)
        try:
            response = tavily.search(**kwargs)
        except Exception as e:
            print(f"Tavily search error for '{query}': {e}")
            continue
        for result in response.get("results", []):
            url = result.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            collected.append({
                "title": result.get("title", ""),
                "url": url,
                "snippet": result.get("content", ""),
                "source": url.split("/")[2] if "//" in url else "",
            })
    if not collected:
        return []
    # Primary: deterministic heuristic (consistent)
    from cognitive_engine import _heuristic_lead_relevance, _fuzzy_company_match as _fuzzy
    heuristic_scores = [_heuristic_lead_relevance(company_name, r, category) for r in collected]
    # Secondary: LLM for category matching signal (best-effort)
    try:
        llm_scores = classify_lead_relevance_batch(company_name, collected, category)
    except Exception:
        llm_scores = [{}] * len(collected)
    out = []
    for item, hscore, lscore in zip(collected, heuristic_scores, llm_scores):
        h = hscore.get("relevance_score") or 0
        l = lscore.get("relevance_score") or 0
        # Combine: take the max of heuristic and LLM, with fuzzy boost
        fuzzy = _fuzzy(company_name, item.get("title", "") + " " + item.get("snippet", ""))
        if fuzzy:
            score = max(h, l, 50)
        else:
            score = max(h, l)
        item["relevance_score"] = score
        item["relevance_reason"] = lscore.get("reason", "") or hscore.get("reason", "")
        if score >= threshold:
            out.append(item)
    out.sort(key=lambda x: x.get("relevance_score") or 0, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Activity 1: Company profile (website, LinkedIn, status)
# ---------------------------------------------------------------------------

def _profile_queries(company_name: str) -> dict:
    q = company_name.strip().replace('"', '')
    return {
        "website": [
            f'"{q}" official website',
            f'"{q}" pharmaceutical company',
        ],
        "linkedin": [
            f'"{q}" linkedin company page',
        ],
        "status": [
            f'"{q}" pharmaceutical company active manufacturing',
            f'"{q}" company news 2025 2026',
        ],
    }


@activity.defn
async def search_company_profile_activity(company_name: str) -> dict:
    def _work() -> dict:
        queries = _profile_queries(company_name)
        website_items = _search_and_classify(company_name, queries["website"], "website", threshold=30)
        linkedin_items = _search_and_classify(company_name, queries["linkedin"], "linkedin", threshold=30)
        status_items = _search_and_classify(company_name, queries["status"], "status", threshold=25)
        return {
            "company_name": company_name,
            "website_candidates": website_items,
            "linkedin_candidates": linkedin_items,
            "status_candidates": status_items,
        }
    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Activity 2: Decision makers (QA head / MD / key people + email)
# ---------------------------------------------------------------------------

def _decision_maker_queries(company_name: str) -> list:
    q = company_name.strip().replace('"', '')
    return [
        f'"{q}" "QA head" OR "quality assurance head" OR "quality head" linkedin',
        f'"{q}" "QA manager" OR "quality manager" OR "quality control" linkedin',
        f'"{q}" "managing director" OR "founder" OR "CEO" linkedin',
        f'"{q}" "plant head" OR "production head" linkedin',
        f'"{q}" email contact',
        f'"{q}" team members quality',
    ]


@activity.defn
async def search_decision_makers_activity(company_name: str) -> dict:
    def _work() -> dict:
        items = _search_and_classify(company_name, _decision_maker_queries(company_name),
                                     "decision_maker", threshold=30, max_results=10)
        return {"company_name": company_name, "people_candidates": items}
    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Activity 3: Intent signals (hiring, news, QMS triggers)
# ---------------------------------------------------------------------------

def _intent_queries(company_name: str) -> dict:
    q = company_name.strip().replace('"', '')
    return {
        "hiring": [
            f'"{q}" hiring jobs careers 2025 2026',
            f'"{q}" "now hiring" OR "walk in" OR job opening',
            f'"{q}" QA quality hiring',
        ],
        "news": [
            f'"{q}" news expansion investment 2025 2026',
            f'"{q}" new facility OR plant OR manufacturing',
        ],
        "triggers": [
            f'"{q}" NSQ OR "not of standard quality" OR substandard',
            f'"{q}" warning letter OR FDA OR "regulatory action"',
            f'"{q}" recall OR CAPA OR deviation OR inspection',
            f'"{q}" "paper QMS" OR "manual records" OR documentation',
        ],
    }


@activity.defn
async def search_intent_signals_activity(company_name: str) -> dict:
    def _work() -> dict:
        queries = _intent_queries(company_name)
        return {
            "company_name": company_name,
            "hiring_candidates": _search_and_classify(company_name, queries["hiring"], "hiring", threshold=35),
            "news_candidates": _search_and_classify(company_name, queries["news"], "news", threshold=25),
            "trigger_candidates": _search_and_classify(company_name, queries["triggers"], "triggers", threshold=30),
        }
    return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Activity 4: Evaluate + save (LLM structures the final lead record)
# ---------------------------------------------------------------------------

def _evaluate_lead(company_name: str, profile: dict, people: dict, signals: dict) -> dict:
    """Structure raw search results into the final lead record.

    Uses a deterministic heuristic for the structured fields (website, LinkedIn,
    decision makers, signals) and a short LLM call ONLY for the narrative summary.
    This avoids the unreliability of asking one giant prompt to do everything."""
    base = _heuristic_evaluate(company_name, profile, people, signals)
    summary = _llm_summary(company_name, profile, people, signals, base)
    if summary:
        base["activity_summary"] = summary
    return base


def _llm_summary(company_name: str, profile: dict, people: dict, signals: dict, base: dict) -> str:
    """Short LLM call for the narrative activity summary only."""
    if not GROQ_API_KEY.startswith("gsk_"):
        return ""
    decision_makers = base.get("decision_makers", [])
    dm_names = ", ".join(d.get("name", d.get("role", "?")) for d in decision_makers[:3])
    trigger_count = len(base.get("trigger_events", []))
    hiring_count = len([s for s in base.get("intent_signals", []) if s.get("category") == "hiring"])
    prompt = f"""Company: {company_name}
Status: {base.get("company_status", "unknown")}
Key people: {dm_names or "none found"}
Active job postings: {hiring_count}
QMS trigger events (NSQ/recall/warning): {trigger_count}
Website: {base.get("website", "")}
Write 2 sentences: is this pharma company active right now, and why might they need QMS software? Plain text, no JSON."""
    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You write concise sales-intelligence notes."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=200,
        )
        return (completion.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"Groq summary error: {e}")
        return ""


def _heuristic_evaluate(company_name: str, profile: dict, people: dict, signals: dict) -> dict:
    """Deterministic structuring of search results into the lead record."""
    website = _pick_best_website(profile.get("website_candidates", [])[:8], company_name)
    linkedin = _pick_linkedin(profile.get("linkedin_candidates", [])[:6], company_name)

    status_items = profile.get("status_candidates", [])
    company_status = "active" if status_items else "unknown"

    people_items = sorted(
        people.get("people_candidates", []),
        key=lambda x: x.get("relevance_score") or 0, reverse=True,
    )
    company_tokens = _company_tokens(company_name)
    company_clean = clean_company_name(PAREN.sub("", company_name or "")).lower()
    generic = {"pharmaceutical", "pharmaceuticals", "pharma", "private", "limited", "pvt", "ltd"}
    distinctive_tokens = company_tokens - generic
    decision_makers = []
    seen_names = set()
    for it in people_items:
        raw_name = it.get("title", "").split("|")[0].split("-")[0].split("·")[0].strip()
        name = re.sub(r"\s+", " ", raw_name).strip()
        if not name or len(name) < 3 or name.lower() in seen_names:
            continue
        # Skip generic job listings and company-description pages
        if re.match(r"^\d", name) or "jobs in" in name.lower() or "hiring" == name.lower():
            continue
        # Skip if the "name" is actually the company itself (not a person)
        name_lower = name.lower()
        if any(t in name_lower for t in company_tokens if len(t) >= 6) and len(name.split()) >= 4:
            continue
        # Skip if it looks like a company description (no person name)
        if any(name_lower.startswith(w) for w in ("the ", "our ", "we ", "this ")):
            continue
        title = it.get("title", "")
        snippet = it.get("snippet", "")
        hay = (title + " " + snippet).lower()

        works_here = _fuzzy_company_match(company_name, title + " " + snippet)
        if not works_here:
            continue

        # Confidence: how directly does the company name appear as their employer?
        employer_field = title.split("-")[-1].strip().lower() if "-" in title else ""
        employer_field += " " + snippet[:200].lower()
        # Direct: distinctive token appears right next to employer context
        direct_employer = (
            any(t in employer_field for t in distinctive_tokens)
            or company_clean in employer_field
            or re.search(r"(at|@)\s*" + re.escape(company_clean.split()[0]), employer_field)
        )
        # Strong: the result's relevance classifier also flagged company_match
        strong_relevance = (it.get("relevance_score") or 0) >= 70
        if direct_employer and strong_relevance:
            confidence = "high"
        elif direct_employer or strong_relevance:
            confidence = "medium"
        else:
            confidence = "low"

        role_type = "other"
        if any(k in hay for k in ("qa head", "qa manager", "quality assurance head", "quality manager", "quality control manager")):
            role_type = "qa_head"
        elif any(k in hay for k in ("qa", "quality assurance", "quality control", "quality officer")):
            role_type = "qa_manager"
        elif any(k in hay for k in ("managing director",)):
            role_type = "managing_director"
        elif any(k in hay for k in ("founder", "ceo ", "chief executive", " co-founder")):
            role_type = "founder_ceo"
        elif any(k in hay for k in ("plant head", "production head", "production manager")):
            role_type = "plant_head"
        linkedin_url = it.get("url", "") if "linkedin.com" in it.get("url", "") else ""
        decision_makers.append({
            "name": name,
            "role": title.strip(),
            "role_type": role_type,
            "linkedin_url": linkedin_url,
            "email": "",
            "confidence": confidence,
        })
        seen_names.add(name.lower())
        if len(decision_makers) >= 6:
            break

    intent_signals = []
    for it in signals.get("hiring_candidates", [])[:4]:
        intent_signals.append({**it, "category": "hiring"})
    for it in signals.get("news_candidates", [])[:3]:
        intent_signals.append({**it, "category": "expansion"})
    intent_signals.sort(key=lambda x: x.get("relevance_score") or 0, reverse=True)

    trigger_events = []
    for it in signals.get("trigger_candidates", [])[:5]:
        hay = (it.get("title", "") + " " + it.get("snippet", "")).lower()
        cat = "regulatory_action"
        if "nsq" in hay or "not of standard" in hay or "substandard" in hay:
            cat = "nsq_alert"
        elif "warning" in hay or "fda" in hay:
            cat = "warning_letter"
        elif "recall" in hay:
            cat = "recall"
        elif "paper" in hay or "manual" in hay or "documentation" in hay:
            cat = "documentation_issue"
        trigger_events.append({**it, "category": cat})

    return {
        "website": website,
        "linkedin_url": linkedin,
        "company_status": company_status,
        "decision_makers": decision_makers,
        "intent_signals": intent_signals[:6],
        "trigger_events": trigger_events[:5],
        "activity_summary": "",
    }


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _extract_email(text: str, domain_hint: str = "") -> str:
    """Best-effort email extraction from visible text."""
    if not text:
        return ""
    matches = _EMAIL_RE.findall(text)
    for m in matches:
        if domain_hint and domain_hint in m:
            return m
    # Return first non-generic match
    for m in matches:
        if not m.lower().startswith(("noreply", "no-reply", "donotreply", "support@", "info@")):
            return m
    return matches[0] if matches else ""


@activity.defn
async def evaluate_and_save_lead_activity(payload: dict) -> dict:
    """Evaluate all search results via LLM and persist the lead record."""
    def _work() -> dict:
        company_key = payload["company_key"]
        company_name = payload["company_name"]
        profile = payload.get("profile", {})
        people = payload.get("people", {})
        signals = payload.get("signals", {})

        evaluated = _evaluate_lead(company_name, profile, people, signals)

        for dm in evaluated.get("decision_makers", []):
            dm.setdefault("confidence", "low")
        evaluated["decision_makers"] = sorted(
            evaluated.get("decision_makers", []),
            key=lambda d: {"high": 0, "medium": 1, "low": 2}.get(d.get("confidence", "low"), 2),
        )

        # Best-effort email extraction from people snippets
        for dm in evaluated.get("decision_makers", []):
            if not dm.get("email"):
                dm["email"] = ""

        website = evaluated.get("website", "")
        domain_hint = website.split("/")[2] if "//" in website else ""
        for dm in evaluated.get("decision_makers", []):
            if dm.get("email"):
                continue
            for it in people.get("people_candidates", []):
                if it.get("url") == dm.get("linkedin_url") or it.get("title", "") in dm.get("role", ""):
                    email = _extract_email(it.get("snippet", ""), domain_hint)
                    if email:
                        dm["email"] = email
                        break

        db = SessionLocal()
        try:
            row = db.query(CompanyLead).filter(CompanyLead.company_key == company_key).first()
            if row is None:
                row = CompanyLead(company_key=company_key)
                db.add(row)
            row.company_name = company_name
            row.website = website
            row.linkedin_url = evaluated.get("linkedin_url", "")
            row.company_status = evaluated.get("company_status", "unknown")
            row.decision_makers = evaluated.get("decision_makers", [])
            row.intent_signals = evaluated.get("intent_signals", [])
            row.trigger_events = evaluated.get("trigger_events", [])
            row.activity_summary = evaluated.get("activity_summary", "")
            row.hiring = [s for s in evaluated.get("intent_signals", []) if s.get("category") == "hiring"]
            row.hiring_news = [s for s in evaluated.get("intent_signals", []) if s.get("category") != "hiring"]
            row.summary = {
                "searched_at": datetime.utcnow().isoformat(),
                "profile_candidates": len(profile.get("website_candidates", [])) + len(profile.get("linkedin_candidates", [])),
                "people_candidates": len(people.get("people_candidates", [])),
                "signal_candidates": len(signals.get("hiring_candidates", [])) + len(signals.get("news_candidates", [])) + len(signals.get("trigger_candidates", [])),
            }
            row.status = "completed"
            row.error = ""
            row.fetched_at = datetime.utcnow()
            db.commit()
            return {"company_key": company_key, "status": "completed"}
        except Exception as e:
            db.rollback()
            print(f"evaluate_and_save_lead_activity error: {e}")
            return {"company_key": company_key, "status": "failed", "error": str(e)[:500]}
        finally:
            db.close()

    return await asyncio.to_thread(_work)


@activity.defn
def mark_lead_failed_activity(company_key: str, error: str) -> dict:
    db = SessionLocal()
    try:
        row = db.query(CompanyLead).filter(CompanyLead.company_key == company_key).first()
        if row is not None:
            row.status = "failed"
            row.error = (error or "")[:500]
            db.commit()
        return {"company_key": company_key, "status": "failed"}
    except Exception as e:
        db.rollback()
        print(f"mark_lead_failed_activity error: {e}")
        return {"company_key": company_key, "status": "failed"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@workflow.defn
class LeadResearchWorkflow:
    def __init__(self):
        self._status = "starting"
        self._company_name = ""

    @workflow.query
    def progress(self) -> dict:
        return {"status": self._status, "company_name": self._company_name}

    @workflow.run
    async def run(self, company_key: str, company_name: str) -> dict:
        self._company_name = company_name
        try:
            self._status = "searching_profile"
            profile = await workflow.execute_activity(
                search_company_profile_activity,
                args=[company_name],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "searching_decision_makers"
            people = await workflow.execute_activity(
                search_decision_makers_activity,
                args=[company_name],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "searching_intent_signals"
            signals = await workflow.execute_activity(
                search_intent_signals_activity,
                args=[company_name],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "evaluating_and_saving"
            saved = await workflow.execute_activity(
                evaluate_and_save_lead_activity,
                args=[{
                    "company_key": company_key,
                    "company_name": company_name,
                    "profile": profile,
                    "people": people,
                    "signals": signals,
                }],
                start_to_close_timeout=timedelta(minutes=8),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            self._status = "completed"
            return saved
        except Exception as e:
            self._status = "failed"
            try:
                await workflow.execute_activity(
                    mark_lead_failed_activity,
                    args=[company_key, str(e)],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            except Exception as mark_err:
                print(f"mark_lead_failed_activity workflow error: {mark_err}")
            raise
