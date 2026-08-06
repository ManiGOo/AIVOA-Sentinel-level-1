import os
import json
from dotenv import load_dotenv
from groq import Groq
from pydantic import BaseModel, ValidationError
from typing import List

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
