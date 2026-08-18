import asyncio
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

from .base import Finding, EnrichmentAdapter


def _fdapi_date(d: str) -> str:
    """Convert 'YYYY-MM-DD' to openFDA's 'YYYYMMDD' format."""
    if not d:
        return d
    return d.replace("-", "")


class OpenFDAAdapter(EnrichmentAdapter):
    """openFDA REST API adapter for drug recalls, enforcement reports, and adverse events.

    Uses the official openFDA API (api.fda.gov) instead of web scraping.
    Supports API key for higher rate limits (set OPENFDA_API_KEY in env).
    """

    source = "FDA"
    base_url = "https://api.fda.gov"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("OPENFDA_API_KEY")
        self.session = None

    async def _get_session(self):
        if self.session is None:
            import aiohttp
            headers = {"User-Agent": "AIVOA-Sentinel/1.0"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self.session = aiohttp.ClientSession(headers=headers)
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    def _build_url(self, endpoint: str, params: dict) -> str:
        """Build URL with query params including API key if available.

        Uses quote_plus so spaces inside the Lucene search expression become
        literal '+' (openFDA syntax), not %20.
        """
        if self.api_key:
            params["api_key"] = self.api_key
        query = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
        return f"{self.base_url}{endpoint}?{query}"

    async def _fetch_page(self, url: str) -> dict | None:
        session = await self._get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 429:
                    raise RateLimitError("Rate limited")
                if resp.status != 200:
                    text = await resp.text()
                    raise APIError(f"HTTP {resp.status}: {text[:200]}")
                return await resp.json()
        except asyncio.TimeoutError:
            raise APIError("Request timeout")
        except aiohttp.ClientError as e:
            raise APIError(f"Network error: {e}")

    async def fetch_enforcement(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch drug enforcement reports (recalls) from openFDA.

        Date range filters on report_date (YYYYMMDD).
        """
        endpoint = "/drug/enforcement.json"
        params = {
            "search": f"report_date:[{_fdapi_date(from_date)} TO {_fdapi_date(to_date)}]",
            "limit": str(min(limit, 1000)),
            "skip": str(skip),
            "sort": "report_date:desc",
        }
        url = self._build_url(endpoint, params)
        data = await self._fetch_page(url)
        if not data or "results" not in data:
            return []

        findings = []
        for item in data["results"]:
            findings.append(self._enforcement_to_finding(item))
        return findings

    async def fetch_adverse_events(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch drug adverse event reports from openFDA (FAERS).

        Date range filters on receiptdate (YYYYMMDD).
        """
        endpoint = "/drug/event.json"
        params = {
            "search": f"receiptdate:[{_fdapi_date(from_date)} TO {_fdapi_date(to_date)}]",
            "limit": str(min(limit, 1000)),
            "skip": str(skip),
            "sort": "receiptdate:desc",
        }
        url = self._build_url(endpoint, params)
        data = await self._fetch_page(url)
        if not data or "results" not in data:
            return []

        findings = []
        for item in data["results"]:
            findings.append(self._adverse_event_to_finding(item))
        return findings

    async def fetch_device_enforcement(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch device enforcement reports (recalls) from openFDA."""
        endpoint = "/device/enforcement.json"
        params = {
            "search": f"report_date:[{_fdapi_date(from_date)} TO {_fdapi_date(to_date)}]",
            "limit": str(min(limit, 1000)),
            "skip": str(skip),
            "sort": "report_date:desc",
        }
        url = self._build_url(endpoint, params)
        data = await self._fetch_page(url)
        if not data or "results" not in data:
            return []

        findings = []
        for item in data["results"]:
            findings.append(self._enforcement_to_finding(item, is_device=True))
        return findings

    def _enforcement_to_finding(self, item: dict, is_device: bool = False) -> Finding:
        """Convert openFDA enforcement record to Finding."""
        firm_name = (
            item.get("recalling_firm")
            or item.get("manufacturer_name")
            or item.get("distributor_name")
            or "Unknown"
        )
        product = item.get("product_description") or item.get("product_type") or ""
        reason = item.get("reason_for_recall") or item.get("event_id") or ""
        report_date = item.get("report_date") or item.get("recall_initiation_date") or ""
        classification = item.get("classification") or ""
        code_info = item.get("code_info") or ""
        recall_number = item.get("recall_number") or ""
        url = f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfRes/res.cfm?id={recall_number}" if recall_number else ""

        if report_date and len(report_date) >= 8:
            try:
                finding_date = f"{report_date[:4]}-{report_date[4:6]}-{report_date[6:8]}"
            except Exception:
                finding_date = None
        else:
            finding_date = None

        evidence_parts = []
        if product:
            evidence_parts.append(f"Product: {product}")
        if reason:
            evidence_parts.append(f"Reason: {reason}")
        if classification:
            evidence_parts.append(f"Class: {classification}")
        if code_info:
            evidence_parts.append(f"Code: {code_info}")
        evidence_text = " | ".join(evidence_parts)

        source_label = "FDA_Device" if is_device else "FDA_Drug"

        return Finding(
            source=source_label,
            firm_name=firm_name,
            finding_date=finding_date,
            url=url,
            subject=recall_number or f"{source_label} Recall",
            evidence_text=evidence_text,
            evidence_quote=reason[:500] if reason else "",
        )

    def _adverse_event_to_finding(self, item: dict) -> Finding:
        """Convert openFDA adverse event (FAERS) to Finding."""
        firm_name = (
            item.get("companynumb")
            or item.get("primarysourcecountry")
            or "Unknown"
        )
        patient = item.get("patient", {})
        drugs = patient.get("drug", [])
        drug_names = []
        for d in drugs:
            if isinstance(d, dict):
                name = d.get("medicinalproduct") or d.get("drugname") or ""
                if name:
                    drug_names.append(name)
        product = "; ".join(drug_names[:3])

        reactions = patient.get("reaction", [])
        reaction_terms = []
        for r in reactions:
            if isinstance(r, dict):
                term = r.get("reactionmeddrapt") or ""
                if term:
                    reaction_terms.append(term)
        reason = "; ".join(reaction_terms[:5])

        receipt_date = item.get("receiptdate") or ""
        if receipt_date and len(receipt_date) >= 8:
            try:
                finding_date = f"{receipt_date[:4]}-{receipt_date[4:6]}-{receipt_date[6:8]}"
            except Exception:
                finding_date = None
        else:
            finding_date = None

        safety_report_id = item.get("safetyreportid") or ""
        url = f"https://api.fda.gov/drug/event.json?search=safetyreportid:{safety_report_id}"

        evidence_parts = []
        if product:
            evidence_parts.append(f"Drug: {product}")
        if reason:
            evidence_parts.append(f"Reactions: {reason}")
        if item.get("serious"):
            evidence_parts.append("Serious: Yes")
        if item.get("outcome"):
            evidence_parts.append(f"Outcome: {item['outcome']}")
        evidence_text = " | ".join(evidence_parts)

        return Finding(
            source="FDA_FAERS",
            firm_name=firm_name,
            finding_date=finding_date,
            url=url,
            subject=f"FAERS Report {safety_report_id}",
            evidence_text=evidence_text,
            evidence_quote=reason[:500] if reason else "",
        )

    async def scrape_all(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        max_records: int = 10000,
    ) -> list[Finding]:
        """Full pull: fetch all enforcement + adverse events in date range."""
        all_findings = []

        for endpoint_type in ["enforcement", "adverse_events", "device_enforcement"]:
            if len(all_findings) >= max_records:
                break

            skip = 0
            page_size = 1000
            while len(all_findings) < max_records:
                if endpoint_type == "enforcement":
                    findings = await self.fetch_enforcement(from_date, to_date, page_size, skip)
                elif endpoint_type == "adverse_events":
                    findings = await self.fetch_adverse_events(from_date, to_date, page_size, skip)
                else:
                    findings = await self.fetch_device_enforcement(from_date, to_date, page_size, skip)

                if not findings:
                    break

                all_findings.extend(findings)
                if len(findings) < page_size:
                    break
                skip += page_size
                await asyncio.sleep(0.1)

        return all_findings[:max_records]

    async def search(self, browser, firm_name: str, limit: int = 10) -> list[Finding]:
        """Search for a specific firm across openFDA endpoints."""
        all_findings = []

        for endpoint_type in ["enforcement", "adverse_events", "device_enforcement"]:
            if len(all_findings) >= limit:
                break

            skip = 0
            page_size = min(100, limit)
            while len(all_findings) < limit:
                if endpoint_type == "enforcement":
                    findings = await self.fetch_enforcement(
                        from_date="2022-01-01",
                        to_date=datetime.utcnow().strftime("%Y-%m-%d"),
                        limit=page_size,
                        skip=skip,
                    )
                elif endpoint_type == "adverse_events":
                    findings = await self.fetch_adverse_events(
                        from_date="2022-01-01",
                        to_date=datetime.utcnow().strftime("%Y-%m-%d"),
                        limit=page_size,
                        skip=skip,
                    )
                else:
                    findings = await self.fetch_device_enforcement(
                        from_date="2022-01-01",
                        to_date=datetime.utcnow().strftime("%Y-%m-%d"),
                        limit=page_size,
                        skip=skip,
                    )

                matched = [
                    f for f in findings
                    if firm_name.lower() in (f.firm_name or "").lower()
                ]
                all_findings.extend(matched)

                if len(findings) < page_size:
                    break
                skip += page_size
                await asyncio.sleep(0.1)

        return all_findings[:limit]


class RateLimitError(Exception):
    pass


class APIError(Exception):
    pass


try:
    import aiohttp
except ImportError:
    aiohttp = None