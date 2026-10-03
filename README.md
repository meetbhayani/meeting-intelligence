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
- **AI:** Gemini (transcription, structured analysis and Q&A) + Cohere Embed v4.0 (RAG embeddings)
- **RAG Q&A:** Meeting-scoped ChromaDB retrieval with Gemini Flash Lite answer generation
- **Database:** SQLite (local) / PostgreSQL (production)
- **Frontend:** HTML + Tailwind CSS
- **Deployment:** Docker, Render

## Getting Started

**Prerequisites:** Python 3.11+, a [Gemini API key](https://aistudio.google.com/apikey), and a Cohere API key.

```bash
cd Task
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # add your GEMINI_API_KEY and COHERE_API_KEY
python run.py
```

Open <http://127.0.0.1:8000> for the dashboard, or <http://127.0.0.1:8000/docs> for the API docs.

**Using Docker**

```bash
cd Task
GEMINI_API_KEY=your-gemini-key COHERE_API_KEY=your-cohere-key docker compose up --build
```

In Windows PowerShell, set `$env:GEMINI_API_KEY` and `$env:COHERE_API_KEY` before running `docker compose up --build`.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `GEMINI_API_KEY` | Gemini API key (required) | – |
| `GEMINI_TRANSCRIPTION_MODEL` | Primary model used to transcribe uploaded audio | `gemini-3.5-transcribe` |
| `GEMINI_TRANSCRIPTION_FALLBACK_MODELS` | Models tried if transcription is overloaded | `gemini-3.5-flash-lite` |
| `GEMINI_ANALYSIS_MODEL` | Model used for meeting analysis and current Q&A | `gemini-3.5-flash-lite` |
| `GEMINI_FALLBACK_MODELS` | Analysis fallback if the selected model is overloaded | `gemini-3.5-flash` |
| `COHERE_API_KEY` | Required for meeting embeddings and RAG retrieval | – |
| `COHERE_EMBEDDING_MODEL` | Cohere embedding model | `embed-v4.0` |
| `COHERE_EMBEDDING_DIMENSION` | Embedding vector dimension | `1024` |
| `CHROMA_PERSIST_DIRECTORY` | Local ChromaDB persistence directory | `./data/chroma_db` |
| `RAG_CHUNK_SIZE` | Maximum chunk size in characters | `3500` |
| `RAG_CHUNK_OVERLAP` | Text overlap between chunks, in characters | `400` |
| `RAG_RETRIEVAL_COUNT` | Maximum evidence chunks retrieved per question | `6` |
| `DATABASE_URL` | Database connection string | `sqlite:///./data/meetings.db` |
| `MAX_UPLOAD_MB` | Max audio file size | `100` |

See `Task/.env.example` for all options.

## How RAG Q&A Works

1. After a meeting has been transcribed and analyzed, the app prepares searchable text from both the transcript and its structured analysis.
2. The text is split into overlapping chunks. `RAG_CHUNK_SIZE` and `RAG_CHUNK_OVERLAP` are measured in characters; chunking prefers line and word boundaries.
3. Cohere `embed-v4.0` embeds document chunks using `search_document`. The vectors, chunk text, and meeting metadata are stored in the `meeting_knowledge` ChromaDB collection.
4. For a question, the app embeds it using `search_query`, retrieves up to `RAG_RETRIEVAL_COUNT` chunks filtered to that meeting, and passes only that evidence to Gemini `gemini-3.5-flash-lite`.
5. Gemini is instructed to answer from retrieved evidence and provide supporting quotes from transcript chunks. If the evidence is insufficient, it should say so.

The relational database remains the source of truth for meeting transcripts and analyses; ChromaDB is a rebuildable vector index. On startup, completed meetings are re-indexed into ChromaDB. On Render's free service, the local filesystem is ephemeral, so the ChromaDB index may be lost on restart or redeploy and rebuilt from PostgreSQL. Rebuilding re-embeds stored content through Cohere and can take time or consume API quota. The configured free PostgreSQL database also has a limited lifetime, so this setup is intended for a demo rather than durable production storage.

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
The health response includes `rag_configured` and `rag_index_ready` to show whether RAG credentials are present and startup indexing has completed.

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
3. Provide both `GEMINI_API_KEY` and `COHERE_API_KEY` when prompted, then click **Apply**.

Render creates the web service and a PostgreSQL database automatically. The `/health` response reports whether RAG is configured and whether startup indexing has completed.

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
    │   ├── rag.py         # Cohere embeddings, ChromaDB indexing and retrieval
    │   └── api/endpoints.py
    ├── index.html         # Dashboard
    ├── Dockerfile
    └── requirements.txt
```

## Limitations

- Background jobs run in-process, so use a single server worker.
- RAG requires valid Gemini and Cohere API keys. ChromaDB data is rebuildable and is not durable on Render's free filesystem.
- Startup re-indexing requires Cohere API access for each stored completed meeting; a Cohere outage or exhausted quota leaves RAG Q&A unavailable until indexing succeeds.
- The Render free PostgreSQL database is temporary and should not be treated as long-term storage.
- Speaker names are detected from conversation context; otherwise speakers appear as "Speaker 1", "Speaker 2".
- Accuracy depends on audio quality.
- Gemini free-tier quotas are low; use a billing-enabled key for heavier use.
- No authentication is included.
