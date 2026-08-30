import urllib.request
import json
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

key = os.environ.get("OPENROUTER_API_KEY", "")
if not key:
    print("[!] OPENROUTER_API_KEY is not set.")
    exit(0)

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=json.dumps({"model": "openai/nvidia/nemotron-3-ultra-550b-a55b:free", "messages": [{"role": "user", "content": "hi"}]}).encode(),
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "HTTP-Referer": "http://localhost", "X-Title": "LocalAgent"}
)
try:
    with urllib.request.urlopen(req) as resp:
        print("Rate Limit Remaining:", resp.headers.get('x-ratelimit-remaining-requests', 'No-Remaining'))
        print("Rate Limit Total:", resp.headers.get('x-ratelimit-limit-requests', 'No-Limit'))
        print(resp.read().decode())
except urllib.error.HTTPError as e:
    print("HTTPError:", e)
    print(e.read().decode())
