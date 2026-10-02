from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Float, Integer, String, Text
from app.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class MeetingStatus:
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"

    IN_PROGRESS = (TRANSCRIBING, ANALYZING)


class MeetingModel(Base):
    __tablename__ = "meetings"

    id = Column(String, primary_key=True, index=True)
    title = Column(String, nullable=True)
    filename = Column(String, nullable=False)
    source_type = Column(String, nullable=True)  # "audio" | "transcript"
    status = Column(String, nullable=False, index=True)
    error_message = Column(Text, nullable=True)

    # Audio metadata
    content_type = Column(String, nullable=True)
    file_size_bytes = Column(Integer, nullable=True)
    duration_seconds = Column(Float, nullable=True)

    # Reference date used to resolve relative deadlines ("tomorrow") — ISO YYYY-MM-DD
    meeting_date = Column(String, nullable=True)

    transcript = Column(Text, nullable=True)  # readable "Speaker: text" lines
    analysis_json = Column(Text, nullable=True)  # validated MeetingAnalysis JSON

    model_name = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
