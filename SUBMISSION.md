# GridWise Submission Details

## Team & Repository
- **Repository (Private during competition)**: https://github.com/isthisdeception/fest-bup-hackathon-
- **Default Branch**: `main`

## Docker Fallback Image
- **Registry**: GitHub Container Registry (GHCR)
- **Image Reference (Tag)**: `ghcr.io/isthisdeception/gridwise-api:round1`
- **Exposed Port**: `8000`
- **Service Binding**: `0.0.0.0:${PORT:-8000}`
- **Non-Root User**: `appuser` (UID 10001)

### Required Environment Variables
| Variable | Required | Description / Allowed Values |
|---|---|---|
| `LLM_PROVIDER` | Yes | Primary provider (`gemini`, `openai`, `groq`, `openrouter`, `ollama`) |
| `LLM_MODEL` | Yes | Model ID (e.g. `gemini-2.5-flash`) |
| `LLM_API_KEY` | Yes | API key for LLM provider |
| `PORT` | Optional | Port to bind (default: `8000`) |
| `LOG_LEVEL` | Optional | Logging level (default: `INFO`) |

### Verified Docker Run Command
```bash
docker run -d \
  --name gridwise \
  -p 8000:8000 \
  -e LLM_PROVIDER=gemini \
  -e LLM_MODEL=gemini-2.5-flash \
  -e LLM_API_KEY=<your_api_key_here> \
  ghcr.io/isthisdeception/gridwise-api:round1
```

### Health Check Verification
```bash
curl -s http://127.0.0.1:8000/health
# Expected Output: {"status":"ok"}
```
