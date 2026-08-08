import requests
import json

base_url = "http://localhost:5000"
endpoints = [
    ("/", "GET", None),
    ("/api/v1/config", "GET", None),
    ("/api/v1/signals/high-priority?page_size=2", "GET", None),
    ("/api/v1/companies/count", "GET", None),
    ("/api/v1/companies/ranking?page_size=2", "GET", None),
    ("/api/v1/scraper/status", "GET", None),
    ("/api/v1/scraper/enrichment/status", "GET", None),
    ("/api/v1/regulatory/status", "GET", None),
    ("/api/v1/campaigns", "GET", None),
    ("/api/v1/leads/status", "GET", None),
]

print("=== CHECKING ALL HTTP ENDPOINTS ===")
for path, method, payload in endpoints:
    url = f"{base_url}{path}"
    print(f"[{method}] {path} ...", end="", flush=True)
    try:
        if method == "GET":
            response = requests.get(url, timeout=5)
        else:
            response = requests.post(url, json=payload, timeout=5)
        
        status = response.status_code
        print(f" Status: {status}", end="")
        if status == 200:
            try:
                # check if it is JSON and print short summary
                data = response.json()
                if isinstance(data, dict):
                    summary = ", ".join([f"{k}={v}" for k, v in list(data.items())[:3] if not isinstance(v, (dict, list))])
                    print(f" (JSON: {summary[:60]}...)")
                elif isinstance(data, list):
                    print(f" (JSON: list of length {len(data)})")
                else:
                    print(" (JSON)")
            except ValueError:
                # not JSON (probably HTML frontend)
                print(f" (HTML: {len(response.text)} chars)")
        else:
            print(f" (FAILED: {response.text[:200]})")
    except Exception as e:
        print(f" ERROR: {e}")
