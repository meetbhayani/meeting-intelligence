# AI Meeting Intelligence & Action Tracker

Upload a meeting recording and get a transcript, a summary, decisions, action items, risks and open questions — then ask questions about the meeting in plain English.

## Features

- **Audio upload** – MP3, WAV, M4A, AAC, OGG, FLAC, WEBM (or paste an existing transcript)
- **Speech-to-text** with speaker identification
- **AI analysis** – summary, decisions, action items (assignee, deadline, priority, status), risks/blockers/dependencies, unresolved items, participants
- **Natural-language Q&A** – e.g. *"What tasks were assigned to Rahul?"*, with supporting quotes
- **Validated JSON output** stored in a database
- **REST API** and a simple web dashboard

## Tech Stack

- **Backend:** FastAPI, SQLAlchemy, Pydantic
- **AI:** Google Gemini (transcription, structured extraction, Q&A)
- **Database:** SQLite (local) / PostgreSQL (production)
- **Frontend:** HTML + Tailwind CSS
- **Deployment:** Docker, Render

## Getting Started

**Prerequisites:** Python 3.11+ and a [Gemini API key](https://aistudio.google.com/apikey).

```bash
cd Task
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # add your GEMINI_API_KEY
python run.py
```

Open <http://127.0.0.1:8000> for the dashboard, or <http://127.0.0.1:8000/docs> for the API docs.

**Using Docker**

```bash
cd Task
GEMINI_API_KEY=your-key docker compose up --build
```

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `GEMINI_API_KEY` | Gemini API key (required) | – |
| `GEMINI_TRANSCRIPTION_MODEL` | Primary model used to transcribe uploaded audio | `gemini-3.5-transcribe` |
| `GEMINI_TRANSCRIPTION_FALLBACK_MODELS` | Models tried if transcription is overloaded | `gemini-3.5-flash-lite` |
| `GEMINI_ANALYSIS_MODEL` | Model used for meeting analysis and current Q&A | `gemini-3.5-flash-lite` |
| `GEMINI_FALLBACK_MODELS` | Analysis fallback if the selected model is overloaded | `gemini-3.5-flash` |
| `DATABASE_URL` | Database connection string | `sqlite:///./data/meetings.db` |
| `MAX_UPLOAD_MB` | Max audio file size | `100` |

See `Task/.env.example` for all options.

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/meetings/upload` | Upload an audio file and start processing |
| `POST` | `/api/meetings/transcript` | Submit a text transcript |
| `GET` | `/api/meetings` | List meetings |
| `GET` | `/api/meetings/{id}` | Get transcript, metadata and analysis |
| `POST` | `/api/meetings/{id}/query` | Ask a question about a meeting |
| `DELETE` | `/api/meetings/{id}` | Delete a meeting |
| `GET` | `/health` | Health check |

Processing runs in the background. Poll `GET /api/meetings/{id}` until the status is `completed`.

**Example**

```bash
curl -F "file=@09-15-2026-Council-Meeting.mp3" http://127.0.0.1:8000/api/meetings/upload

curl -X POST http://127.0.0.1:8000/api/meetings/{id}/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What decisions were finalized?"}'
```

## Deployment (Render)

1. Push the repository to GitHub.
2. In Render, choose **New → Blueprint** and select the repo (it uses `render.yaml`).
3. Enter your `GEMINI_API_KEY` when prompted and click **Apply**.

Render creates the web service and a PostgreSQL database automatically.

## Project Structure

```
├── render.yaml            # Render deployment config
└── Task/
    ├── app/
    │   ├── main.py        # App entry point
    │   ├── config.py      # Settings
    │   ├── database.py    # Database setup
    │   ├── models.py      # Database models
    │   ├── schemas.py     # Request/response and AI output schemas
    │   ├── service.py     # Gemini integration and processing pipeline
    │   └── api/endpoints.py
    ├── index.html         # Dashboard
    ├── Dockerfile
    └── requirements.txt
```

## Limitations

- Background jobs run in-process, so use a single server worker.
- Speaker names are detected from conversation context; otherwise speakers appear as "Speaker 1", "Speaker 2".
- Accuracy depends on audio quality.
- Gemini free-tier quotas are low; use a billing-enabled key for heavier use.
- No authentication is included.
