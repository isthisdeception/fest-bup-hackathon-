# GridWise Hackathon - Final Submission Package

This document collects all required deliverables for the BUP CSE Fest 2026 GridWise Hackathon preliminary submission in accordance with Participant Guide Section 02 & Section 05.

---

## 1. Hosted Public API Service
- **Service Base URL**: `http://<PENDING_AWS_VPS_IP>:8000` *(Being deployed on AWS VPS)*
- **Health Check Endpoint**: `GET /health` -> `{"status":"ok"}`
- **Optimization Endpoint**: `POST /optimize-energy`

---

## 2. GitHub Repository
- **Repository URL**: `https://github.com/isthisdeception/fest-bup-hackathon-`
- **Current Visibility**: **Private** (Kept private during competition per Section 04; to be made public after deadline)
- **Default Branch**: `main`
- **Secret Audit**: Clean (0 credentials committed; `.env` git-ignored and absent from repo)

---

## 3. Docker Fallback Image
- **Registry**: GitHub Container Registry (GHCR)
- **Publicly Pullable**: **Yes** (Confirmed anonymous pull access verified)
- **Image Reference (Tag)**: `ghcr.io/isthisdeception/gridwise-api:round1`
- **Image Reference (Digest)**: `ghcr.io/isthisdeception/gridwise-api@sha256:9851cd12bd88887fc1069b3f45c144227069f0e454377e03d1237676c88d4b66`
- **Exposed Port**: `8000`
- **Service Binding**: `0.0.0.0:${PORT:-8000}`
- **Non-Root User**: `appuser` (UID 10001)

### Required Environment Variable Names (No values)
| Variable Name | Required | Purpose |
|---|---|---|
| `LLM_PROVIDER` | Yes | LLM adapter selection (`gemini`, `openai`, `groq`, `openrouter`, `ollama`) |
| `LLM_MODEL` | Yes | Model ID (e.g. `gemini-2.5-flash`) |
| `LLM_API_KEY` | Yes | Secret API credential for the LLM provider |
| `PORT` | Optional | Internal port override (default: `8000`) |
| `LOG_LEVEL` | Optional | Logging level (default: `INFO`) |

### Verified Docker Run Command
```bash
docker run -d \
  --name gridwise \
  -p 8000:8000 \
  -e LLM_PROVIDER=gemini \
  -e LLM_MODEL=gemini-2.5-flash \
  -e LLM_API_KEY=<your_gemini_api_key> \
  ghcr.io/isthisdeception/gridwise-api:round1
```

### Health Check Smoke Test
```bash
curl -s http://127.0.0.1:8000/health
# Expected output: {"status":"ok"}
```

---

## 4. Model Provider & Architecture
- **LLM Provider**: Google Gemini (`gemini`)
- **LLM Model**: `gemini-2.5-flash`
- **LLM Output Mode**: Strict Structured JSON Schema (`OUTPUT_JSON_SCHEMA`)
- **Temperature**: `0.0`
- **Optimizer**: Two-Phase Linear Program solved via `scipy.optimize.linprog(method="highs")`
  - Phase 1: Total energy procurement cost minimization
  - Phase 2: Cost-preserving peak hourly grid import reduction
- **Validation**: Independent 20-rule physical replay validator (`app/validator.py`)

---

## 5. Benchmark Performance Summary
- **Public Sample Cases**: **10 / 10 PASS** (100% interpretation, 100% validity, average quality ratio 1.0000).
- **Adversarial Operator Notes Suite**: **41 / 41 (100%)** correct interpretations; **19 / 19** LP validity stress fixtures pass with zero violations.
- **API Robustness Suite**: **38 / 38** edge status codes pass; **30 / 30** stability calls return HTTP 200 with zero 5xx.
- **Latency Benchmarks**:
  - Uncached LLM calls (25 unique queries): **p50 = 1.39s, p95 = 2.20s** (Target: $\le 5.0$s; max 3/3 points).
  - Cached calls (25 queries): **p50 = 15.9ms, p95 = 16.8ms** (Target: $< 300$ms).
  - Startup readiness: `/health` reachable within **150ms** (Target: $< 60$s).

---

## 6. Solution Video (Tie-Breaker Deliverable)
- **Video Link**: `[PENDING UPLOAD]`
- **Target Duration**: $< 3:00$
