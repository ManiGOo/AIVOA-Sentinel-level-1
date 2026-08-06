import os

from .base import Finding, EnrichmentAdapter


class EudraGMDPAdapter(EnrichmentAdapter):
    """EudraGMDP GMP non-compliance statements (EU).

    The search is behind a free-account login (credentials from env
    ``EUDRA_GMDP_USER`` / ``EUDRA_GMDP_PASS``). This adapter logs in when
    redirected, then searches the non-compliance search page and reads the
    rendered results table. Marked best-effort until the login flow is
    verified against a live account.
    """

    source = "EudraGMDP"
    search_url = ("https://eudragmdp.ema.europa.eu/inspections/gmpc/"
                  "searchGMPNonCompliance.do")
    login_url = ("https://eudragmdp.ema.europa.eu/inspections/login.do")

    async def search(self, browser, firm_name: str, limit: int = 10) -> list[Finding]:
        page = await browser.new_page()
        findings = []
        try:
            await page.goto(self.search_url, wait_until="domcontentloaded",
                            timeout=90000)

            if "login" in page.url or await page.locator(
                    "input[name=j_username]").count():
                username = os.environ.get("EUDRA_GMDP_USER", "")
                password = os.environ.get("EUDRA_GMDP_PASS", "")
                if not username or not password:
                    return findings  # caller reports as an empty/skipped source
                await page.goto(self.login_url, wait_until="domcontentloaded",
                                timeout=90000)
                await page.fill("input[name=j_username]", username)
                await page.fill("input[name=j_password]", password)
                await page.click("button[type=submit], input[type=submit]")
                await page.wait_for_load_state("networkidle", timeout=60000)
                await page.goto(self.search_url, wait_until="domcontentloaded",
                                timeout=90000)

            query = page.locator("input[type=text], input[name*=search]").first
            await query.wait_for(state="visible", timeout=20000)
            await query.fill(firm_name)
            submit = page.locator("button[type=submit], input[type=submit]").first
            if await submit.count():
                await submit.click()
                await page.wait_for_load_state("networkidle", timeout=60000)
            await page.wait_for_timeout(2000)

            rows = page.locator("table tbody tr, table tr").first.locator("tr")
            count = await rows.count()
            for i in range(count):
                cells = await rows.nth(i).locator("td").all_inner_texts()
                cells = [c.strip() for c in cells]
                if not cells or not any(firm_name.lower() in c.lower() for c in cells):
                    continue
                findings.append(Finding(
                    source=self.source,
                    firm_name=cells[0] if cells else firm_name,
                    finding_date=None,
                    url=page.url,
                    evidence_text=" | ".join(cells),
                ))
                if len(findings) >= limit:
                    break

            return findings
        finally:
            await page.close()
