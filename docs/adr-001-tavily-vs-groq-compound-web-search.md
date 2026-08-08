# ADR-001: Why We Use Tavily Direct Instead of Groq Compound Web Search

**Status:** Accepted
**Date:** 2026-08-08
**Context:** Choosing the web search architecture for structured B2B lead intelligence

---

## Executive Summary

We evaluated Groq Compound (built-in web search) against direct Tavily API usage for our lead research pipeline. **We chose Tavily Direct** because our use case requires precise, structured, repeatable search queries — exactly the opposite of what Groq Compound's "let the model decide" architecture provides.

---

## Background

Groq offers two ways to access web search:

1. **Groq Compound** (`groq/compound`, `groq/compound-mini`) — Agentic systems with built-in web search, code execution, and browser automation. The model decides when and what to search.
2. **Tavily Direct** — Our current approach: we craft explicit search queries, call Tavily's API, then send results to Groq's LLM for analysis.

**Key fact:** Groq's web search is powered by Tavily under the hood. The question is not *which search engine* but *who controls the queries*.

---

## Why Groq Compound Doesn't Fit Our Use Case

### 1. We Need Query Precision, Not Model Discretion

Our lead research runs **9 structured queries per company**:

```python
# These are NOT open-ended research questions.
# They are precise data-extraction queries.

f'"{company}" "QA head" OR "quality manager" linkedin'
f'"{company}" "managing director" OR "foundin" linkedin'
f'"{company}" NSQ OR "not of standard quality" OR substandard'
f'"{company}" hiring jobs careers 2025 2026'
f'"{company}" "official website"'
f'"{company}" linkedin company page'
```

**Groq Compound** receives a prompt like "find me QA people at Saintlife" and the model *decides* what to search. Sometimes it searches well, sometimes it doesn't. You can't guarantee coverage.

**Tavily Direct** executes exactly what we ask. Every company gets the same systematic coverage.

### 2. Lead Research Is Data Extraction, Not Open-Ended Research

Groq Compound is designed for questions like:
- "What happened in AI last week?"
- "Tell me about this company's recent news."

Our pipeline needs:
- Extract all LinkedIn profiles mentioning role X at company Y
- Extract all NSQ alerts for company Z in 2026
- Extract company website URL from search results

This is **structured data extraction**, not open-ended synthesis. The queries must be deterministic.

### 3. Relevance Classification Requires Full Content

We classify every search result for relevance (company match + category match). This requires:

- **Full page content** — Tavily `/extract` gives us the complete article/profile text
- **Exact URL and snippet** — for fuzzy company-name matching
- **Structured scoring** — 0-100 relevance with confidence levels

Groq Compound returns snippets only. We can't run our fuzzy matching or confidence scoring on snippets.

### 4. Repeatability and Debugging

When a search returns wrong results:

**With Tavily Direct:**
```
Query: "Saintlife Pharmaceuticals" "QA manager" linkedin
→ Tavily returns 8 results
→ We see exactly which passed/failed relevance
→ We adjust thresholds or queries
```

**With Groq Compound:**
```
Prompt: "Find QA managers at Saintlife"
→ Model decides to search something
→ We see the final answer, not the search
→ Can't tell if it searched wrong or reasoned wrong
→ Can't fix it deterministically
```

### 5. Cost Predictability

| | Groq Compound | Tavily Direct + Groq LLM |
|---|---|---|
| Search cost | Bundled into compound pricing | Pay per search (known rate) |
| LLM cost | Bundled | Pay per token (known rate) |
| Overhead | You pay for code execution, browser tools you don't use | Pay only for what you call |
| Billing opacity | Single line item, hard to optimize | Separate line items, easy to optimize |

### 6. Tavily Features We Actually Use

| Feature | Usage | Groq Equivalent |
|---|---|---|
| `/extract` | Full page content for relevance classification | Not available |
| `/crawl` | Map a company website | Not available |
| `/map` | Discover site structure | Not available |
| Domain filtering | Exclude directories, include LinkedIn | Basic only |
| Date range | Find recent triggers (2025-2026) | Not available |
| Content type | General vs news vs finance | Not available |
| PII protection | Built-in leakage prevention | Not documented |

---

## When Groq Compound Would Be Useful

We might adopt Groq Compound for:

- **Chat assistant** — The AI chat feature (AIVOA Sentinel) where users ask open-ended questions and the model should decide when to search
- **Exploratory research** — "Tell me everything about this company" where queries aren't predefined
- **Browser automation** — Visiting interactive pages that require JavaScript rendering

Even then, we'd likely keep Tavily Direct for the structured lead research pipeline and use Compound only for the chat assistant.

---

## Architecture Decision

```
CURRENT (Tavily Direct):
  We craft queries → Tavily search → We extract content → Groq LLM analyzes → We structure output

REJECTED (Groq Compound):
  We ask question → Model decides search → Model decides analysis → We get final answer
                  (opaque)          (opaque)           (not auditable)
```

---

## Conclusion

Groq Compound is an excellent product for open-ended agentic research. Our structured B2B lead intelligence pipeline is the opposite of open-ended. We need deterministic queries, full content extraction, transparent debugging, and systematic coverage — all of which Tavily Direct provides and Groq Compound does not.

**Keep Tavily Direct for lead research. Consider Groq Compound only for the chat assistant.**
