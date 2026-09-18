# GridWise Energy Optimizer

A robust, production-ready FastAPI microservice for 24-hour campus microgrid energy scheduling. GridWise ingests dynamic solar availability, hourly load demands, time-of-use tariffs, battery storage parameters, and 1–3 unstructured natural-language operator notes. It couples a Generative Language Model (LLM) for semantic note interpretation with deterministic schema guardrails, a two-phase Linear Program (LP) solver via HiGHS, and an independent 20-point constraint replay validator to deliver provably optimal, physically valid dispatch plans.

---

## 1. Live Endpoint

- **Service Base URL**: `https://gridwise-api-8b23.onrender.com`
- **Health Check**: `GET /health`
- **Schedule Optimization**: `POST /optimize-energy`

---

## 2. Architecture & Pipeline

The pipeline enforces a strict **separation of concerns**: the LLM is used strictly for linguistic comprehension and semantic parameter extraction, while all numerical calculations, schedule dispatching, and physical safety guarantees are handled deterministically.

```
                  ┌─────────────────────────────────────────┐
                  │ 1-3 Unstructured Operator Notes (Text)  │
                  └────────────────────┬────────────────────┘
                                       │
                                       ▼
                  ┌─────────────────────────────────────────┐
                  │ LLM Semantic Interpreter (Gemini 2.5)   │
                  │ Structured Schema Extraction (temp=0.0) │
                  └────────────────────┬────────────────────┘
                                       │
                                       ▼
                  ┌─────────────────────────────────────────┐
                  │ Deterministic Guardrail Validation       │
                  │ Range checks, hallucination rejection,   │
                  │ Isolation per note, fallback to no_op   │
                  └────────────────────┬────────────────────┘
                                       │
                                       ▼
                  ┌─────────────────────────────────────────┐
                  │ Directive Compilation (Most-Restrictive)│
                  └────────────────────┬────────────────────┘
                                       │
   ┌───────────────────────────────────┴───────────────────────────────────┐
   │                                                                       │
   ▼                                                                       ▼
┌───────────────────────────────────────┐   ┌─────────────────────────────────────────┐
│ Two-Phase HiGHS Linear Program (LP)   │   │ Independent Replay Validator (20 Rules) │
│ Phase 1: Pure Cost Minimization       │──▶│ Energy balance, battery dynamics,       │
│ Phase 2: Cost-Preserving Peak Shaping │   │ solar limits, directive compliance      │
└───────────────────────────────────────┘   └────────────────────┬────────────────────┘
                                                                 │
                                                                 ▼
                                            ┌─────────────────────────────────────────┐
                                            │ Final Verified OptimizeResponse (HTTP)  │
                                            └─────────────────────────────────────────┘
```

### LLM vs. Deterministic Responsibilities

| Responsibility | Component | Mechanism |
|---|---|---|
| Natural-language parsing & time/fraction inference | LLM Interpreter | Google Gemini 2.5 Flash (Structured JSON mode, zero-shot few-shots) |
| Syntactic & Semantic Bounds Checking | Guardrails (`app/guardrails.py`) | Deterministic validation: non-empty hours, factor in [0,1], reserve <= capacity |
| Conflicting Directive Merging | Directives (`app/directives.py`) | Most-restrictive composition (multiplication of solar factors, max reserves) |
| Optimal Dispatch Calculation | LP Optimizer (`app/optimizer.py`) | SciPy HiGHS interior point & simplex solver (96 decision variables) |
| Physical Invariance Replay | Validator (`app/validator.py`) | 20 physical constraint audits (independent of optimizer code) |
| Safe Fallback Guarantee | Pipeline (`app/pipeline.py`) | Deterministic no-op safe schedule if LP encounters unsolvable conflicts |

---

## 3. Model & LLM Configuration

- **Provider**: Google Gemini (`gemini`) via standard REST API
- **Model**: `gemini-2.5-flash`
- **Temperature**: `0.0` (fully deterministic token selection)
- **Output Mode**: Strict JSON schema adhering to flat Directive specification
- **Retry & Budget Control**: 15.0-second total wall-clock budget, single-attempt self-repair retry, graceful degradation to `no_op` on network or quota exhaustion.
- **LRU In-Memory Cache**: Thread-safe LRU caching keyed by SHA-256 over scenario parameters and notes (sub-15ms cached responses).

---

## 4. Guardrail Verification Rules

