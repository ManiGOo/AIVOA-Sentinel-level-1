import re

# Address markers appear right after a trading name; cut the name there.
# Deliberately excludes state/city/district words because they can be part of
# a company name (e.g. "Gujarat Ambuja Exports Ltd", "Syncom ... (India) Ltd").
ADDRESS_MARKERS = re.compile(
    r"(?i)"
    r"\bplot\s*(?:no\.?|no)?\s*:?|"
    r"\bkhasra\s*(?:no\.?)?|"
    r"\bsurvey\s*(?:no\.?)?|"
    r"\bsurvery\s*(?:no\.?)?|"
    r"\bk\.?\s*h\.?\s*no\.?|"
    r"\bs\.?\s*(?:f\.?\s*)?no\.?|"
    r"\bsector[- ]?\d|"
    r"\bphase[- ]?[iv\d]|"
    r"\bvill[e]?\b|"
    r"\bvillage\b|"
    r"\btehsil\b|"
    r"\btaluk\b|"
    r"\bdistt?\.?\b|"
    r"\bn[h]-?\s*\d|"
    r"\broad\b|\brd\.?\b|\bstreet\b|"
    r"\bgali\b|\bbazar\b|\bmarket\b|\bfloor\b|\bchawri\b|"
    r"\bbehind\b|\bnear\b|"
    r"\bp\.?o\.?\b|\bpo\s*:|"
    r"\b(?:gidc|sidcul|hpsidc|hsiidc|sidco|pipsdic|ida|i\.?d\.?a|epip|sldc|hsidc)\b|"
    r"\bindustrial\s*(?:area|estate|park|growth)\b|"
    r"\bindl\.?\s*area\b|"
    r"\bfactory\b|\bfact[.:]|\bat\s*[:.]|"
    r"\b\#\s?\d|"
    r"\br[^ ]*-?\s*\d{3,}|"
    r"\b\d{6}\b|"
    r"\b(?:estate|colony|extension)\b"
)

# Company-name suffix tokens. After address-cutting, the name is truncated
# again at the LAST occurrence of one of these (kept) to drop trailing noise
# like "198-A," or "Unit-II".
COMPANY_TOKENS = (
    r"ltd\.?|limited|pvt\.?|private|llp|corp\.?|corporation|inc\.?|co\.?|company|"
    r"pharma(?:ceuticals?|chem)?|healthcare|health\s*care|"
    r"laborator(?:ies|y)\b|labs?\b|"
    r"biotec(h)?|life\s*science\s*|bioscience|medisciences|medicare|medicines?|"
    r"remedies|organics|drugs|chemicals?|industr(?:ies|y)|enterprises?|"
    r"trading|traders?|exports?|imports?|formulations?|sciences?|naturals|"
    r"ayurved(?:a)?|capsules?|specialit(?:ies|y)|surgicals?|parenterals?|"
    r"care|lifesciences|genetics?|novitas|halden|curation|health|"
    r"scientific|biologicals?|botanicals?|diagnostics?|devices?|medical"
)
COMPANY_SUFFIX_RE = re.compile(rf"(?i)\b({COMPANY_TOKENS})\b")
# Business-type tokens only (excludes legal-entity words like private/ltd) —
# used to detect a *second* company chunk in _company_cut.
BUSINESS_TOKEN_RE = re.compile(
    rf"(?i)\b(pharma(?:ceuticals?|chem)?|healthcare|health\s*care|laborator(?:ies|y)\b|labs?\b"
    rf"|biotec(h)?|life\s*science\s*|lifesciences|bioscience|medicare|medicines?|remedies"
    rf"|organics|drugs|chemicals?|industr(?:ies|y)|enterprises?|trading|traders?|exports?"
    rf"|imports?|formulations?|sciences?|naturals|ayurveda|capsules?|specialit(?:ies|y)"
    rf"|surgicals?|parenterals?|care|genetics?|novitas|halden|curation|health|scientific"
    rf"|biologicals?|botanicals?|diagnostics?|devices?|medical)\b"
)
COMPANY_SUFFIX_STRICT_RE = re.compile(
    rf"(?i)\b(ltd\.?|limited|pvt\.?|private|llp|corp\.?|corporation|inc\.?|co\.?|company"
    rf"|pharma(?:ceuticals?|chem)?|healthcare|laborator(?:ies|y)\b|labs?\b|biotec(h)?"
    rf"|lifesciences|remedies|organics|drugs|chemicals?|industr(?:ies|y)|enterprises?"
    rf"|trading|formulations?|sciences?|surgicals?|parenterals?|care)\b"
)

LEAD_PREFIXES = re.compile(r"(?i)^(m/s\.?\s*|messrs\.?\s*|the\s+|mft\.?\s*by|mfg\s*by|mkd\.?\s*by|mktd\.?\s*by|mrkt\s*[:.]|manufactured\s*by|marketed\s*by)\s*")

