"""Quick verification script for Step 06 completion criteria."""
import httpx
import sys

BASE = "http://127.0.0.1:8000"
errors = []

# 1. GET /health exact match
r = httpx.get(f"{BASE}/health")
if r.status_code != 200:
    errors.append(f"/health status: expected 200, got {r.status_code}")
if r.text != '{"status":"ok"}':
    errors.append(f"/health body mismatch: {r.text!r}")
print(f"GET /health  -> {r.status_code}  body={r.text!r}")

# 2. POST /optimize-energy with {} -> 400
r2 = httpx.post(f"{BASE}/optimize-energy", json={})
if r2.status_code != 400:
    errors.append(f"POST {{}} status: expected 400, got {r2.status_code}")
print(f"POST {{}}     -> {r2.status_code}")

# 3. POST /optimize-energy with invalid JSON -> 400
r3 = httpx.post(f"{BASE}/optimize-energy", content=b"not json", headers={"content-type": "application/json"})
if r3.status_code != 400:
    errors.append(f"POST bad JSON status: expected 400, got {r3.status_code}")
print(f"POST badJSON -> {r3.status_code}")

if errors:
    print("\nFAILED:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("\nAll Step 06 checks PASSED")
