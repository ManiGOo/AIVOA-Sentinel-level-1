import asyncio
import random
import re
from datetime import datetime

from .base import Finding, EnrichmentAdapter

EUDRA_SEARCH_URL = ("https://eudragmdp.ema.europa.eu/inspections/gmpc/"
                    "searchGMPNonCompliance.do")


def _statement_section(text: str) -> str:
    """Isolate the statement body, dropping cookie banner / nav / footer."""
    start = text.find("STATEMENT OF NON-COMPLIANCE")
    if start == -1:
        start = text.find("Report No:")
    if start == -1:
        start = 0
    cut = text.find("The EudraGMDP database is maintained", start)
    if cut == -1:
        cut = len(text)
    return re.sub(r"\n{2,}", "\n", text[start:cut]).strip()


class EudraGMDPAdapter(EnrichmentAdapter):
    """EudraGMDP GMP non-compliance statements (public, no login).

    EMA does not grant scraping accounts, but the non-compliance search is a
    public date-range form (no firm-name field). We submit a wide date range,
    read the rendered results table (ibody table with 'Report Number' header),
    keep rows whose *Site Name* cell matches the firm, then navigate the same
    page to each matching statement's drilldown (the session is URL/cookie
    scoped to the page — opening a fresh page drops it and yields the error
    page) to capture the statement body. Throttled with random 2-5s delays.
    """

    source = "EudraGMDP"
    search_url = EUDRA_SEARCH_URL
    default_from_date = "2019-01-01"

    async def _results_table(self, page):
        bodies = page.locator("table.ibody")
        for b in range(await bodies.count()):
            head = await bodies.nth(b).locator("tr").first.locator("td,th").all_inner_texts()
            if any("Report Number" in h for h in head):
                return bodies.nth(b)
        return None

    async def search(self, browser, firm_name: str, limit: int = 10,
                     from_date: str | None = None,
                     to_date: str | None = None) -> list[Finding]:
        from_date = from_date or self.default_from_date
        to_date = to_date or datetime.utcnow().strftime("%Y-%m-%d")

        page = await browser.new_page()
        findings = []
        try:
            await page.goto(self.search_url, wait_until="domcontentloaded",
                            timeout=60000)
            await page.fill("input[name=fromDate]", from_date)
            await page.fill("input[name=toDate]", to_date)
            await page.evaluate("document.forms.GMPNCSearchForm.submit()")
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            table = await self._results_table(page)
            if table is None:
                return findings

            rows = table.locator("tr")
            matches = []
            for i in range(1, await rows.count()):  # skip header row
                cells = [c.strip() for c in await rows.nth(i).locator("td").all_inner_texts()]
                if len(cells) < 11:
                    continue
                site = cells[3]
                if firm_name.lower() not in site.lower():
                    continue
                link = rows.nth(i).locator("a").first
                href = await link.evaluate("el => el.href") if await link.count() else ""
                matches.append((site, cells[10], href))

            for site, issue_date, href in matches:
                if not href:
                    continue
                await asyncio.sleep(random.uniform(2.0, 5.0))
                await page.goto(href, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
                body = await page.locator("body").inner_text()
                text = _statement_section(body)
                findings.append(Finding(
                    source=self.source,
                    firm_name=site,
                    finding_date=issue_date,
                    url=href,
                    evidence_text=text,
                ))
                if len(findings) >= limit:
                    break

            return findings
        finally:
            await page.close()

    async def scrape_all(self, browser, from_date: str = "2022-01-01",
                         to_date: str = "2026-12-31", limit: int = 1000,
                         heartbeat=None) -> list[Finding]:
        """Full-pull every GMP non-compliance statement in the date range.

        Unlike ``search`` there is no firm filter — the date-range form returns
        every statement, and we drill into each one's statement body (the
        session is URL/cookie scoped to the page, so we stay on the same tab).
        ``heartbeat`` (optional callable) lets the caller keep Temporal alive
        during the throttled drilldowns.
        """
        page = await browser.new_page()
        findings = []
        try:
            await page.goto(self.search_url, wait_until="domcontentloaded",
                            timeout=60000)
            await page.fill("input[name=fromDate]", from_date)
            await page.fill("input[name=toDate]", to_date)
            await page.evaluate("document.forms.GMPNCSearchForm.submit()")
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            table = await self._results_table(page)
            if table is None:
                return findings

            rows = table.locator("tr")
            matches = []
            for i in range(1, await rows.count()):  # skip header row
                cells = [c.strip() for c in await rows.nth(i).locator("td").all_inner_texts()]
                if len(cells) < 11:
                    continue
                link = rows.nth(i).locator("a").first
                href = await link.evaluate("el => el.href") if await link.count() else ""
                matches.append((cells[3], cells[10], href))

            for idx, (site, issue_date, href) in enumerate(matches):
                if heartbeat:
                    heartbeat({"scraped": idx, "total": len(matches)})
                if not href:
                    continue
                await asyncio.sleep(random.uniform(2.0, 5.0))
                await page.goto(href, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
                body = await page.locator("body").inner_text()
                text = _statement_section(body)
                findings.append(Finding(
                    source=self.source,
                    firm_name=site,
                    finding_date=issue_date,
                    url=href,
                    subject=issue_date,
                    evidence_text=text,
                ))
                if len(findings) >= limit:
                    break

            return findings
        finally:
            await page.close()