Before any model output reaches the optimizer, `app/guardrails.py` enforces:
1. **Schema Whitelist**: Only 6 allowed directive types (`solar_reduction`, `battery_reserve`, `no_charge_window`, `no_discharge_window`, `max_grid_window`, `no_op`).
2. **Note Index Alignment**: Exactly one directive per operator note in 0..N-1 order; no missing or duplicate indices.
3. **Temporal Bounds**: `hours` must be a strictly non-empty, deduplicated, sorted list of integers within `[0, 23]`.
4. **Fractional Bounding**: `factor` must fall strictly within `[0.0, 1.0]`. If specified as a percentage, normalized by `factor / 100.0`.
5. **Reserve Legality**: `reserve_kwh` must be non-negative, finite, and `<=` battery `capacity_kwh`.
6. **Grid Cap Legality**: `max_grid_kwh` must be non-negative and finite.
7. **Deterministic `applies` Derivation**: `applies` is set to `False` for `no_op` and `True` for active directives, completely ignoring model self-reports.
8. **Hallucination Rejection**: Any unparsable or out-of-range directive safely defaults to `no_op` without corrupting other notes.

---

## 5. Optimizer Formulation

The dispatch problem is formulated as a 96-variable Linear Program over the 24-hour horizon:
- **Variables**: `grid_kwh[h]`, `solar_used_kwh[h]`, `battery_charge_kwh[h]`, `battery_discharge_kwh[h]` for `h in 0..23`.
- **Phase 1 Objective**: Minimize total energy procurement cost:
  $$\min \sum_{h=0}^{23} \text{tariff}[h] \times \text{grid\_kwh}[h]$$
- **Phase 2 Objective**: Keeping Phase 1 cost within $10^{-6}$ tolerance, minimize peak hourly grid demand:
  $$\min \text{peak\_grid\_kwh}$$
- **Constraints**:
  - Energy Balance: $\text{grid}[h] + \text{solar\_used}[h] + \text{battery\_discharge}[h] - \text{battery\_charge}[h] = \text{demand}[h]$
  - Solar Generation Bound: $0 \le \text{solar\_used}[h] \le \text{effective\_solar}[h]$
  - Battery Accumulation: $E[h] = E[h-1] + \text{charge}[h] - \text{discharge}[h]$
  - Battery State Limits: $\max(\text{minimum\_energy}, \text{active\_reserve}[h]) \le E[h] \le \text{capacity\_kwh}$
  - Charge / Discharge Rates: $0 \le \text{charge}[h] \le \text{max\_charge}$, $0 \le \text{discharge}[h] \le \text{max\_discharge}$
  - End-of-Day Neutrality: $E[23] \ge \text{initial\_energy\_kwh}$

---

## 6. Environment Variables

All settings are configured via environment variables. **Never put secrets into source control.**

| Variable Name | Required | Default | Description |
|---|---|---|---|
| `LLM_PROVIDER` | Yes | `gemini` | Primary LLM provider (`gemini`, `openai`, `groq`, `openrouter`, `ollama`) |
| `LLM_MODEL` | Yes | `gemini-2.5-flash` | Provider-specific model identifier |
| `LLM_API_KEY` | Yes | *(None)* | Secret API key for the chosen LLM provider |
| `LLM_BASE_URL` | No | `""` | Optional custom base URL for OpenAI-compatible proxies |
| `LLM_TIMEOUT_S` | No | `8.0` | Per-request timeout for LLM provider HTTP calls |
| `LLM_MAX_ATTEMPTS` | No | `2` | Max LLM attempts including repair retry |
| `LLM_TOTAL_BUDGET_S`| No | `15.0` | Total wall-clock time limit for LLM stage per request |
| `LLM_TEMPERATURE` | No | `0.0` | Model temperature (0.0 for reproducibility) |
| `LLM_CACHE_ENABLED` | No | `true` | Enable in-memory LRU interpretation caching |
| `LLM_CACHE_SIZE` | No | `256` | Maximum entries retained in interpretation cache |
| `PORT` | No | `8000` | Port for uvicorn to bind |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 7. Setup & Local Quickstart

### Prerequisites
- Python 3.11+
- Git

### Linux / macOS (Bash)
```bash
# 1. Clone repository
git clone https://github.com/isthisdeception/fest-bup-hackathon-.git
cd fest-bup-hackathon-

# 2. Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and supply your LLM_API_KEY

# 5. Start the service
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Windows (PowerShell)
```powershell
# 1. Clone repository
git clone https://github.com/isthisdeception/fest-bup-hackathon-.git
cd fest-bup-hackathon-

# 2. Set up virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Configure environment
Copy-Item .env.example .env
# Open .env and add your LLM_API_KEY

# 5. Start the service
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 8. Verified API Execution Examples

### Health Check (`GET /health`)
```bash
curl -s http://127.0.0.1:8000/health
```
**Expected Response:**
```json
{"status":"ok"}
```

### Energy Optimization (`POST /optimize-energy`)
```bash
curl -s -X POST http://127.0.0.1:8000/optimize-energy \
  -H "Content-Type: application/json" \
  --data-binary "@local_request.json"
```

