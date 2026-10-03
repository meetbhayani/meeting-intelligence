import logging
import os
import uuid
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from google.genai.errors import APIError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import MeetingModel, MeetingStatus
from app.rag import RAGServiceUnavailable, meeting_rag
from app.schemas import (
    AudioMetadata,
    MeetingAnalysis,
    MeetingDetail,
    MeetingListItem,
    QueryRequest,
    QueryResponse,
    TranscriptSubmission,
    UploadAccepted,
)
from app.service import AIServiceUnavailable, gemini_service, parse_transcript_segments

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meetings", tags=["Meetings"])

CHUNK_SIZE = 1024 * 1024


def _looks_like_audio(header: bytes, ext: str) -> bool:
    """Cheap magic-byte check so renamed non-audio files are rejected before hitting the AI."""
    if ext == ".wav":
        return header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    if ext == ".mp3":
        return header[:3] == b"ID3" or (len(header) > 1 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0)
    if ext == ".ogg":
        return header[:4] == b"OggS"
    if ext == ".flac":
        return header[:4] == b"fLaC"
    if ext == ".m4a":
        return header[4:8] == b"ftyp"
    if ext == ".webm":
        return header[:4] == b"\x1a\x45\xdf\xa3"
    if ext == ".aac":
        return header[:3] == b"ID3" or (len(header) > 1 and header[0] == 0xFF and (header[1] & 0xF6) == 0xF0)
    return False


def _audio_duration(path: str) -> Optional[float]:
    try:
        import mutagen

        audio = mutagen.File(path)
        if audio is not None and audio.info and audio.info.length:
            return round(float(audio.info.length), 2)
    except Exception:
        logger.info("Could not read audio duration for %s", path)
    return None


def _ensure_ai_configured():
    if not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="AI service is not configured (GEMINI_API_KEY missing).")
    if not settings.COHERE_API_KEY:
        raise HTTPException(status_code=503, detail="RAG service is not configured (COHERE_API_KEY missing).")


def _get_meeting_or_404(db: Session, meeting_id: str) -> MeetingModel:
    m = db.get(MeetingModel, meeting_id)
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    return m


def _to_detail(m: MeetingModel) -> MeetingDetail:
    analysis = None
    if m.analysis_json:
        try:
            analysis = MeetingAnalysis.model_validate_json(m.analysis_json)
        except ValidationError:
            logger.warning("Stored analysis for %s does not match the current schema.", m.id)

    audio = None
    if m.source_type != "transcript":
        audio = AudioMetadata(
            filename=m.filename,
            content_type=m.content_type,
            file_size_bytes=m.file_size_bytes,
            duration_seconds=m.duration_seconds,
        )

    return MeetingDetail(
        **MeetingListItem.model_validate(m).model_dump(),
        audio=audio,
        transcript=m.transcript,
        transcript_segments=parse_transcript_segments(m.transcript) if m.transcript else None,
        analysis=analysis,
        model_name=m.model_name,
        updated_at=m.updated_at,
    )


@router.post("/upload", status_code=202, response_model=UploadAccepted)
async def upload_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Meeting recording (MP3, WAV, M4A, OGG, FLAC, WEBM, AAC)"),
    meeting_date: Optional[date] = Form(None, description="YYYY-MM-DD; used to resolve 'today'/'tomorrow'. Defaults to today."),
    db: Session = Depends(get_db),
):
    """Upload a meeting recording and start transcription + analysis in the background."""
    _ensure_ai_configured()

    filename = os.path.basename(file.filename or "")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in settings.ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(settings.ALLOWED_EXTENSIONS))
        raise HTTPException(status_code=415, detail=f"Unsupported file type '{ext or 'none'}'. Allowed: {allowed}")

    meeting_id = str(uuid.uuid4())
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    temp_file_path = os.path.join(settings.UPLOAD_DIR, f"{meeting_id}{ext}")

    # Stream to disk in chunks so large files never sit fully in memory, enforcing the size cap.
    size = 0
    header = b""
    try:
        with open(temp_file_path, "wb") as buffer:
            while chunk := await file.read(CHUNK_SIZE):
                if not header:
                    header = chunk[:16]
                size += len(chunk)
                if size > settings.MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.")
                buffer.write(chunk)

        if size == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        if not _looks_like_audio(header, ext):
            raise HTTPException(status_code=415, detail=f"File content does not look like a valid {ext} audio file.")
    except HTTPException:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        raise

    duration = _audio_duration(temp_file_path)

    db_meeting = MeetingModel(
        id=meeting_id,
        filename=filename,
        source_type="audio",
        status=MeetingStatus.TRANSCRIBING,
        content_type=settings.AUDIO_MIME_TYPES[ext],
        file_size_bytes=size,
        duration_seconds=duration,
        meeting_date=(meeting_date or date.today()).isoformat(),
    )
    db.add(db_meeting)
    db.commit()

    background_tasks.add_task(
        gemini_service.run_processing_pipeline, meeting_id, temp_file_path, settings.AUDIO_MIME_TYPES[ext]
    )

    return UploadAccepted(
        meeting_id=meeting_id,
        status=db_meeting.status,
        message="Audio uploaded. Transcription and analysis started; poll GET /api/meetings/{id} for status.",
    )


