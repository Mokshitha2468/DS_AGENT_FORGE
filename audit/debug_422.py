import json
import urllib.request
import urllib.error
from pathlib import Path

payloads_dir = Path(r"c:\Users\user\Desktop\DS-AgentForge\DS-AgentForge\audit\payloads")
req_data = json.loads((payloads_dir / "step1_generate_request.json").read_text(encoding="utf-8"))

req = urllib.request.Request(
    "http://127.0.0.1:8000/api/plan",
    data=json.dumps(req_data).encode("utf-8"),
    headers={"Content-Type": "application/json"}
)

try:
    with urllib.request.urlopen(req) as resp:
        print("Success:", resp.read().decode())
except urllib.error.HTTPError as e:
    err_body = e.read().decode()
    print(f"HTTP Error {e.code}:")
    print(err_body)
