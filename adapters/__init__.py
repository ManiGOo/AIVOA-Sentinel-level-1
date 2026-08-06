from .base import Finding, EnrichmentAdapter
from .fda import FDAWarningLetterAdapter
from .eudragmdp import EudraGMDPAdapter

REGULATORY_SOURCES: dict[str, type[EnrichmentAdapter]] = {
    "fda": FDAWarningLetterAdapter,
    "eudragmdp": EudraGMDPAdapter,
}

__all__ = ["Finding", "EnrichmentAdapter", "REGULATORY_SOURCES",
           "FDAWarningLetterAdapter", "EudraGMDPAdapter"]
