import requests
from bs4 import BeautifulSoup
from db_setup import SessionLocal, RegulatoryEvent
from cognitive_engine import analyze_cdsco_failure
from datetime import datetime
import json

# Target endpoints you discovered
CDSCO_ENDPOINTS = {
    "NSQ_DRUG": "https://cdscoonline.gov.in/CDSCO/publicNsqDrugTable",
    "SPURIOUS_DRUG": "https://cdscoonline.gov.in/CDSCO/viewPublicSpuriousDrugData"
}

def calculate_base_score(event_type: str, llm_analysis: dict) -> int:
    score = 0
    if event_type == 'SPURIOUS_DRUG':
        score += 40
    elif event_type == 'NSQ_DRUG':
        score += 20
        
    if llm_analysis.get('is_paper_failure'):
        score += 30
        
    if any([
        llm_analysis.get('violates_rule_96'),
        llm_analysis.get('violates_sub_rule_7'),
        llm_analysis.get('violates_schedule_h2')
    ]):
        score += 20
        
    return score

def scrape_and_process():
    session = requests.Session()
    # Spoof headers to avoid 403 Forbidden errors
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        'Referer': 'https://cdscoonline.gov.in/CDSCO/opencms/en/Notifications/nsq-drugs/',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest'
    })

    db_session = SessionLocal()

    results_summary = {}

    for event_type, url in CDSCO_ENDPOINTS.items():
        print(f"Fetching {event_type} data from {url}...")
        try:
            response = session.get(url, timeout=15)
        except Exception as e:
            print(f"Request failed for {url}: {e}")
            continue

        events_to_parse = []

        try:
            # Attempt to parse as JSON (Standard for DataTables)
            data = response.json()
            events = data.get('aaData') or data.get('data') or []
            for item in events:
                events_to_parse.append({
                    "drug_name": item.get('str_product_name', item.get('drug_name', '')),
                    "manufacturer": item.get('str_manufactured_by', item.get('manufacturer', '')),
                    "batch_no": item.get('str_batch_no', item.get('batch_no', '')),
                    "reason": item.get('str_nsq_result', item.get('reason', '')),
                    "reporting_source": item.get('str_reporting_source', ''),
                    "reported_by": item.get('str_reported_by_lab_or_state', '')
                })
        except ValueError:
            # Fallback if CDSCO returns an HTML table fragment
            soup = BeautifulSoup(response.text, 'html.parser')
            for row in soup.find_all('tr')[1:]:
                cols = [col.text.strip() for col in row.find_all('td')]
                if len(cols) >= 5:
                    events_to_parse.append({
                        "drug_name": cols[1],
                        "manufacturer": cols[2],
                        "batch_no": cols[3],
                        "reason": cols[4]
                    })

        print(f"Found {len(events_to_parse)} records for {event_type}. Processing via Groq...")
        
        # Process concurrently to handle large scale data efficiently
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time

        processed_count = 0
        max_workers = 10 # Adjust based on your Groq API tier limits

        def process_item(item):
            manufacturer = item.get('manufacturer', 'Unknown')
            reason = item.get('reason', '')
            drug_name = item.get('drug_name', '')
            
            # Basic retry logic for API limits
            retries = 3
            for attempt in range(retries):
                llm_analysis = analyze_cdsco_failure(manufacturer, drug_name, reason)
                if llm_analysis or attempt == retries - 1:
                    break
                time.sleep(2 ** attempt) # Exponential backoff

            score = calculate_base_score(event_type, llm_analysis)
            
            return {
                "event_type": event_type,
                "raw_details": item,
                "llm_analysis": llm_analysis,
                "score": score,
                "reporting_source": item.get('reporting_source', ''),
                "reported_by": item.get('reported_by', ''),
                "event_date": datetime.utcnow().date()
            }

        events_to_save = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks
            future_to_item = {executor.submit(process_item, item): item for item in events_to_parse}
            for future in as_completed(future_to_item):
                try:
                    result = future.result()
                    events_to_save.append(result)
                    processed_count += 1
                except Exception as exc:
                    print(f"Error processing item: {exc}")

        # Save to PostgreSQL
        for event_data in events_to_save:
            new_event = RegulatoryEvent(**event_data)
            db_session.add(new_event)
            
        db_session.commit()
        results_summary[event_type] = processed_count
        print(f"Committed {processed_count} {event_type} records to database.\n")
        
    db_session.close()
    return results_summary

if __name__ == "__main__":
    scrape_and_process()
