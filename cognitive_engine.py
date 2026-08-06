import os
import json
import re
from dotenv import load_dotenv
from groq import Groq
from pydantic import BaseModel, ValidationError
from typing import List

from company_names import clean_company_name, PAREN

load_dotenv()
GROQ_API_KEY_FALLBACK = "dummy_key"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", GROQ_API_KEY_FALLBACK)
client = Groq(
    api_key=GROQ_API_KEY,
    timeout=60.0,
    max_retries=2,
)

class ComplianceAuditResult(BaseModel):
    entity_name: str
    is_paper_failure: bool
    evidence_quote: str
    violates_rule_96: bool
    violates_sub_rule_7: bool
    violates_schedule_h2: bool
    root_cause_summary: str
    failure_mode: str = ""  # 'manual_process' | 'formulation' | 'unclear'

class BatchComplianceAuditResult(BaseModel):
    results: List[ComplianceAuditResult]

def analyze_cdsco_failure_batch(batch_items: List[dict]) -> dict:
    if not GROQ_API_KEY.startswith("gsk_"):
        print("Warning: GROQ_API_KEY is not set. Skipping LLM analysis.")
        return {str(i): {} for i in range(len(batch_items))}
        
    prompt = f"""
    You are a Pharmaceutical Compliance Auditor. Analyze the CDSCO failure notices below.

    RULE: Base every boolean strictly on the text of each notice. NEVER infer a
    regulatory violation from a generic quality term ("Content", "pH", "Dissolution",
    "Net Weight", "Microbial", "Description", etc. are quality failures and are NOT
    evidence of any rule below).

    Definitions:
    - is_paper_failure: TRUE only if the text explicitly indicates a paper-based
      QMS issue (e.g. missing signatures, manual batch records, uncontrolled
      spreadsheets/logbooks, ALCOA+ data integrity, transcription errors).
    - failure_mode: classify the NATURE of the quality failure for Category-2
      (deductive) assessment. CDSCO NSQ alerts publish only chemical test
      outcomes, never IT audits, so we infer manual process gaps from failure
      type:
        "manual_process"  -> failure typical of manual weighing/dispensing and
          missing in-process interlocks: dissolution, assay, content
          uniformity, weight variation / uniformity of weight, disintegration,
          hardness, friability, dose uniformity, label claim.
        "formulation"     -> API/formulation-quality issue NOT tied to manual
          operations: related substances, impurities, degradation products,
          microbial/bacterial contamination, sterility, endotoxin, pyrogen,
          preservative, moisture, particulate.
        "unclear"         -> generic or insufficient text ("Content", "The
          sample does not conform to the IP", "Description", etc.).
      Choose the single best label from the reason text; if none applies,
      use "unclear".
    - violates_rule_96: Rule 96 = QR code/serialization on APIs. TRUE only if the
      text mentions QR code, barcode, 2D data matrix, serialization, or
      track-and-trace on an Active Pharmaceutical Ingredient.
    - violates_sub_rule_7: Sub-Rule 7 = excipient controls for FDFs. TRUE only if
      the text mentions an excipient (e.g. denaturant, bitterant, gelatin,
      colouring agent) or Sub-Rule 7 explicitly.
    - violates_schedule_h2: Schedule H2 = antimicrobial track-and-trace. TRUE only
      if the text mentions track-and-trace, serialization, QR/barcode on an
      antimicrobial/Schedule H2 product, or "Schedule H2" explicitly. NOTE:
      "Schedule V" in the reason text is a different legal schedule (assay claims)
      and is NOT Schedule H2.

    For each item:
    - If no quoted text supports a boolean, that boolean MUST be false.
    - evidence_quote: the exact fragment of the reason text supporting the booleans
      you set; empty string if there is none.
    - root_cause_summary: concise summary of the recorded failure; if the true root
      cause is not stated in the notice, say so rather than inventing one.

    Data:
    {json.dumps(batch_items, indent=2)}
    
    Respond ONLY with a valid JSON object using this exact schema:
    {{
      "results": [
        {{
          "entity_name": "string",
          "is_paper_failure": boolean,
          "evidence_quote": "string",
          "violates_rule_96": boolean,
          "violates_sub_rule_7": boolean,
          "violates_schedule_h2": boolean,
          "root_cause_summary": "string",
          "failure_mode": "manual_process|formulation|unclear"
        }}
      ]
    }}
    Important: The length of the 'results' array MUST EXACTLY match the number of items in the input data ({len(batch_items)} items), preserving the original order.
    """
    
    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You are a data extraction assistant that outputs strict JSON array wrapped in a 'results' object."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=4096
        )
        
        raw_json = completion.choices[0].message.content
        parsed_data = json.loads(raw_json)
        
        # Validate against Pydantic model
        validated_data = BatchComplianceAuditResult(**parsed_data)
        return {str(i): res.dict() for i, res in enumerate(validated_data.results)}
        
    except (json.JSONDecodeError, ValidationError) as e:
        print(f"Groq Extraction Error for batch: {e}")
        return {str(i): {} for i in range(len(batch_items))}
    except Exception as e:
        print(f"Groq API Error: {e}")
        return {str(i): {} for i in range(len(batch_items))}

