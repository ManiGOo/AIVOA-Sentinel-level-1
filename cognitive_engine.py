import os
import json
from dotenv import load_dotenv
from groq import Groq
from pydantic import BaseModel, ValidationError
from typing import List

load_dotenv()
client = Groq(api_key=os.environ.get("GROQ_API_KEY", "dummy_key"))

class ComplianceAuditResult(BaseModel):
    entity_name: str
    is_paper_failure: bool
    evidence_quote: str
    violates_rule_96: bool
    violates_sub_rule_7: bool
    violates_schedule_h2: bool
    root_cause_summary: str

class BatchComplianceAuditResult(BaseModel):
    results: List[ComplianceAuditResult]

def analyze_cdsco_failure_batch(batch_items: List[dict]) -> dict:
    if os.environ.get("GROQ_API_KEY", "dummy_key") == "dummy_key":
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
          "root_cause_summary": "string"
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