@router.post("/transcript", status_code=202, response_model=UploadAccepted)
def submit_transcript(payload: TranscriptSubmission, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Submit an existing text transcript (testing path that skips speech-to-text)."""
    _ensure_ai_configured()
    if len(payload.transcript) > settings.MAX_TRANSCRIPT_CHARS:
        raise HTTPException(status_code=413, detail=f"Transcript exceeds {settings.MAX_TRANSCRIPT_CHARS} characters.")

    meeting_id = str(uuid.uuid4())
    db_meeting = MeetingModel(
        id=meeting_id,
        title=payload.title,
        filename=payload.title or "Pasted transcript",
        source_type="transcript",
        status=MeetingStatus.ANALYZING,
        transcript=payload.transcript,
        meeting_date=(payload.meeting_date or date.today()).isoformat(),
    )
    db.add(db_meeting)
    db.commit()

    background_tasks.add_task(gemini_service.run_transcript_pipeline, meeting_id)
    return UploadAccepted(
        meeting_id=meeting_id,
        status=db_meeting.status,
        message="Transcript accepted. Analysis started; poll GET /api/meetings/{id} for status.",
    )


@router.get("", response_model=List[MeetingListItem])
def list_meetings(
    status: Optional[str] = Query(None, description="Filter by status: transcribing, analyzing, completed, failed"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List meetings, newest first."""
    q = db.query(MeetingModel)
    if status:
        q = q.filter(MeetingModel.status == status)
    return q.order_by(MeetingModel.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/{id}", response_model=MeetingDetail)
def get_meeting(id: str, db: Session = Depends(get_db)):
    """Audio metadata, transcript and AI analysis for one meeting."""
    return _to_detail(_get_meeting_or_404(db, id))


@router.post("/{id}/query", response_model=QueryResponse)
def query_meeting(id: str, payload: QueryRequest, db: Session = Depends(get_db)):
    """Ask a natural-language question about a processed meeting."""
    m = _get_meeting_or_404(db, id)
    if m.status != MeetingStatus.COMPLETED:
        raise HTTPException(status_code=409, detail=f"Meeting is not ready for questions (status: {m.status}).")

    try:
        result = gemini_service.answer_meeting_query(m, payload.question)
    except AIServiceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RAGServiceUnavailable as e:
        logger.exception("RAG query failed")
        raise HTTPException(status_code=503, detail=str(e))
    except APIError as e:
        logger.exception("Gemini query failed")
        if e.code == 429:
            raise HTTPException(
                status_code=429,
                detail="Gemini API quota/rate limit exceeded. Wait for the quota to reset or use a key with billing enabled.",
            )
        if e.code in (500, 502, 503, 504):
            raise HTTPException(status_code=503, detail="Gemini is temporarily overloaded. Please try again in a minute.")
        raise HTTPException(status_code=502, detail=f"AI service error ({e.code}). Please retry.")
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return QueryResponse(
        meeting_id=m.id,
        question=payload.question,
        answer=result.answer,
        answerable=result.answerable,
        supporting_quotes=result.supporting_quotes,
    )


@router.delete("/{id}")
def delete_meeting(id: str, db: Session = Depends(get_db)):
    """Remove a meeting and its transcript/analysis."""
    m = _get_meeting_or_404(db, id)
    try:
        db.delete(m)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Delete failed for %s", id)
        raise HTTPException(status_code=500, detail="Could not delete the meeting. Please try again.")
    try:
        meeting_rag.delete_meeting(id)
    except Exception:
        logger.exception("Could not remove ChromaDB vectors for deleted meeting %s.", id)
    return {"message": f"Meeting {id} deleted."}