def analyze_regulatory_finding(evidence_text: str, firm_name: str) -> dict:
    """Classify one external regulatory finding for paper-QMS fingerprints.

    Returns {"is_paper_qms": bool, "evidence_quote": str, "confidence": float,
    "reason": str}. is_paper_qms is True only when the text explicitly cites a
    documentation/data-integrity failure (manual batch records, missing
    signatures, uncontrolled spreadsheets/logbooks, ALCOA+, transcription
    errors, record discrepancies).
    """
    if not GROQ_API_KEY.startswith("gsk_"):
        return {"is_paper_qms": False, "evidence_quote": "", "confidence": 0.0,
                "reason": "LLM unavailable"}
    if not evidence_text:
        return {"is_paper_qms": False, "evidence_quote": "", "confidence": 0.0,
                "reason": "no evidence text"}

    prompt = f"""
    You are a Pharmaceutical Compliance Auditor reviewing a regulatory finding
    for {firm_name}.

    Task: decide whether the finding indicates the firm relies on PAPER-BASED
    quality management (paper QMS).

    Evidence taxonomy:
    - CATEGORY 1 (explicit evidence): the regulator DIRECTLY cites paper / manual
      data handling. This is the only basis for is_paper_qms = TRUE. Examples:
      manual/inaccurate batch production records, missing or forged signatures,
      uncontrolled spreadsheets/logbooks/paper records, ALCOA/ALCOA+ data
      integrity violations (backdating, falsification), transcription errors,
      failure to record, record discrepancies.
    - CATEGORY 2 (deductive, NOT set here): CDSCO NSQ alerts publish only
      chemical test failures, never IT audits — "paper-based" there is an
      analytical inference from proxies, NOT a direct quote. Do NOT treat a
      failed dissolution/assay/potency test as explicit paper evidence.

    is_paper_qms = TRUE only if the text explicitly cites a documentation /
    data-integrity failure as described under Category 1.

    Quality failures such as failed potency, dissolution, contamination,
    mislabeling, or failed CGMP practices WITHOUT an explicit documentation
    component are NOT paper-QMS evidence.

    Finding text:
    {evidence_text[:4000]}

    Respond ONLY with a valid JSON object:
    {{
      "is_paper_qms": boolean,
      "evidence_quote": "exact fragment supporting the verdict ('' if none)",
      "confidence": number between 0 and 1,
      "reason": "short justification"
    }}
    """

    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You output strict JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=512,
        )
        return json.loads(completion.choices[0].message.content)
    except (json.JSONDecodeError, ValidationError) as e:
        print(f"Groq Extraction Error (finding): {e}")
    except Exception as e:
        print(f"Groq API Error (finding): {e}")
    return {"is_paper_qms": False, "evidence_quote": "", "confidence": 0.0,
            "reason": "LLM error"}