PAREN = re.compile(r"\([^)]*\)")

PLACEHOLDERS = {
    "under investigation", "not mentioned", "not known", "unknown",
    "not available", "nil", "n/a", "na", "not disclosed",
}


def _norm(raw: str) -> str:
    raw = raw.replace("\n", ", ").replace("\t", " ")
    return re.sub(r"\s+", " ", raw).strip(" ,;:-")


def _company_cut(text: str) -> str:
    """Truncate at the last company-token (keeping it). If a second, separate
    company chunk follows a first one (e.g. "Cureza Healthcare Pvt Ltd. Cureza
    Healthcare Ltd"), keep only the first chunk."""
    ms = list(COMPANY_SUFFIX_RE.finditer(text))
    if not ms:
        return text.rstrip(" ,;:-")
    out = text[: ms[-1].end()].rstrip(" ,;:-")
    if len(ms) > 1 and BUSINESS_TOKEN_RE.search(text[ms[0].end(): ms[-1].start()]):
        out = text[: ms[0].end()].rstrip(" ,;:-")
    return out


def _strip_noise(text: str) -> str:
    text = _norm(text)
    text = ADDRESS_MARKERS.split(text, maxsplit=1)[0]
    text = _company_cut(text)
    text = PAREN.sub("", text)
    text = LEAD_PREFIXES.sub("", text)
    text = _norm(text)
    text = re.sub(r"\s+[-&,;:.]+\s+", " ", text)
    text = text.strip(" ,;:.-&")
    text = re.sub(r"\s+", " ", text)
    return text


def strip_legal_suffix(name: str) -> str:
    """Drop a trailing legal-entity suffix for search recall.

    e.g. "Cipla Ltd" -> "Cipla"; "Avlab S.r.l." (unmatched) is left intact.
    """
    s = name.strip().rstrip("., ")
    return re.sub(
        r"(?i)\s+(?:(?:pvt\.?|private)\s+)?(?:ltd\.?|limited|llp|inc\.?|corporation|corp\.?)\s*$",
        "", s).strip()


def clean_company_name(raw: str) -> str:
    """Best-effort clean trading name from a CDSCO manufacturer string."""
    if not raw:
        return ""
    raw = _norm(raw)
    m = re.search(r"(?i)unit\s+of\s+([^(),]+)", raw)
    if not m:
        m = re.search(r"(?i)(?:mkd\.?\s*by|marketed\s*by|mktd\.?\s*by)\s+([A-Z][^,]+)", raw)
    if not m:
        m = re.search(r"(?i)(?:mfg\s*by|mft\.?\s*by|manufactured\s*by)\s+([A-Z][^,]+)", raw)
    name = _strip_noise(m.group(1)) if m else _strip_noise(raw)
    return name


def looks_like_company(name: str, llm_trusted: bool = False) -> bool:
    """Reject address leftovers / placeholders / empty noise.

    ``llm_trusted`` relaxes the 2-word minimum so a single-word brand rescued
    by the LLM extractor (e.g. "Dentiglow") is accepted.
    """
    if not name:
        return False
    low = name.strip().lower()
    if low in PLACEHOLDERS or len(name.strip()) < 3 or len(name.strip()) > 90:
        return False
    if any(k in low for k in ("road", "plot", "village", "distt", "tehsil",
                              "gidc", "nagar", "pincode", "taluk", "vill")):
        return False
    if re.search(r"\b\d{6}\b", low):
        return False
    if not COMPANY_SUFFIX_STRICT_RE.search(name):
        if re.search(r"\d", name) or not re.search(r"^[A-Z][A-Za-z0-9 .\-&'()]+$", name):
            return False
        if len(name.strip().split()) < 2 and not llm_trusted:
            return False
    return True


def clean_company_names_batch(raw_names: list[str], llm_fallback: bool = True) -> list[str]:
    """Heuristic pass; LLM cleans the ones that still look invalid."""
    cleaned = []
    needs_llm = []
    idx_fail = []
    for i, raw in enumerate(raw_names):
        name = clean_company_name(raw)
        if looks_like_company(name):
            cleaned.append(name)
        else:
            cleaned.append("")
            if llm_fallback:
                needs_llm.append(raw)
                idx_fail.append(i)
    if needs_llm:
        try:
            from cognitive_engine import extract_company_names_batch
            llm_out = extract_company_names_batch(needs_llm)
            for j, name in enumerate(llm_out):
                name = clean_company_name(name or "")
                if looks_like_company(name, llm_trusted=True):
                    cleaned[idx_fail[j]] = name
        except Exception as e:  # noqa: BLE001
            print(f"LLM company-name fallback failed: {e}")
    return cleaned
