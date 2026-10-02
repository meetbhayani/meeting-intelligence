# AI Meeting Intelligence & Action Tracker

Upload a meeting recording (MP3/WAV/…) and get back a transcript with speakers, a summary, confirmed decisions, action items (assignee, deadline, priority, status), risks/blockers/dependencies, unresolved questions and participants. Every result is validated JSON stored in a database. You can then ask questions about any processed meeting in plain English.

**Stack:** FastAPI · Google Gemini (speech-to-text, extraction, Q&A) · Pydantic structured output · SQLAlchemy (SQLite locally, PostgreSQL in production) · single-page Tailwind dashboard · Docker / Render.

---

## Contents
- [Architecture](#architecture)
- [Quick start (local)](#quick-start-local)
- [Deploying to Render](#deploying-to-render)
- [API reference](#api-reference)
- [Output format](#output-format)
- [Model choices](#model-choices)
- [Handling edge cases](#handling-edge-cases)
- [Data model](#data-model)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [Limitations](#limitations)

---

## Architecture

```
 Browser dashboard (index.html)          any REST client
              │                                 │
              ▼                                 ▼
 ┌──────────────────────────── FastAPI ────────────────────────────┐
 │ POST /api/meetings/upload ──► check type, size and file header  │
 │        │                     stream to disk, read duration      │
 │        │                     save row (status = transcribing)   │
 │        │                     return 202 + meeting_id            │
 │        ▼  background task                                       │
 │  ┌─────────────── processing pipeline (service.py) ───────────┐ │
 │  │ 1. Upload audio to Gemini Files API, wait until ACTIVE      │ │
 │  │ 2. Speech-to-text with speaker labels  ──► save transcript  │ │
 │  │                                          (status=analyzing) │ │
 │  │ 3. Structured extraction (response_schema = MeetingAnalysis)│ │
 │  │ 4. Pydantic validation (regenerate once if invalid)         │ │
 │  │ 5. Save analysis JSON (status = completed | failed + reason)│ │
 │  │ 6. Delete the local temp file and the Gemini copy           │ │
 │  └─────────────────────────────────────────────────────────────┘ │
 │                                                                  │
 │ POST /api/meetings/transcript ──► same pipeline from step 3      │
 │ POST /api/meetings/{id}/query ──► transcript + analysis as       │
 │        context, answer validated against the QueryAnswer schema  │
 └──────────────────────────────┬───────────────────────────────────┘
                                ▼
                SQLite (local) / PostgreSQL (production)
```

**Main design decisions**

| Decision | Why |
|---|---|
| **Asynchronous processing** (`202 Accepted`, then poll for status) | Transcribing a long recording takes 30 s or more. Clients get an id straight away, and the dashboard refreshes the list until processing finishes. |
| **Gemini for both speech-to-text and extraction** | One multimodal model hears the audio directly, so it can pick up speaker changes and names from context (*"Rahul, please…"*). This avoids running a separate speech-to-text service and a separate speaker-separation step. |
| **Schema passed to the model, then validated** | The model generates against a `response_schema`, and Pydantic checks the result again before it is saved. Invalid output is regenerated once; otherwise the meeting is marked failed with the reason. |
| **Question answering uses the whole meeting, not a vector database** | One meeting's transcript plus its analysis fits easily in Gemini's context window. Giving the model the full meeting avoids retrieval misses that chunk-based search (RAG) can cause, such as a task and its deadline landing in different chunks, and needs no extra infrastructure. |
| **Lightweight auto-migration** | On startup the app creates tables and adds any missing columns, so an older database keeps working without a migration tool. |

---

## Quick start (local)

Requirements: Python 3.11+ and a Gemini API key from <https://aistudio.google.com/apikey>.

```bash
cd Task
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then set GEMINI_API_KEY in .env
python run.py               # http://127.0.0.1:8000
```

- Dashboard: <http://127.0.0.1:8000/>
- Interactive API docs (Swagger): <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>

**With Docker**

```bash
cd Task
GEMINI_API_KEY=your-key docker compose up --build
```

Data is kept in the `meeting-data` volume.

**Try it with the sample recording**

`09-15-2026-Council-Meeting.mp3` (10 minutes, in the repo root) is a real council meeting you can upload from the dashboard, or with:

```bash
curl -F "file=@09-15-2026-Council-Meeting.mp3" -F "meeting_date=2026-09-15" http://127.0.0.1:8000/api/meetings/upload
```

**Try it with the example from the brief**

```bash
curl -X POST http://127.0.0.1:8000/api/meetings/transcript \
  -H "Content-Type: application/json" \
  -d '{"meeting_date": "2026-09-01", "transcript": "Manager: We need to finalize the beta launch by 15 September.\nRahul: I can complete the payment API integration by 10 September.\nPriya: I will prepare the QA test cases by 12 September.\nManager: Rahul, please also investigate the timeout issue reported by the mobile team.\nRahul: I will check the timeout issue tomorrow.\nPriya: The product pricing is still not finalized.\nManager: Let'"'"'s discuss pricing in the next meeting. For now, the beta launch date remains 15 September.\nRahul: I also need access to the payment gateway test account.\nManager: I will provide that access today."}'
```

---

## Deploying to Render

The app has a `Dockerfile` (in `Task/`) and a Render Blueprint (`render.yaml`, in the repo root) that sets up a web service and a PostgreSQL database.

1. Push the repo to GitHub.
2. In Render, go to **New → Blueprint** and select the repo.
3. Enter `GEMINI_API_KEY` when prompted. `DATABASE_URL` is connected to the Postgres database automatically.
4. Click **Apply**. Once the build finishes, open `https://<service>.onrender.com/health` and check that it shows `"ai_configured": true`.

Every push to `main` redeploys. The free plan sleeps after 15 minutes idle, and free Postgres expires after 30 days; switch both plans in `render.yaml` for a permanent setup.

> Serverless hosts such as Vercel are a poor fit: their ~4.5 MB request-body limit blocks typical meeting recordings, they have no persistent disk, and work can't continue after the response is sent.

---

## API reference

All endpoints are under `/api/meetings`. Full schemas are at `/docs`.

| Method | Path | Description | Success |
|---|---|---|---|
| `POST` | `/upload` | Multipart form: `file` (audio, required) and `meeting_date` (`YYYY-MM-DD`, optional). Starts processing. | `202` |
| `POST` | `/transcript` | JSON `{transcript, title?, meeting_date?}`. Analyses an existing transcript, skipping speech-to-text. | `202` |
| `GET` | `` | Lists meetings, newest first. Query parameters: `status`, `limit` (1–200), `offset`. | `200` |
| `GET` | `/{id}` | Audio metadata, transcript, transcript split into speaker segments, and the full analysis. | `200` |
| `POST` | `/{id}/query` | JSON `{question}`. Returns an answer grounded in the meeting, plus supporting quotes. | `200` |
| `DELETE` | `/{id}` | Deletes the meeting, its transcript and its analysis. | `200` |
| `GET` | `/health` | Liveness check, whether an API key is configured, and the active model. | `200` |

**Processing status:** `transcribing` → `analyzing` → `completed`, or `failed` (the reason is in `error_message`).

**Example query**

```http
POST /api/meetings/{id}/query
{"question": "What tasks were assigned to Rahul?"}
```
```json
{
  "meeting_id": "b74ad659-…",
  "question": "What tasks were assigned to Rahul?",
  "answer": "Rahul was assigned two tasks: complete the payment API integration by 10 September and investigate the timeout issue reported by the mobile team by tomorrow.",
  "answerable": true,
  "supporting_quotes": [
    "Rahul: I can complete the payment API integration by 10 September.",
    "Manager: Rahul, please also investigate the timeout issue reported by the mobile team.",
    "Rahul: I will check the timeout issue tomorrow."
  ]
}
```

If a question is about something the meeting never covered, the answer says so and `answerable` is `false`.

**Error responses**

| Code | When |
|---|---|
| `400` | The uploaded file is empty. |
| `404` | Meeting not found. |
| `409` | A question was asked before processing completed. |
| `413` | Audio is over `MAX_UPLOAD_MB`, or the transcript is over `MAX_TRANSCRIPT_CHARS`. |
| `415` | Unsupported file extension, or the file content isn't really audio (checked from the file header). |
| `422` | Validation error, e.g. a blank question or a transcript shorter than 20 characters. |
| `429` | Gemini quota or rate limit exceeded. |
| `502` | Gemini error, or the model output failed validation after a retry. |
| `503` | `GEMINI_API_KEY` is not configured. |

---

## Output format

This is the actual output for the example meeting in the brief (`meeting_date` = 2026-09-01), with the `evidence` quotes shortened:

```json
{
  "title": "Beta Launch Finalization and Action Items",
  "summary": "The team discussed the beta launch, confirming the launch date for September 15th. Key action items were assigned to Rahul and Priya regarding API integration, QA test cases, and investigating a timeout issue. The Manager also committed to providing necessary access, while product pricing remains an open item for future discussion.",
  "participants": [{"name": "Manager", "role": null}, {"name": "Rahul", "role": null}, {"name": "Priya", "role": null}],
  "decisions": [
    {"decision": "The beta launch date is confirmed for 15 September.", "decided_by": "Manager", "evidence": "…the beta launch date remains 15 September."}
  ],
  "action_items": [
    {"task": "Complete the payment API integration", "assignee": "Rahul", "deadline": "10 September", "deadline_date": "2026-09-10", "priority": "unspecified", "status": "open", "evidence": "…"},
    {"task": "Prepare the QA test cases", "assignee": "Priya", "deadline": "12 September", "deadline_date": "2026-09-12", "priority": "unspecified", "status": "open", "evidence": "…"},
    {"task": "Investigate the timeout issue reported by the mobile team", "assignee": "Rahul", "deadline": "tomorrow", "deadline_date": "2026-09-02", "priority": "unspecified", "status": "open", "evidence": "…"},
    {"task": "Provide access to the payment gateway test account", "assignee": "Manager", "deadline": "today", "deadline_date": "2026-09-01", "priority": "unspecified", "status": "open", "evidence": "…"}
  ],
  "risks_blockers": [
    {"type": "dependency", "description": "Rahul needs access to the payment gateway test account to proceed with integration.", "affects": "Rahul", "owner": "Manager", "evidence": "…"}
  ],
  "unresolved": [
    {"item": "Product pricing", "reason": "The product pricing is still not finalized.", "next_step": "Discuss in the next meeting."}
  ],
  "ambiguities": []
}
```

Every item in the brief's expected output appears: the decision, all four action items with deadlines, the pricing question as unresolved, and the gateway-access dependency.

---

## Model choices

| Task | Model | Notes |
|---|---|---|
| Speech-to-text and speaker labels | `gemini-3.5-flash` (configurable with `GEMINI_MODEL`) | Native audio input through the Gemini Files API (files up to 2 GB). Speakers are labelled by name or role when the conversation makes it clear. |
| Structured extraction | same model, `temperature=0`, `response_schema=MeetingAnalysis` | For audio uploads the model analyses the audio itself, so it can use tone and speaker turns. For pasted transcripts it analyses the text. The meeting date is passed in so relative deadlines can be turned into real dates. |
| Question answering | same model, `temperature=0.1`, `response_schema=QueryAnswer` | Gets the full transcript and the analysis JSON. Returns an answer, an `answerable` flag and verbatim supporting quotes. |

**Why Gemini Flash:** it accepts audio natively, supports structured output, has a long context window, and is cheap per request. Its main alternative is Whisper (speech-to-text) plus GPT or Claude (extraction), which would need two vendors and a separate speaker-separation step such as pyannote.

**Why the schema is designed this way**
- There are no default values. The model must fill every field, and genuinely missing data is `null` (e.g. `assignee: null`) rather than an invented placeholder.
- Fixed value lists are used for `priority` (`high | medium | low | unspecified`), `status` (`open | in_progress | done`) and risk `type` (`risk | blocker | dependency`).
- Decisions, action items and risks each carry an `evidence` quote, so every result can be traced back to the transcript.
- `deadline` keeps the spoken wording ("tomorrow"), and `deadline_date` holds the resolved ISO date.

---

## Handling edge cases

| Case | Handling |
|---|---|
| Unclear speech | The transcriber keeps speech verbatim. The `ambiguities` list is where unclear passages that affect a task, decision or deadline are flagged. |
| Missing assignee or deadline | Stored as `null`; the dashboard shows "Unassigned" or "Not set". |
| Relative deadlines ("today", "tomorrow") | Resolved against the `meeting_date`, which defaults to the upload date and can be set on upload. The original wording is kept as well. |
| A request plus its later acceptance | Merged into one action item, using the more specific deadline (e.g. the timeout investigation is due "tomorrow"). |
| Conflicting statements | The latest confirmed statement wins, and the conflict can be noted in `ambiguities`. |
| Deferred topics | Recorded as `unresolved`, with the deferral as `next_step` (e.g. pricing). |
| Questions about things not discussed | `answerable: false` with a plain "not discussed" answer. Quotes must come from the transcript. |
| Invalid model JSON | Regenerated once, then the meeting is marked failed with the validation error. |
| Gemini 429/5xx | Retried with exponential backoff. If Gemini asks to wait more than 60 s (daily quota exhausted), the app stops immediately rather than wasting more requests. |
| Bad uploads | Checked for: allowed extension, file header matching the format, empty file, and size limit (enforced while the file is being received, so large files never sit in memory). |
| Server restart during processing | Any meeting still in progress is marked `failed` ("Interrupted by a server restart") on the next startup. |
| Deleted while processing | The pipeline notices the row is gone and stops quietly. |

---

## Data model

There is one table, `meetings`. The analysis is stored as validated JSON because it is always read and written as a whole document.

| Column | Type | Notes |
|---|---|---|
| `id` | string (UUID) | Primary key |
| `title` | string | Generated by the model, or provided for transcripts |
| `filename`, `source_type` | string | `source_type` is `audio` or `transcript` |
| `status`, `error_message` | string, text | Indexed status, and the failure reason |
| `content_type`, `file_size_bytes`, `duration_seconds` | audio metadata | Duration is read from the file with `mutagen` |
| `meeting_date` | ISO date | Reference date for relative deadlines |
| `transcript` | text | "Speaker: text" lines; the API also returns them split into speaker segments |
| `analysis_json` | text (JSON) | A validated `MeetingAnalysis` |
| `model_name` | string | Model that produced the analysis |
| `created_at`, `updated_at` | timestamptz | `created_at` is indexed for listing |

---

## Configuration

All settings are environment variables (or `Task/.env`). Secrets are never committed.

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | — | **Required** for uploads and questions |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Any Gemini model that accepts audio |
| `DATABASE_URL` | `sqlite:///./data/meetings.db` | Any SQLAlchemy URL; `postgres://` URLs are converted automatically |
| `UPLOAD_DIR` | `./data/uploads` | Temporary audio storage (files deleted after processing) |
| `MAX_UPLOAD_MB` | `100` | Audio size limit |
| `MAX_TRANSCRIPT_CHARS` | `300000` | Pasted transcript size limit |
| `CORS_ORIGINS` | empty | Comma-separated origins, for a frontend on another domain |
| `HOST`, `PORT`, `RELOAD` | `127.0.0.1`, `8000`, `false` | Used by `run.py` |

Supported audio formats: `.mp3 .wav .m4a .aac .ogg .flac .webm`.

---

## Project structure

```
.
├── README.md
├── render.yaml                    # Render Blueprint (builds Task/Dockerfile)
├── 09-15-2026-Council-Meeting.mp3 # Sample recording
└── Task/
    ├── app/
    │   ├── main.py            # App setup, startup (DB init, stale-job recovery), /health, serves the dashboard
    │   ├── config.py          # Settings read from environment variables
    │   ├── database.py        # Engine/session setup, create tables + add missing columns
    │   ├── models.py          # SQLAlchemy MeetingModel and status values
    │   ├── schemas.py         # Pydantic schemas: model output (MeetingAnalysis, QueryAnswer) and API
    │   ├── service.py         # Gemini client, retries, pipelines, structured output validation
    │   └── api/endpoints.py   # REST routes, upload checks, error mapping
    ├── index.html             # Dashboard (upload, paste transcript, results, Q&A)
    ├── run.py                 # Local entry point
    ├── Dockerfile · docker-compose.yml
    ├── requirements.txt
    └── .env.example
```

---

## Limitations

- **In-process background jobs.** Processing runs in FastAPI background tasks inside the web process, so run a single worker. A production version would use a job queue (e.g. Celery/RQ + Redis, or Cloud Tasks) for retries and horizontal scaling.
- **Speaker names depend on context.** Names are only reliable when people say them or address each other. Otherwise speakers become "Speaker 1/2". There is no voice enrolment.
- **No timestamps.** The transcript has speaker turns but no timings.
- **Quality depends on the recording.** Overlapping speech, heavy accents or poor microphones reduce transcription accuracy, and errors carry through to extraction.
- **Questions cover one meeting at a time.** There is no search across meetings. That would need a vector index or full-text search over all meetings.
- **Gemini quotas.** The free tier allows only a small number of requests per day. Each audio meeting uses 2 requests and each question uses 1. Use a key with billing enabled for real use.
- **No authentication.** Every meeting is visible to anyone who can reach the API. Add auth (e.g. an API key or OAuth) and per-user ownership before exposing it publicly.
- **Audio is not kept.** Recordings are deleted after processing; only their metadata, transcript and analysis are stored.
- **Video** is not supported (it was optional in the brief).
