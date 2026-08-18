from .base import Finding, EnrichmentAdapter
from .fda import FDAWarningLetterAdapter
from .eudragmdp import EudraGMDPAdapter
from .openfda import OpenFDAAdapter
from .ema_epi import EMAePIAdapter
from .ema_upd import EMAUPDAdapter

REGULATORY_SOURCES: dict[str, type[EnrichmentAdapter]] = {
    "fda": FDAWarningLetterAdapter,
    "eudragmdp": EudraGMDPAdapter,
    "openfda": OpenFDAAdapter,
    "ema_epi": EMAePIAdapter,
    "ema_upd": EMAUPDAdapter,
}

API_SOURCES: dict[str, type[EnrichmentAdapter]] = {
    "openfda": OpenFDAAdapter,
    "ema_epi": EMAePIAdapter,
    "ema_upd": EMAUPDAdapter,
}

WEB_SCRAPING_SOURCES: dict[str, type[EnrichmentAdapter]] = {
    "fda": FDAWarningLetterAdapter,
    "eudragmdp": EudraGMDPAdapter,
}

__all__ = [
    "Finding", "EnrichmentAdapter", "REGULATORY_SOURCES",
    "API_SOURCES", "WEB_SCRAPING_SOURCES",
    "FDAWarningLetterAdapter", "EudraGMDPAdapter",
    "OpenFDAAdapter", "EMAePIAdapter", "EMAUPDAdapter",
]
