import re
from urllib.parse import quote

from .base import Finding, EnrichmentAdapter


def _iso_date(value: str) -> str | None:
    """'MM/DD/YYYY' -> 'YYYY-MM-DD', else None."""
    m = re.match(r"\s*(\d{2})/(\d{2})/(\d{4})\s*", value)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return None


# Server-side DataTables AJAX endpoint backing the warning letters table. The
# static page only renders ~11 nav rows; the real rows (3,651 total) come from
# this Drupal views endpoint. Parameters mirror the datatables config found in
# drupalSettings on the page.
_FDA_DT_URL = "https://www.fda.gov/datatables/views/ajax"
_FDA_DT_VIEW = {
    "view_base_path": ("inspections-compliance-enforcement-and-criminal-"
                       "investigations/compliance-actions-and-activities/"
                       "warning-letters/datatables-data"),
    "view_display_id": "warning_letter_solr_block",
    "view_dom_id": "cbb39a970ac35379ec6068ffd417e25dfea4003a33db592bfbe06628e2419a10",
    "view_name": "warning_letter_solr_index",
    "view_path": "/inspections-compliance-enforcement-and-criminal-"
                 "investigations/compliance-actions-and-activities/"
                 "warning-letters",
}
_EMAIL_DATE_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})')
_LINK_RE = re.compile(r'<a href="([^"]+)">(.*?)</a>', re.S)


def _dt_row_to_finding(cells: list[str]) -> Finding | None:
    """Parse one server-side DataTables row into a Finding.

    Columns: 0 posted date, 1 letter issue date, 2 company (+ letter link),
    3 issuing office, 4 subject, 5 response letter, 6 closeout letter.
    """
    if len(cells) < 5 or not cells[2]:
        return None
    posted = _EMAIL_DATE_RE.search(cells[0])
    company_html = cells[2]
    link = _LINK_RE.search(company_html)
    company = (link.group(2) if link else company_html).strip()
    href = link.group(1) if link else ""
    if href.startswith("/"):
        href = f"https://www.fda.gov{href}"
    subject = cells[4].strip() if len(cells) > 4 else ""
    office = cells[3].strip() if len(cells) > 3 else ""
    snippet = " | ".join(x for x in (posted.group(1) if posted else "",
                                     company, office, subject) if x)
    return Finding(
        source="FDA",
        firm_name=company,
        finding_date=posted.group(1) if posted else None,
        url=href,
        subject=subject,
        evidence_text=snippet,
    )


def _letter_body(html: str, max_len: int = 6000) -> str:
    """Strip a FDA warning-letter page down to the letter body text.

    The letter proper runs from 'Dear ...' to the closing
    ('If you have any questions regarding this letter'), which keeps the
    evidence short and on-point for LLM classification.
    """
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    start = text.lower().find("dear ")
    if start == -1:
        start = 0
    cut = text.find("If you have any questions regarding this letter", start)
    if cut == -1:
        cut = len(text)
    body = text[start:cut].strip()
    if len(body) > max_len:
        body = body[:max_len]
    return body


def _letter_page(url: str, timeout: int = 60) -> str | None:
    """Fetch a warning-letter detail page and return the letter body text."""
    import requests
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        return _letter_body(r.text)
    except Exception:  # noqa: BLE001
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

    def scrape_all(self, from_date: str = "2022-01-01",
                   to_date: str = "2026-12-31",
                   page_size: int = 100) -> list[Finding]:
        """Bulk-pull every FDA warning letter via the server-side DataTables
        AJAX endpoint (no browser needed), filtered by ``posted`` date.

        Returns Findings whose ``evidence_text`` is the summary row and whose
        ``url`` links to the letter detail page (the body is fetched lazily by
        the per-company link step — 3,600+ letter fetches up front is wasteful).
        """
        import requests

        findings: list[Finding] = []
        records_total = None
        start = 0
        while True:
            params = {
                "_drupal_ajax": "1",
                "_wrapper_format": "drupal_ajax",
                "pager_element": "0",
                "view_args": "",
                **_FDA_DT_VIEW,
                "start": str(start),
                "length": str(page_size),
                "search[value]": "",
                "search[regex]": "false",
                "order[0][column]": "0",
                "order[0][dir]": "desc",
            }
            r = requests.get(_FDA_DT_URL, params=params, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0",
                                      "X-Requested-With": "XMLHttpRequest"})
            r.raise_for_status()
            data = r.json()
            rows = data.get("data") or []
            if records_total is None:
                records_total = int(data.get("recordsTotal") or 0)
            for cells in rows:
                f = _dt_row_to_finding(cells)
                if f is None:
                    continue
                if f.finding_date and from_date <= f.finding_date <= to_date:
                    findings.append(f)
            if not rows or start + len(rows) >= records_total:
                break
            start += len(rows)

        return findings

    @staticmethod
    def fetch_letter_body(url: str) -> str | None:
        """Fetch and cache the full body of one warning letter."""
        return _letter_page(url)

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
