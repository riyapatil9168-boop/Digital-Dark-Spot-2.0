# Digital Dark Spot 2.0 — FastAPI backend

The original HTML/CSS interface is retained. The PHP API has been replaced by Python FastAPI.

## Pipeline

`Frontend → POST /analyze → Source ingestion (URL/PDF/TXT/DOCX) → claim extraction → gap/conflict analysis → structured JSON → frontend rendering`

## Run locally (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Add your Gemini API key to .env (optional for heuristic fallback; required for semantic analysis/chat)
uvicorn app.main:app --reload --port 8000
```

Open http://127.0.0.1:8000. Swagger: /docs; health: /health.

## API
- `GET /health`
- `POST /analyze` multipart: `question`, `sources` (JSON array of URLs), `documents[]`
- `POST /chat` JSON: `{ "message": "...", "history": [{"role":"user|model","text":"..."}] }`

## Configuration
`GEMINI_API_KEY` (server-side only), `GEMINI_MODEL` (defaults to gemini-2.5-flash), `CORS_ORIGINS` (comma-separated allowed origins).

## Limitations and safety
URL ingestion is HTTP(S)-only, rejects private/local addresses, limits file size and redirects. Sources may fail independently. Without Gemini, results are marked `heuristic_fallback` with low confidence; heuristic extraction is preliminary and cannot reliably detect semantic contradictions. Always verify findings against source material.