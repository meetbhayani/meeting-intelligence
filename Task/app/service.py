import logging
import os
import re
import time
from datetime import date
from typing import List, Optional

from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.database import SessionLocal
from app.models import MeetingModel, MeetingStatus
from app.schemas import MeetingAnalysis, QueryAnswer, TranscriptSegment

logger = logging.getLogger(__name__)

RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_RETRY_WAIT_S = 60
_RETRY_IN = re.compile(r"retry in\s+(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.IGNORECASE)


class AIServiceUnavailable(RuntimeError):
    """Raised when the AI backend is not configured or keeps failing."""


def _suggested_retry_seconds(error: APIError) -> Optional[float]:
    """Gemini 429s say e.g. 'Please retry in 9h40m33s'; returns that wait in seconds."""
    match = _RETRY_IN.search(str(error.message or error))
    if not match or not any(match.groups()):
        return None
    h, m, s = match.groups()
    return int(h or 0) * 3600 + int(m or 0) * 60 + float(s or 0)


class GeminiService:
    def __init__(self):
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        # Created lazily so the API (health, listing) can start without a key configured.
        if self._client is None:
            if not settings.GEMINI_API_KEY:
                raise AIServiceUnavailable("GEMINI_API_KEY is not configured on the server.")
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    def _generate(self, contents, response_schema=None, temperature=0.0, retries=3, delay=4):
        """
        Calls Gemini with exponential backoff on transient 429/5xx upstream errors.
        """
        config = types.GenerateContentConfig(temperature=temperature)
        if response_schema:
            config.response_mime_type = "application/json"
            config.response_schema = response_schema

        for attempt in range(retries):
            try:
                return self.client.models.generate_content(
                    model=settings.GEMINI_MODEL, contents=contents, config=config
                )
            except APIError as e:
                if e.code not in RETRYABLE_CODES or attempt == retries - 1:
                    raise
                wait = _suggested_retry_seconds(e) or delay
                if wait > MAX_RETRY_WAIT_S:
                    raise  # daily quota exhausted: retrying only burns more requests
                logger.warning("Gemini returned %s. Retrying %d/%d in %.0fs...", e.code, attempt + 1, retries, wait)
                time.sleep(wait)
                delay *= 2

    def _generate_validated(self, contents, schema: type[BaseModel], temperature=0.0, attempts=2) -> BaseModel:
        """Structured generation + Pydantic validation; regenerates once if the JSON is invalid."""
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            response = self._generate(contents, response_schema=schema, temperature=temperature)
            if not response.text:
                last_error = ValueError("Model returned an empty response.")
                continue
            try:
                return schema.model_validate_json(response.text)
            except ValidationError as e:
                last_error = e
                logger.warning("Structured output failed validation, regenerating: %s", e)
        raise ValueError(f"Model output failed schema validation: {last_error}")

    def _upload_audio(self, path: str, mime_type: Optional[str], timeout_s: int = 300):
        """Uploads audio to the Gemini Files API and waits until it is ready for use."""
        remote = self.client.files.upload(file=path, config=types.UploadFileConfig(mime_type=mime_type))
        waited = 0
        while remote.state and remote.state.name == "PROCESSING":
            if waited >= timeout_s:
                raise TimeoutError("Gemini file processing timed out.")
            time.sleep(2)
            waited += 2
            remote = self.client.files.get(name=remote.name)
        if remote.state and remote.state.name == "FAILED":
            raise ValueError("Gemini could not process this audio file (corrupt or unsupported encoding).")
        return remote

    # ------------------------------------------------------------------
    # Pipelines (run as background tasks)
    # ------------------------------------------------------------------

    def run_processing_pipeline(self, meeting_id: str, temp_file_path: str, mime_type: Optional[str] = None):
        """
        Background worker pipeline executing Speech-To-Text and Structured Extraction.
        """
        audio_file_remote = None
        try:
            logger.info("Uploading %s to Gemini...", temp_file_path)
            audio_file_remote = self._upload_audio(temp_file_path, mime_type)

            # Step 1: Speech-To-Text Transcription
            logger.info("[%s] Generating transcript with speaker separation...", meeting_id)
            transcribe_prompt = (
                "Transcribe this meeting audio. Accurately detect and separate speakers by their name/role "
                "(e.g., Manager, Rahul, Priya) based on conversation headers. Output clean dialogue text lines."
            )
            transcript_response = self._generate([audio_file_remote, transcribe_prompt])
            transcript_text = (transcript_response.text or "").strip()
            if not transcript_text:
                raise ValueError("No speech could be transcribed from this audio.")

            meeting = self._update(meeting_id, transcript=transcript_text, status=MeetingStatus.ANALYZING)
            if meeting is None:
                return  # deleted while processing

            # Step 2: Strict Feature Extraction against target JSON Pydantic format schema
            logger.info("[%s] Extracting structured analysis...", meeting_id)
            analysis = self._extract(audio_file_remote, meeting.meeting_date)
            self._complete(meeting_id, analysis)
        except Exception as e:
            self._fail(meeting_id, e)
        finally:
            if audio_file_remote is not None:
                try:
                    self.client.files.delete(name=audio_file_remote.name)
                except Exception:
                    logger.warning("Could not delete remote Gemini file %s", audio_file_remote.name)
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

    def run_transcript_pipeline(self, meeting_id: str):
        """Structured extraction for a meeting submitted as text transcript (no STT step)."""
        try:
            with SessionLocal() as db:
                meeting = db.get(MeetingModel, meeting_id)
                if meeting is None:
                    return
                transcript, meeting_date = meeting.transcript, meeting.meeting_date
            analysis = self._extract(f"MEETING TRANSCRIPT:\n{transcript}", meeting_date)
            self._complete(meeting_id, analysis)
        except Exception as e:
            self._fail(meeting_id, e)

    def _extract(self, source, meeting_date: Optional[str]) -> MeetingAnalysis:
        extract_prompt = (
            "Analyze the meeting recording precisely. Extract exactly as structured:\n"
            "1. Summary text (Concise recap text)\n"
            "2. Decisions made (List of final confirmed decisions)\n"
            "3. Action items (Task, owner, deadline, priority, status)\n"
            "4. Risks/blockers/dependencies\n"
            "5. Unresolved items (Open points like pricing)\n"
            "6. Participants list"
        )
        meeting_date = meeting_date or date.today().isoformat()
        contents = [source, f"Meeting date: {meeting_date}", extract_prompt]
        return self._generate_validated(contents, MeetingAnalysis)

    # ------------------------------------------------------------------
    # Natural-language query
    # ------------------------------------------------------------------

    def answer_meeting_query(self, m: MeetingModel, question: str) -> QueryAnswer:
        """
        Grounded Natural Language Q&A.
        Queries the model directly using the complete transcript context stored in the database.
        """
        context_payload = (
            f"You are an expert project assistant. Answer the user query using only the complete "
            f"meeting transcript details and operational matrices provided below. If the answer "
            f"cannot be verified by the text facts, state clearly that it was not discussed.\n\n"
            f"MEETING TRANSCRIPT:\n{m.transcript}\n\n"
            f"SUMMARY STRUCTURED ANALYTICS:\n{m.analysis_json}\n\n"
            f"USER QUESTION: {question}"
        )
        return self._generate_validated(context_payload, QueryAnswer, temperature=0.1)

    # ------------------------------------------------------------------
    # Persistence helpers (each uses its own short-lived session)
    # ------------------------------------------------------------------

    def _update(self, meeting_id: str, **fields) -> Optional[MeetingModel]:
        with SessionLocal() as db:
            meeting = db.get(MeetingModel, meeting_id)
            if meeting is None:
                return None
            for key, value in fields.items():
                setattr(meeting, key, value)
            db.commit()
            db.refresh(meeting)
            db.expunge(meeting)
            return meeting

    def _complete(self, meeting_id: str, analysis: MeetingAnalysis):
        self._update(
            meeting_id,
            analysis_json=analysis.model_dump_json(),
            title=analysis.title,
            status=MeetingStatus.COMPLETED,
            error_message=None,
            model_name=settings.GEMINI_MODEL,
        )
        logger.info("[%s] Processing completed.", meeting_id)

    def _fail(self, meeting_id: str, error: Exception):
        logger.exception("[%s] Processing failed: %s", meeting_id, error)
        if isinstance(error, APIError) and error.code == 429:
            message = "Gemini API quota/rate limit exceeded. Wait for the quota to reset or use a key with billing enabled."
        elif isinstance(error, APIError):
            message = f"AI service error ({error.code}): {error.message or error}"
        else:
            message = str(error) or error.__class__.__name__
        try:
            self._update(meeting_id, status=MeetingStatus.FAILED, error_message=message[:1000])
        except Exception:
            logger.exception("[%s] Could not record failure state.", meeting_id)


_SPEAKER_LINE = re.compile(r"^\s*(?:\[[\d:.\s\-]+\]\s*)?\**([A-Z][\w .'()\-]{0,40}?)\**\s*:\s*(.+)$")


def parse_transcript_segments(transcript: Optional[str]) -> List[TranscriptSegment]:
    """Splits 'Speaker: text' lines into segments; lines without a speaker continue the previous turn."""
    segments: List[TranscriptSegment] = []
    for line in (transcript or "").splitlines():
        if not line.strip():
            continue
        match = _SPEAKER_LINE.match(line)
        if match:
            segments.append(TranscriptSegment(speaker=match.group(1).strip(), text=match.group(2).strip()))
        elif segments:
            segments[-1].text += " " + line.strip()
        else:
            segments.append(TranscriptSegment(speaker=None, text=line.strip()))
    return segments


gemini_service = GeminiService()
