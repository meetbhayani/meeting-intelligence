import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.endpoints import router as api_router
from app.config import settings
from app.database import SessionLocal, init_db
from app.models import MeetingModel, MeetingStatus
from app.rag import RAGServiceUnavailable, meeting_rag

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

INDEX_HTML = Path(__file__).resolve().parent.parent / "index.html"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    # Background tasks live in-process: anything still "in progress" at boot was interrupted.
    with SessionLocal() as db:
        stale = db.query(MeetingModel).filter(MeetingModel.status.in_(MeetingStatus.IN_PROGRESS + ("processing",)))
        count = stale.update(
            {MeetingModel.status: MeetingStatus.FAILED, MeetingModel.error_message: "Interrupted by a server restart."},
            synchronize_session=False,
        )
        db.commit()
        if count:
            logger.warning("Marked %d interrupted meeting(s) as failed.", count)
        completed = db.query(MeetingModel).filter(MeetingModel.status == MeetingStatus.COMPLETED).all()
        if completed:
            try:
                meeting_rag.rebuild(completed)
            except RAGServiceUnavailable:
                logger.exception("RAG startup re-indexing failed; meeting Q&A will be unavailable.")
        elif settings.COHERE_API_KEY:
            meeting_rag.rebuild([])
    if not settings.COHERE_API_KEY:
        logger.warning("COHERE_API_KEY is not set: RAG indexing and meeting Q&A will return 503.")
    if not settings.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is not set: uploads and queries will return 503.")
    yield


app = FastAPI(
    title="AI Meeting Intelligence & Action Tracker",
    description="Upload meeting audio -> transcript -> structured analysis (decisions, actions, risks) -> Q&A.",
    version="2.1.0",
    lifespan=lifespan,
)

if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router)


@app.get("/health", tags=["Ops"])
def health():
    return {
        "status": "ok",
        "ai_configured": bool(settings.GEMINI_API_KEY),
        "model": settings.GEMINI_ANALYSIS_MODEL,
        "transcription_model": settings.GEMINI_TRANSCRIPTION_MODEL,
        "analysis_model": settings.GEMINI_ANALYSIS_MODEL,
        "rag_configured": bool(settings.COHERE_API_KEY),
        "rag_index_ready": meeting_rag.ready,
        "embedding_model": settings.COHERE_EMBEDDING_MODEL,
    }


@app.get("/", tags=["Frontend"], include_in_schema=False)
async def serve_frontend():
    """Serves the single-page dashboard."""
    return FileResponse(INDEX_HTML)
