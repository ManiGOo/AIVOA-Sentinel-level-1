from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Finding:
    source: str
    firm_name: str
    finding_date: str | None = None
    url: str = ""
    evidence_text: str = ""
    evidence_quote: str = ""
    classification: dict = field(default_factory=dict)
    paper_qms_score: int = 0
    mfr_key: str = ""   # normalized raw manufacturer (links to regulatory_events)


class EnrichmentAdapter(ABC):
    source: str = ""

    @abstractmethod
    async def search(self, browser, firm_name: str, limit: int = 10) -> list[Finding]:
        """Search one regulatory source for findings about ``firm_name``.

        ``browser`` is an open Playwright Browser (Chromium) that the adapter
        drives and must NOT close. Returns a list of Findings.
        """