def classify_failure_modes_batch(items: List[dict], batch_size: int = 20,
                                 on_chunk=None) -> dict:
    """Classify the failure mode of CDSCO NSQ items into
    'manual_process' | 'formulation' | 'unclear'. Lightweight, failure-mode-only
    (no root-cause/report generation) so backfills run fast. Returns
    {index: label} for the input list."""
    if not GROQ_API_KEY.startswith("gsk_"):
        return {i: "" for i in range(len(items))}

    labels = {}
    total_chunks = (len(items) + batch_size - 1) // batch_size
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        # NOTE: keep this prompt concise. A long definitional preamble makes
        # gpt-oss-120b fail Groq's strict JSON validation at batch 20.
        prompt = f"""Classify each CDSCO NSQ failure notice below.
manual_process = dissolution, assay, content uniformity, weight variation or uniformity of weight, disintegration, hardness, friability, dose uniformity, label claim.
formulation = related substances, impurities, microbial or bacterial contamination, sterility, endotoxin, pyrogen, moisture, particulate, degradation products.
unclear = generic or insufficient text (e.g. "Content", "The sample does not conform to the IP", "Description").
Base the label only on the reason text. Prefer manual_process when both apply. Keep labels in the exact same order as the input.
Inputs:
{json.dumps([{"drug_name": (i.get("drug_name","") or "")[:120], "reason": (i.get("reason","") or "")[:300]} for i in chunk])}
Respond ONLY with a valid JSON object with a single key: {{"labels": ["manual_process|formulation|unclear", ...]}}"""
        try:
            completion = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {"role": "system", "content": "You output strict JSON."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=2048,
            )
            data = json.loads(completion.choices[0].message.content)
            out = data.get("labels", [])
            if len(out) != len(chunk):
                out = out + [""] * (len(chunk) - len(out))
        except Exception as e:  # noqa: BLE001
            print(f"Groq failure-mode error: {e}", flush=True)
            out = [""] * len(chunk)
        for j, label in enumerate(out):
            labels[start + j] = label
        if on_chunk is not None:
            on_chunk((start // batch_size) + 1, total_chunks)
    return labels


def classify_schedule_m_gap_batch(items: List[dict], batch_size: int = 20,
                                  on_chunk=None) -> dict:
    """Classify each CDSCO NSQ notice into the revised Schedule M Part A
    requirement area its failure exposes. Lightweight, gap-area-only, so
    backfills run fast. Returns {index: label}.

    Labels:
    - process_control        dissolution, assay, content uniformity, weight
                             variation/uniformity of weight, disintegration,
                             hardness, friability, dose uniformity.
    - contamination_control  microbial/bacterial contamination, sterility,
                             endotoxin, pyrogen, particulate, moisture.
    - stability              related substances, impurities, degradation
                             products, assay drift.
    - labeling_packaging     label claim, labeling, packaging/container issues.
    - data_integrity         explicit documentation / batch-record / data issues.
    - unclear                generic or insufficient text.
    """
    if not GROQ_API_KEY.startswith("gsk_"):
        return {i: "" for i in range(len(items))}

    labels = {}
    total_chunks = (len(items) + batch_size - 1) // batch_size
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        prompt = f"""Map each CDSCO NSQ failure notice below to the revised Schedule M (GMP, India) Part A requirement area its failure exposes.
process_control = dissolution, assay, content uniformity, weight variation or uniformity of weight, disintegration, hardness, friability, dose uniformity.
contamination_control = microbial or bacterial contamination, sterility, endotoxin, pyrogen, particulate, moisture.
stability = related substances, impurities, degradation products.
labeling_packaging = label claim, labeling, packaging or container issues.
data_integrity = explicit documentation, batch record or data reliability issues.
unclear = generic or insufficient text (e.g. "Content", "The sample does not conform to the IP", "Description").
Base the label only on the reason text. Prefer process_control when both apply. Keep labels in the exact same order as the input.
Inputs:
{json.dumps([{"drug_name": (i.get("drug_name","") or "")[:120], "reason": (i.get("reason","") or "")[:300]} for i in chunk])}
Respond ONLY with a valid JSON object with a single key: {{"labels": ["process_control|contamination_control|stability|labeling_packaging|data_integrity|unclear", ...]}}"""
        try:
            completion = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {"role": "system", "content": "You output strict JSON."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=2048,
            )
            data = json.loads(completion.choices[0].message.content)
            out = data.get("labels", [])
            if len(out) != len(chunk):
                out = out + [""] * (len(chunk) - len(out))
        except Exception as e:  # noqa: BLE001
            print(f"Groq schedule-M-gap error: {e}", flush=True)
            out = [""] * len(chunk)
        for j, label in enumerate(out):
            labels[start + j] = label
        if on_chunk is not None:
            on_chunk((start // batch_size) + 1, total_chunks)
    return labels


def extract_company_names_batch(raw_names: List[str]) -> List[str]:
    """Clean CDSCO manufacturer strings into trading names, preserving order.

    Returns one clean name per input ('' when unparseable). Intended as a
    fallback when the heuristic in company_names.py fails.
    """
    if not GROQ_API_KEY.startswith("gsk_"):
        return [""] * len(raw_names)

    prompt = f"""
    You extract clean pharmaceutical company trading names from messy CDSCO
    manufacturer strings that mix the company name with a full site address.

    For each input, return ONLY the company trading/legal name — drop address,
    pincode, "M/s.", "Plot No", village, district, state, and unit/site details.
    Examples:
    - "M/s.Argon Remedies Pvt. Ltd., Sarverkhera. Moradabad Road, Kashipur..." -> "Argon Remedies Pvt. Ltd."
    - "Gidsha Pharmaceuticals Plot No. 611 612, Mega GIDC, Kharedi, Dahod..." -> "Gidsha Pharmaceuticals"
    - "Zee Laboratories 47, Industrial Area, Paonta Sahib-173025" -> "Zee Laboratories"
    If the string has no company name (it is only an address), use "".

    Inputs:
    {json.dumps(raw_names, indent=2)}

    Respond ONLY with a valid JSON object:
    {{"names": ["clean name or empty string for each input, in order"]}}
    """

    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You output strict JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=2048,
        )
        data = json.loads(completion.choices[0].message.content)
        names = data.get("names", [])
        if len(names) != len(raw_names):
            raise ValueError("length mismatch")
        return [str(n).strip() for n in names]
    except (json.JSONDecodeError, ValidationError, ValueError) as e:
        print(f"Groq Extraction Error (company names): {e}")
    except Exception as e:
        print(f"Groq API Error (company names): {e}")
    return [""] * len(raw_names)

def generate_search_queries(record_details: dict) -> List[str]:
    """Generate search queries for a given regulatory record."""
    if not GROQ_API_KEY.startswith("gsk_"):
        return []
    
    mfr = record_details.get("manufacturer", "")
    drug = record_details.get("drug_name", "")
    batch = record_details.get("batch_no", "")
    
    if not mfr:
        return []
        
    prompt = f"""
    Generate 3-5 Google search queries to find news reports, press releases, or 
    regulatory actions related to a specific pharmaceutical product failure.
    
    Manufacturer: {mfr}
    Product: {drug}
    Batch: {batch}
    
    Generate queries that would uncover:
    1. Recalls for this specific batch or product
    2. GMP or quality issues at this manufacturer
    3. Regulatory actions by CDSCO, FDA, or other bodies against this manufacturer
    4. Facility closure, licence suspension/cancellation, plant shutdown, or GMP 
       suspension at this manufacturer

    Respond ONLY with a valid JSON object:
    {{"queries": ["query1", "query2", ...]}}
    """
    
    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You output strict JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=512,
        )
        data = json.loads(completion.choices[0].message.content)
        queries = data.get("queries", [])
        return [str(q) for q in queries][:5]
    except Exception as e:
        print(f"Groq query generation error: {e}")
    return []

def _focused_excerpt(text: str, record_details: dict,
                     head_len: int = 1800, window: int = 2200,
                     max_len: int = 6000) -> str:
    """Build a classifier excerpt from a possibly-long document (e.g., monthly
    NSQ alert tables listing hundreds of drugs). A blind [:6000] truncation can
    cut away the manufacturer's own row; instead we keep the document head plus
    a window around the first mention of the manufacturer or product."""
    if not text:
        return text

    needles = []
    mfr = (record_details.get("manufacturer") or "").strip()
    drug = (record_details.get("drug_name") or "").strip()

    mfr_clean = clean_company_name(PAREN.sub("", mfr)).lower().strip()
    if mfr_clean:
        needles.append(mfr_clean)
        needles.append(re.sub(r"[^a-z0-9 ]+", " ", mfr_clean).strip())

    drug_low = re.sub(r"\s+", " ", drug.lower()).strip()
    if drug_low:
        needles.append(drug_low)
        words = drug_low.split()
        if len(words) >= 2:
            needles.append(" ".join(words[:2]))

    low = text.lower()
    pos = None
    for n in needles:
        if not n:
            continue
        i = low.find(n)
        if i >= 0 and (pos is None or i < pos):
            pos = i

    if pos is None or pos <= head_len:
        return text[:max_len]

    start = max(0, pos - window)
    end = min(len(text), pos + window)
    return text[:head_len] + "\n...\n" + text[start:end]


def _heuristic_web_classification(text: str, record_details: dict) -> dict:
    """Deterministic relevance fallback used when the LLM classifier fails.

    Keeps the dashboard honest: an article that demonstrably discusses the
    manufacturer alongside a regulatory/quality signal is NEVER labelled
    NOT RELEVANT. When there is no defensible signal, relevance_score stays
    None so the UI shows UNSCORED instead of a confident "not relevant"."""
    low = (text or "").lower()
    mfr = (record_details.get("manufacturer") or "").strip()
    drug = (record_details.get("drug_name") or "").strip()

    mfr_clean = clean_company_name(PAREN.sub("", mfr)).lower().strip()
    stop = {"ltd", "pvt", "limited", "private", "company", "co", "inc", "llc",
            "pharma", "pharmaceutical", "pharmaceuticals", "laboratories",
            "laboratory", "lab", "labs", "formulation", "formulations",
            "biotech", "remedies", "manufacturing", "works", "industries"}
    tokens = [t for t in mfr_clean.split() if t not in stop and len(t) >= 4]
    company_hit = bool(mfr_clean and (mfr_clean in low or any(t in low for t in tokens)))

    drug_clean = re.sub(r"\s+", " ", drug.lower()).strip()
    drug_hit = bool(drug_clean and (drug_clean in low or " ".join(drug_clean.split()[:2]) in low))

    kw = {
        "closure": ["closure", "closed", "shut down", "shutdown", "cease manufacturing",
                    "cease production", "stop manufacturing", "stop production", "plant closure"],
        "licence_suspension": ["suspension", "suspended", "licence cancelled", "licence revoked",
                               "license cancelled", "license revoked", "gmp certificate",
                               "gmp suspension", "withdraw approval"],
        "recall": ["recall", "voluntary recall", "market withdrawal", "withdrawn from the market",
                   "withdraw the product"],
        "warning_letter": ["warning letter", "import alert", "notice of violation", "form 483",
                           "regulatory notice"],
        "prosecution": ["prosecution", "prosecuted", "court", "convicted", "charged", "fined", "criminal"],
    }
    matched = {k: [kwd for kwd in words if kwd in low] for k, words in kw.items()}
    hits = sum(bool(v) for v in matched.values())

    if not company_hit or hits == 0:
        return {"relevance_score": None, "is_relevant": False, "corroborates_failure": False,
                "is_paper_qms": False, "recall_action": False, "severity": "low",
                "regulatory_action": "none",
                "summary": "Heuristic (LLM unavailable): no company + regulatory signal found.",
                "heuristic": True}

    severity = ("high" if any(matched[k] for k in ("closure", "licence_suspension", "prosecution"))
                else ("medium" if any(matched[k] for k in ("recall", "warning_letter")) else "low"))
    reg_priority = ["closure", "licence_suspension", "recall", "warning_letter", "prosecution"]
    reg_action = next((k for k in reg_priority if matched[k]), "none")
    score = 70 + (10 if drug_hit else 0) + (10 if severity == "high" else 0)
    score = min(95, score)
    return {
        "relevance_score": score,
        "is_relevant": True,
        "corroborates_failure": bool(matched["recall"] or matched["closure"] or matched["licence_suspension"]),
        "is_paper_qms": False,
        "recall_action": bool(matched["recall"]),
        "severity": severity,
        "regulatory_action": reg_action,
        "summary": "Heuristic (LLM unavailable): article links the manufacturer to a regulatory/quality signal.",
        "heuristic": True,
    }


def classify_web_evidence(article_text: str, record_details: dict) -> dict:
    """Classify a fetched web article for relevance, paper-QMS implications and
    the type of regulatory action it describes."""
    if not GROQ_API_KEY.startswith("gsk_"):
        return {"relevance_score": None, "is_relevant": False, "corroborates_failure": False, 
                "is_paper_qms": False, "recall_action": False, "severity": "low",
                "regulatory_action": "none", "summary": "LLM unavailable"}
    
    if not article_text:
         return {"relevance_score": None, "is_relevant": False, "corroborates_failure": False, 
                "is_paper_qms": False, "recall_action": False, "severity": "low",
                "regulatory_action": "none", "summary": "No text"}
                
    mfr = record_details.get("manufacturer", "")
    drug = record_details.get("drug_name", "")
    
    prompt = f"""
    You are a Pharmaceutical Compliance Analyst. Analyze the following news article or web page
    in the context of a known regulatory failure.
    
    Manufacturer of interest: {mfr}
    Product of interest: {drug}
    
    Determine:
    1. relevance_score (0-100): How relevant is this article to the manufacturer or product? 
       (e.g., 100 if it discusses this specific failure, 70 if it discusses a different failure 
       at the same manufacturer, 0 if it's unrelated).
    2. is_relevant (boolean): True if score >= 50.
    3. corroborates_failure (boolean): True if the article mentions the specific product failure/recall.
    4. is_paper_qms (boolean): True if the article explicitly cites documentation/data-integrity failures
       (e.g., missing signatures, manual records, falsification, transcription errors).
    5. recall_action (boolean): True if the article mentions a product recall or market withdrawal.
    6. severity (string): "high", "medium", or "low" based on the implications for the manufacturer's QMS.
    7. summary (string): A 1-sentence summary of the article's findings related to the manufacturer.
    8. regulatory_action (string): the most serious regulatory action described, one of:
       "closure" (facility closed / plant shut down), "licence_suspension" (licence cancelled
       or suspended, GMP certificate suspended/withdrawn), "recall" (product recall or market
       withdrawal), "warning_letter" (regulatory warning/notice), "prosecution" (legal action
       against the firm), or "none".
    
    Article text (truncated):
    {_focused_excerpt(article_text, record_details)}
    
    Respond ONLY with a valid JSON object:
    {{
      "relevance_score": integer,
      "is_relevant": boolean,
      "corroborates_failure": boolean,
      "is_paper_qms": boolean,
      "recall_action": boolean,
      "severity": "high|medium|low",
      "regulatory_action": "closure|licence_suspension|recall|warning_letter|prosecution|none",
      "summary": "string"
    }}
    """
    
    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You output strict JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=1024,
        )
        return json.loads(completion.choices[0].message.content)
    except Exception as e:
        print(f"Groq classification error: {e}")

    return _heuristic_web_classification(article_text, record_details)

