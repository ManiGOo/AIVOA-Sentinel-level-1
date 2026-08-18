import asyncio
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .base import Finding, EnrichmentAdapter


class EMAUPDAdapter(EnrichmentAdapter):
    """EMA Union Product Database (UPD) API adapter.

    Fetches nationally authorised product data across EU member states.
    Covers manufacturing authorisations, GMP certificates, and
    regulatory actions from national competent authorities (NCAs).

    Public read-only API — no authentication required.
    """

    source = "EMA_UPD"
    base_url = "https://www.ema.europa.eu/api"

    def __init__(self):
        self.session = None

    async def _get_session(self):
        if self.session is None:
            import aiohttp
            headers = {"User-Agent": "AIVOA-Sentinel/1.0"}
            self.session = aiohttp.ClientSession(headers=headers)
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    def _build_url(self, endpoint: str, params: dict) -> str:
        query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        return f"{self.base_url}{endpoint}?{query}"

    async def _fetch_page(self, url: str) -> dict | list | None:
        session = await self._get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 429:
                    raise RateLimitError("Rate limited")
                if resp.status != 200:
                    text = await resp.text()
                    raise APIError(f"HTTP {resp.status}: {text[:200]}")
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return await resp.json()
                else:
                    return await resp.text()
        except asyncio.TimeoutError:
            raise APIError("Request timeout")
        except aiohttp.ClientError as e:
            raise APIError(f"Network error: {e}")

    async def fetch_gmp_noncompliance(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch GMP non-compliance statements from national authorities."""
        endpoint = "/gmp-non-compliance"
        params = {
            "date_from": from_date,
            "date_to": to_date,
            "limit": str(min(limit, 100)),
            "offset": str(skip),
        }
        url = self._build_url(endpoint, params)
        data = await self._fetch_page(url)
        if not data:
            return []

        findings = []
        items = data.get("results", data) if isinstance(data, dict) else data
        for item in items:
            findings.append(self._gmp_to_finding(item))
        return findings

    async def fetch_authorised_medicines(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch nationally authorised medicinal products in the date range."""
        endpoint = "/medicines/national"
        params = {
            "authorisation_date_from": from_date,
            "authorisation_date_to": to_date,
            "limit": str(min(limit, 100)),
            "offset": str(skip),
        }
        url = self._build_url(endpoint, params)
        data = await self._fetch_page(url)
        if not data:
            return []

        findings = []
        items = data.get("results", data) if isinstance(data, dict) else data
        for item in items:
            findings.append(self._medicine_to_finding(item))
        return findings

    async def fetch_variation_notifications(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch Type IA/IB/II variation notifications."""
        endpoint = "/variations"
        params = {
            "submission_date_from": from_date,
            "submission_date_to": to_date,
            "limit": str(min(limit, 100)),
            "offset": str(skip),
        }
        url = self._build_url(endpoint, params)
        data = await self._fetch_page(url)
        if not data:
            return []

        findings = []
        items = data.get("results", data) if isinstance(data, dict) else data
        for item in items:
            findings.append(self._variation_to_finding(item))
        return findings

    def _gmp_to_finding(self, item: dict) -> Finding:
        """Convert GMP non-compliance statement to Finding."""
        site_name = item.get("site_name") or item.get("company_name") or "Unknown"
        country = item.get("country") or item.get("nc") or ""
        report_number = item.get("report_number") or item.get("reference") or ""
        issue_date = item.get("issue_date") or item.get("date") or ""
        reason = item.get("reason") or item.get("nature_of_action") or ""
        status = item.get("status") or ""

        if issue_date:
            try:
                finding_date = issue_date[:10] if len(issue_date) >= 10 else None
            except Exception:
                finding_date = None
        else:
            finding_date = None

        url = (
            f"https://eudragmdp.ema.europa.eu/inspections/gmpc/"
            f"searchGMPNonCompliance.do?report_number={report_number}"
            if report_number else ""
        )

        evidence_parts = []
        if country:
            evidence_parts.append(f"Country: {country}")
        if reason:
            evidence_parts.append(f"Reason: {reason}")
        if status:
            evidence_parts.append(f"Status: {status}")
        if report_number:
            evidence_parts.append(f"Report: {report_number}")
        evidence_text = " | ".join(evidence_parts)

        return Finding(
            source=self.source,
            firm_name=site_name,
            finding_date=finding_date,
            url=url,
            subject=f"GMP Non-Compliance: {report_number}",
            evidence_text=evidence_text,
            evidence_quote=reason[:500] if reason else "",
        )

    def _medicine_to_finding(self, item: dict) -> Finding:
        """Convert nationally authorised medicine to Finding."""
        product_name = item.get("product_name") or item.get("name") or "Unknown"
        ma_holder = item.get("marketing_authorisation_holder") or item.get("mah") or ""
        country = item.get("country") or item.get("nc") or ""
        auth_date = item.get("authorisation_date") or item.get("date") or ""
        status = item.get("status") or ""
        active_substances = item.get("active_substances", [])
        substance_names = [s.get("name", "") for s in active_substances if isinstance(s, dict)]
        product_info = "; ".join(filter(None, substance_names))

        if auth_date:
            try:
                finding_date = auth_date[:10] if len(auth_date) >= 10 else None
            except Exception:
                finding_date = None
        else:
            finding_date = None

        url = f"https://www.ema.europa.eu/en/medicines/{product_name.lower().replace(' ', '-')}"

        evidence_parts = []
        if product_info:
            evidence_parts.append(f"Active substances: {product_info}")
        if ma_holder:
            evidence_parts.append(f"MAH: {ma_holder}")
        if country:
            evidence_parts.append(f"Country: {country}")
        if status:
            evidence_parts.append(f"Status: {status}")
        evidence_text = " | ".join(evidence_parts)

        return Finding(
            source=self.source,
            firm_name=ma_holder or "Unknown",
            finding_date=finding_date,
            url=url,
            subject=f"EU National Auth: {product_name}",
            evidence_text=evidence_text,
            evidence_quote=product_info[:500] if product_info else "",
        )

    def _variation_to_finding(self, item: dict) -> Finding:
        """Convert variation notification to Finding."""
        product_name = item.get("product_name") or item.get("medicine") or ""
        variation_type = item.get("variation_type") or item.get("type") or ""
        scope = item.get("scope") or item.get("description") or ""
        submission_date = item.get("submission_date") or item.get("date") or ""
        ma_holder = item.get("marketing_authorisation_holder") or item.get("mah") or ""
        country = item.get("country") or item.get("nc") or ""

        if submission_date:
            try:
                finding_date = submission_date[:10] if len(submission_date) >= 10 else None
            except Exception:
                finding_date = None
        else:
            finding_date = None

        url = f"https://www.ema.europa.eu/en/medicines/{product_name.lower().replace(' ', '-')}/variations"

        evidence_parts = []
        if variation_type:
            evidence_parts.append(f"Type: {variation_type}")
        if scope:
            evidence_parts.append(f"Scope: {scope}")
        if country:
            evidence_parts.append(f"Country: {country}")
        evidence_text = " | ".join(evidence_parts)

        return Finding(
            source=self.source,
            firm_name=ma_holder or "Unknown",
            finding_date=finding_date,
            url=url,
            subject=f"Variation: {variation_type}",
            evidence_text=evidence_text,
            evidence_quote=scope[:500] if scope else "",
        )

    async def scrape_all(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        max_records: int = 10000,
    ) -> list[Finding]:
        """Full pull: GMP non-compliance + national authorisations + variations.

        The EMA UPD gateway requires a registered API key; without one the
        endpoints return 404. We catch those and return whatever we can so the
        workflow never crashes on a missing credential.
        """
        all_findings = []

        for endpoint_type in ["gmp_noncompliance", "authorised_medicines", "variation_notifications"]:
            if len(all_findings) >= max_records:
                break

            skip = 0
            page_size = 100
            while len(all_findings) < max_records:
                try:
                    if endpoint_type == "gmp_noncompliance":
                        findings = await self.fetch_gmp_noncompliance(from_date, to_date, page_size, skip)
                    elif endpoint_type == "authorised_medicines":
                        findings = await self.fetch_authorised_medicines(from_date, to_date, page_size, skip)
                    else:
                        findings = await self.fetch_variation_notifications(from_date, to_date, page_size, skip)
                except APIError as e:
                    print(f"EMA UPD {endpoint_type} skipped: {e}")
                    break

                if not findings:
                    break

                all_findings.extend(findings)
                if len(findings) < page_size:
                    break
                skip += page_size
                await asyncio.sleep(0.1)

        return all_findings[:max_records]

    async def search(self, browser, firm_name: str, limit: int = 10) -> list[Finding]:
        """Search for a specific firm across EMA UPD endpoints."""
        all_findings = []

        for endpoint_type in ["gmp_noncompliance", "authorised_medicines"]:
            if len(all_findings) >= limit:
                break

            skip = 0
            page_size = min(100, limit)
            while len(all_findings) < limit:
                if endpoint_type == "gmp_noncompliance":
                    findings = await self.fetch_gmp_noncompliance(
                        from_date="2022-01-01",
                        to_date=datetime.utcnow().strftime("%Y-%m-%d"),
                        limit=page_size,
                        skip=skip,
                    )
                else:
                    findings = await self.fetch_authorised_medicines(
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