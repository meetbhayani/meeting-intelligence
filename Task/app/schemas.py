from datetime import date, datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# LLM-facing schemas (passed to Gemini as response_schema and validated after).
# No default values: every field must be produced explicitly, nullable ones as null.
# ---------------------------------------------------------------------------

class Priority(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"
    unspecified = "unspecified"


class ActionStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    done = "done"


class RiskType(str, Enum):
    risk = "risk"
    blocker = "blocker"
    dependency = "dependency"


class Participant(BaseModel):
    name: str = Field(..., description="Name or label exactly as used in the transcript")
    role: Optional[str] = Field(..., description="Role/title if stated or clearly evident (e.g. 'Manager'); null otherwise")


class Decision(BaseModel):
    decision: str = Field(..., description="The confirmed/final decision, stated as a fact")
    decided_by: Optional[str] = Field(..., description="Who confirmed it; null if unclear")
    evidence: str = Field(..., description="Short supporting quote from the transcript")


class ActionItem(BaseModel):
    task: str = Field(..., description="What needs to be done, as a concise imperative phrase")
    assignee: Optional[str] = Field(..., description="Person responsible; null if nobody was assigned")
    deadline: Optional[str] = Field(..., description="Deadline as spoken (e.g. '10 September', 'tomorrow'); null if none mentioned")
    deadline_date: Optional[str] = Field(..., description="Deadline resolved to YYYY-MM-DD using the meeting date; null if it cannot be resolved reliably")
    priority: Priority = Field(..., description="Priority if stated or clearly implied by urgency; otherwise 'unspecified'")
    status: ActionStatus = Field(..., description="'open' for newly assigned work unless the meeting says otherwise")
    evidence: str = Field(..., description="Short supporting quote from the transcript")


class RiskItem(BaseModel):
    type: RiskType = Field(..., description="'blocker' = currently preventing work; 'dependency' = work waiting on someone/something; 'risk' = potential future problem")
    description: str = Field(..., description="What the risk/blocker/dependency is")
    affects: Optional[str] = Field(..., description="Task, person or milestone affected; null if unclear")
    owner: Optional[str] = Field(..., description="Who is responsible for resolving it; null if nobody")
    evidence: str = Field(..., description="Short supporting quote from the transcript")


class UnresolvedItem(BaseModel):
    item: str = Field(..., description="The open question or pending decision")
    reason: Optional[str] = Field(..., description="Why it remains open, as stated or evident in the meeting; null if no reason was given")
    next_step: Optional[str] = Field(..., description="Agreed follow-up (e.g. 'discuss in next meeting'); null if none")


class MeetingAnalysis(BaseModel):
    title: str = Field(..., description="Short descriptive meeting title (max ~8 words)")
    summary: str = Field(..., description="Concise 2-4 sentence summary of what was discussed and confirmed")
    participants: List[Participant] = Field(..., description="People who spoke in the meeting")
    decisions: List[Decision] = Field(..., description="Only confirmed/final decisions, not proposals or general discussion")
    action_items: List[ActionItem] = Field(..., description="One entry per distinct task; merge a request and its acceptance into one item")
    risks_blockers: List[RiskItem] = Field(..., description="Risks, blockers and dependencies")
    unresolved: List[UnresolvedItem] = Field(..., description="Questions or decisions left open")
    ambiguities: List[str] = Field(..., description="Unclear speech, conflicting statements, missing assignees/deadlines worth flagging; empty if none")


class QueryAnswer(BaseModel):
    answer: str = Field(..., description="Direct answer grounded only in the meeting content")
    answerable: bool = Field(..., description="False if the meeting does not contain the information asked for")
    supporting_quotes: List[str] = Field(..., description="Up to 5 short verbatim transcript quotes supporting the answer")


# ---------------------------------------------------------------------------
# API request / response schemas
# ---------------------------------------------------------------------------

class TranscriptSubmission(BaseModel):
    transcript: str = Field(..., min_length=20, description="Meeting transcript text, ideally 'Speaker: text' per line")
    title: Optional[str] = Field(None, max_length=200)
    meeting_date: Optional[date] = Field(None, description="Used to resolve relative deadlines; defaults to today")

    @field_validator("transcript")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("transcript must not be blank")
        return v.strip()


class UploadAccepted(BaseModel):
    meeting_id: str
    status: str
    message: str


class MeetingListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: Optional[str] = None
    filename: str
    source_type: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    meeting_date: Optional[str] = None
    duration_seconds: Optional[float] = None
    created_at: Optional[datetime] = None


class TranscriptSegment(BaseModel):
    speaker: Optional[str] = None
    text: str


class AudioMetadata(BaseModel):
    filename: str
    content_type: Optional[str] = None
    file_size_bytes: Optional[int] = None
    duration_seconds: Optional[float] = None


class MeetingDetail(MeetingListItem):
    audio: Optional[AudioMetadata] = None
    transcript: Optional[str] = None
    transcript_segments: Optional[List[TranscriptSegment]] = None
    analysis: Optional[MeetingAnalysis] = None
    model_name: Optional[str] = None
    updated_at: Optional[datetime] = None


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank")
        return v.strip()


class QueryResponse(BaseModel):
    meeting_id: str
    question: str
    answer: str
    answerable: bool
    supporting_quotes: List[str]
