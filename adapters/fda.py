import re
from urllib.parse import quote

from .base import Finding, EnrichmentAdapter


def _iso_date(value: str) -> str | None:
    """'MM/DD/YYYY' -> 'YYYY-MM-DD', else None."""
    m = re.match(r"\s*(\d{2})/(\d{2})/(\d{4})\s*", value)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return None


class FDAWarningLetterAdapter(EnrichmentAdapter):
    """FDA Warning Letters.

    The results table is a server-rendered Drupal view (DataTables decorates
    it client-side); the exposed search form is a plain GET that filters via
    the ``search_api_fulltext`` query param. We navigate straight to the
    filtered URL and read the rendered table — no JS interaction needed.

    Columns: 0 posted date, 1 response date, 2 company (with letter link),
    3 issuing office, 4 subject, 5 product type, 7 evidence snippet. The
    search is fuzzy, so rows are kept only when the company cell matches.
    """

    source = "FDA"
    page_url = ("https://www.fda.gov/inspections-compliance-enforcement-and-"
                "criminal-investigations/compliance-actions-and-activities/"
                "warning-letters")

    async def search(self, browser, firm_name: str, limit: int = 10) -> list[Finding]:
        page = await browser.new_page()
        findings = []
        try:
            await page.goto(f"{self.page_url}?search_api_fulltext={quote(firm_name)}",
                            wait_until="domcontentloaded", timeout=60000)

            rows = page.locator("#datatable tbody tr")
            count = await rows.count()
            for i in range(count):
                cells = [c.strip() for c in await rows.nth(i).locator("td").all_inner_texts()]
                if len(cells) < 5:
                    continue
                company = cells[2]
                if firm_name.lower() not in company.lower():
                    continue  # fuzzy search can surface other firms
                link = rows.nth(i).locator("td").nth(2).locator("a").first
                href = (await link.get_attribute("href")) if await link.count() else None
                if href and href.startswith("/"):
                    href = f"https://www.fda.gov{href}"
                findings.append(Finding(
                    source=self.source,
                    firm_name=company,
                    finding_date=_iso_date(cells[0]),
                    url=href or "",
                    evidence_text=" | ".join(cells),
                ))
                if len(findings) >= limit:
                    break

            return findings
        finally:
            await page.close()