**Captured Response (Truncated for display):**
```json
{
  "scenario_id": "LOCAL-001",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {
        "hours": [13, 14],
        "factor": 0.2
      },
      "explanation": "Solar output reduced to 20% from 1 PM to 3 PM."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 150.0,
      "solar_used_kwh": 0.0,
      "battery_action": "charge",
      "battery_kwh": 50.0,
      "battery_energy_after_kwh": 150.0
    },
    {
      "hour": 1,
      "grid_kwh": 151.0,
      "solar_used_kwh": 0.0,
      "battery_action": "charge",
      "battery_kwh": 50.0,
      "battery_energy_after_kwh": 200.0
    }
  ],
  "total_grid_kwh": 2676.0,
  "total_cost_bdt": 18782.0,
  "peak_grid_kwh": 172.0,
  "plan_summary": "Applied 1 operator directive(s): solar_reduction on hours 13-14 (factor 0.20). Battery returns to 100.0 kWh. Total grid 2676.00 kWh at 18782.00 BDT, peak 172.00 kWh."
}
```

### HTTP Status Code Contract
- `200 OK`: Successful optimization and plan validation.
- `400 Bad Request`: Malformed JSON or structural validation error (missing fields, wrong data types, payload > 2MB).
- `422 Unprocessable Entity`: Well-formed request with domain violations (negative demand, hours not covering 0..23).
- `500 Internal Server Error`: Masked internal failure (returns sanitized envelope with `error_id`, zero tracebacks).

---

## 9. Public Sample Test Procedure

Run the public validation suite against the live service:
```bash
python tests/run_public_samples.py --mode http --base-url http://127.0.0.1:8000
```

### Expected Output
```
Total cases: 10
Semantic interpretation matching: 10 / 10 (100.0%)
Schedule physical validity:       10 / 10 (100.0%)
Optimization cost ratio (avg):    1.000000
Cost equivalence matches:         10 / 10 (100.0%)
RESULT: PASSED 10/10
```

---

## 10. Docker Fallback Image

A pre-built, production-hardened Docker image is publicly hosted on GitHub Packages (GHCR):

- **Image Reference (Tag)**: `ghcr.io/isthisdeception/gridwise-api:round1`
- **Image Reference (Digest)**: `ghcr.io/isthisdeception/gridwise-api@sha256:9851cd12bd88887fc1069b3f45c144227069f0e454377e03d1237676c88d4b66`
- **Exposed Port**: `8000` (configurable via `PORT` environment variable)
- **Security**: Runs under non-root `appuser` (UID 10001); zero baked-in secrets.

### Pull & Run Command
```bash
docker run -d --name gridwise -p 8000:8000 \
  -e LLM_PROVIDER=gemini \
  -e LLM_MODEL=gemini-2.5-flash \
  -e LLM_API_KEY="<YOUR_GEMINI_API_KEY>" \
  ghcr.io/isthisdeception/gridwise-api:round1
```

---

## 11. Dependencies & Credits

### Core Dependencies
- **FastAPI** (`0.115.6`): High-performance async API framework.
- **Uvicorn** (`0.34.0`): ASGI web server.
- **SciPy** (`1.15.1`): High-performance HiGHS linear programming solver.
- **NumPy** (`2.2.3`): Vectorized numerical array computations.
- **Pydantic** (`2.10.4`): Strict data validation and schema serialization.
- **HTTPX** (`0.28.1`): Resilient HTTP client for LLM provider communication.
- **Python-Dotenv** (`1.0.1`): Secure local environment file loader.

### AI Assistance Disclosure
Development of test fixtures, LP formulation templates, and prompt few-shot expansions was assisted by Antigravity IDE (Google DeepMind advanced agentic coding pair-programmer) in accordance with Section 04 of the Participant Guide.

---

## 12. Secret Handling & Security

1. **Zero Secret Baking**: `.dockerignore` strictly excludes `.env`, `tests/`, and local logs from Docker build contexts.
2. **Runtime Injection Only**: Credentials exist exclusively in environment variables and are never persisted to disk.
3. **Automatic Log Scrubbing**: A custom `SecretScrubbingFilter` intercepts all application log streams, masking tokens and API keys with `***REDACTED***`.
4. **Sanitized Error Responses**: Production 500 handlers generate an isolated tracking `error_id` and omit stack traces or internal environment dumps.

---

## 13. Known Limitations & Edge Behaviors

1. **LLM Degradation Protocol**: If the LLM provider experiences total network failure or quota exhaustion, notes degrade gracefully to `no_op`. The optimizer still generates a physically valid cost-optimal schedule, prioritizing safety and ground-truth validity over unverified operator text.
2. **Conflicting Directives**: When two operator notes alter the same hour with identical directive types, the most restrictive constraint is applied (e.g. solar factors multiply, reserves take the maximum).
3. **End-Hour Window Conventions**: In accordance with the problem statement, time windows expressed as "from H1 to H2" are interpreted as left-closed, right-open `[H1, H2)` covering standard operating hour slots.

---

## 14. Repository Lifecycle

Created after official question reveal. Kept private during competition execution; made public immediately upon submission deadline in full compliance with Participant Guide Section 04.
