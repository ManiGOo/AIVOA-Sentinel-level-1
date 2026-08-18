import asyncio
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .base import Finding, EnrichmentAdapter


class EMAePIAdapter(EnrichmentAdapter):
    """EMA Electronic Product Information (ePI) API adapter.

    Fetches centrally authorised medicinal product data from EMA.
    Data is in HL7 FHIR JSON format.
    Public API, no authentication required for basic access.
    """

    source = "EMA_ePI"
    base_url = "https://epi.ema.europa.eu/api/v1"

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

    async def fetch_products(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch medicinal products authorised in the date range.

        The ePI API search supports filtering by authorisation date.
        """
        endpoint = "/medicinal-products"
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
            findings.append(self._product_to_finding(item))
        return findings

    async def fetch_product_variations(
        self,
        product_id: str,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
    ) -> list[Finding]:
        """Fetch regulatory variations for a specific product."""
        endpoint = f"/medicinal-products/{product_id}/variations"
        params = {
            "start_date_from": from_date,
            "start_date_to": to_date,
            "limit": "100",
        }
        url = self._build_url(endpoint, params)
        data = await self._fetch_page(url)
        if not data:
            return []

        findings = []
        items = data.get("results", data) if isinstance(data, dict) else data
        for item in items:
            findings.append(self._variation_to_finding(item, product_id))
        return findings

    async def fetch_referrals(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch Article 31/20 referrals (safety reviews) from EMA."""
        endpoint = "/referrals"
        params = {
            "start_date_from": from_date,
            "start_date_to": to_date,
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
            findings.append(self._referral_to_finding(item))
        return findings

    async def fetch_shortages(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        limit: int = 1000,
        skip: int = 0,
    ) -> list[Finding]:
        """Fetch medicine shortages from EMA."""
        endpoint = "/shortages"
        params = {
            "start_date_from": from_date,
            "start_date_to": to_date,
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
            findings.append(self._shortage_to_finding(item))
        return findings

    def _product_to_finding(self, item: dict) -> Finding:
        """Convert EMA ePI medicinal product to Finding."""
        name = item.get("invented_name") or item.get("product_name") or "Unknown"
        ma_holder = item.get("marketing_authorisation_holder") or item.get("mah_name") or ""
        auth_number = item.get("authorisation_number") or ""
        auth_date = item.get("authorisation_date") or item.get("valid_from") or ""
        status = item.get("authorisation_status") or ""
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

        url = f"https://www.ema.europa.eu/en/medicines/{name.lower().replace(' ', '-')}"
        if auth_number:
            url += f"?auth={auth_number}"

        evidence_parts = []
        if product_info:
            evidence_parts.append(f"Active substances: {product_info}")
        if ma_holder:
            evidence_parts.append(f"MAH: {ma_holder}")
        if status:
            evidence_parts.append(f"Status: {status}")
        if auth_number:
            evidence_parts.append(f"Auth: {auth_number}")
        evidence_text = " | ".join(evidence_parts)

        return Finding(
            source=self.source,
            firm_name=ma_holder or "Unknown",
            finding_date=finding_date,
            url=url,
            subject=f"EU Centralised Auth: {name}",
            evidence_text=evidence_text,
            evidence_quote=product_info[:500] if product_info else "",
        )

    def _variation_to_finding(self, item: dict, product_id: str) -> Finding:
        """Convert EMA variation to Finding."""
        variation_type = item.get("variation_type") or item.get("type") or ""
        scope = item.get("scope") or item.get("description") or ""
        start_date = item.get("start_date") or item.get("date") or ""
        status = item.get("status") or ""
        ma_holder = item.get("marketing_authorisation_holder") or ""

        if start_date:
            try:
                finding_date = start_date[:10] if len(start_date) >= 10 else None
            except Exception:
                finding_date = None
        else:
            finding_date = None

        url = f"https://www.ema.europa.eu/en/documents/variation-report/{product_id}"

        evidence_parts = []
        if variation_type:
            evidence_parts.append(f"Type: {variation_type}")
        if scope:
            evidence_parts.append(f"Scope: {scope}")
        if status:
            evidence_parts.append(f"Status: {status}")
        evidence_text = " | ".join(evidence_parts)

        return Finding(
            source="EMA_Variation",
            firm_name=ma_holder or "Unknown",
            finding_date=finding_date,
            url=url,
            subject=f"Variation: {variation_type}",
            evidence_text=evidence_text,
            evidence_quote=scope[:500] if scope else "",
        )

    def _referral_to_finding(self, item: dict) -> Finding:
        """Convert EMA referral (Article 31/20) to Finding."""
        procedure_number = item.get("procedure_number") or ""
        title = item.get("title") or item.get("subject") or ""
        start_date = item.get("start_date") or item.get("date") or ""
        status = item.get("status") or ""
        article = item.get("article") or ""
        ma_holders = item.get("marketing_authorisation_holders", [])
        holder_names = [h.get("name", "") for h in ma_holders if isinstance(h, dict)]
        firm_name = "; ".join(holder_names) if holder_names else "Unknown"

        if start_date:
            try:
                finding_date = start_date[:10] if len(start_date) >= 10 else None
            except Exception:
                finding_date = None
        else:
            finding_date = None

        url = f"https://www.ema.europa.eu/en/documents/referral/{procedure_number.lower().replace(' ', '-')}"

        evidence_parts = []
        if article:
            evidence_parts.append(f"Article: {article}")
        if title:
            evidence_parts.append(f"Subject: {title}")
        if status:
            evidence_parts.append(f"Status: {status}")
        evidence_text = " | ".join(evidence_parts)

        return Finding(
            source="EMA_Referral",
            firm_name=firm_name,
            finding_date=finding_date,
            url=url,
            subject=f"Referral {procedure_number}",
            evidence_text=evidence_text,
            evidence_quote=title[:500] if title else "",
        )

    def _shortage_to_finding(self, item: dict) -> Finding:
        """Convert EMA shortage to Finding."""
        product_name = item.get("product_name") or item.get("medicinal_product") or ""
        ma_holder = item.get("marketing_authorisation_holder") or item.get("mah") or ""
        shortage_type = item.get("shortage_type") or ""
        reason = item.get("reason") or ""
        start_date = item.get("start_date") or item.get("date") or ""
        status = item.get("status") or ""

        if start_date:
            try:
                finding_date = start_date[:10] if len(start_date) >= 10 else None
            except Exception:
                finding_date = None
        else:
            finding_date = None

        url = f"https://www.ema.europa.eu/en/medicines/shortages/{product_name.lower().replace(' ', '-')}"

        evidence_parts = []
        if product_name:
            evidence_parts.append(f"Product: {product_name}")
        if shortage_type:
            evidence_parts.append(f"Type: {shortage_type}")
        if reason:
            evidence_parts.append(f"Reason: {reason}")
        if status:
            evidence_parts.append(f"Status: {status}")
        evidence_text = " | ".join(evidence_parts)

        return Finding(
            source="EMA_Shortage",
            firm_name=ma_holder or "Unknown",
            finding_date=finding_date,
            url=url,
            subject=f"Shortage: {product_name}",
            evidence_text=evidence_text,
            evidence_quote=reason[:500] if reason else "",
        )

    async def scrape_all(
        self,
        from_date: str = "2022-01-01",
        to_date: str = "2026-12-31",
        max_records: int = 10000,
    ) -> list[Finding]:
        """Full pull: fetch products, referrals, shortages, variations.

        EMA ePI requires a registered API key (per the EMA developer portal);
        without one the endpoints return 404. We catch those and return
        whatever we can so the workflow never crashes on a missing credential.
        """
        all_findings = []

        for endpoint_type in ["products", "referrals", "shortages"]:
            if len(all_findings) >= max_records:
                break

            skip = 0
            page_size = 100
            while len(all_findings) < max_records:
                try:
                    if endpoint_type == "products":
                        findings = await self.fetch_products(from_date, to_date, page_size, skip)
                    elif endpoint_type == "referrals":
                        findings = await self.fetch_referrals(from_date, to_date, page_size, skip)
                    else:
                        findings = await self.fetch_shortages(from_date, to_date, page_size, skip)
                except APIError as e:
                    print(f"EMA ePI {endpoint_type} skipped: {e}")
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
        """Search for a specific firm across EMA ePI endpoints."""
        all_findings = []

        for endpoint_type in ["products", "referrals", "shortages"]:
            if len(all_findings) >= limit:
                break

            skip = 0
            page_size = min(100, limit)
            while len(all_findings) < limit:
                if endpoint_type == "products":
                    findings = await self.fetch_products(
                        from_date="2022-01-01",
                        to_date=datetime.utcnow().strftime("%Y-%m-%d"),
                        limit=page_size,
                        skip=skip,
                    )
                elif endpoint_type == "referrals":
                    findings = await self.fetch_referrals(
                        from_date="2022-01-01",
                        to_date=datetime.utcnow().strftime("%Y-%m-%d"),
                        limit=page_size,
                        skip=skip,
                    )
                else:
                    findings = await self.fetch_shortages(
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