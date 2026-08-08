import requests

url = "http://localhost:5000/mcp"
payload = {
    "jsonrpc": "2.0",
    "method": "tools/list",
    "id": 1
}

print("Sending POST to", url)
try:
    response = requests.post(url, json=payload, timeout=5)
    print("Status:", response.status_code)
    print("Headers:", response.headers)
    print("Response text:", response.text)
except Exception as e:
    print("Error:", e)
