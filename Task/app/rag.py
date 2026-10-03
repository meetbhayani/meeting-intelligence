import hashlib
import logging
import os
from typing import Optional

import chromadb
import cohere

from app.config import settings
from app.models import MeetingModel
from app.schemas import MeetingAnalysis

logger = logging.getLogger(__name__)

EMBEDDING_BATCH_SIZE = 96


class RAGServiceUnavailable(RuntimeError):
    """Raised when embedding or vector retrieval is not configured or ready."""


def chunk_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Split text on line boundaries where possible, retaining a bounded overlap."""
    if max_chars < 1 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("RAG chunk size must be positive and overlap must be smaller than chunk size.")

    remaining = text.strip()
    chunks: list[str] = []
    while len(remaining) > max_chars:
        split_at = max_chars
        boundary_start = max_chars // 2
        for separator in ("\n", " "):
            boundary = remaining.rfind(separator, boundary_start, max_chars)
            if boundary > 0:
                split_at = boundary
                break
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[max(1, split_at - overlap_chars):].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def analysis_to_text(analysis_json: str) -> str:
    """Render validated analysis fields as labeled, searchable text."""
    analysis = MeetingAnalysis.model_validate_json(analysis_json)
    sections = [
        f"Meeting title: {analysis.title}",
        f"Summary: {analysis.summary}",
    ]
    for label, entries in (
        ("Participants", analysis.participants),
        ("Decisions", analysis.decisions),
        ("Action items", analysis.action_items),
        ("Risks, blockers, and dependencies", analysis.risks_blockers),
        ("Unresolved items", analysis.unresolved),
    ):
        if entries:
            sections.append(f"{label}:")
            sections.extend(entry.model_dump_json(exclude_none=True) for entry in entries)
    if analysis.ambiguities:
        sections.append("Ambiguities:\n" + "\n".join(analysis.ambiguities))
    return "\n\n".join(sections)


class MeetingRAG:
    def __init__(self):
        self._client: Optional[cohere.ClientV2] = None
        self._chroma: Optional[chromadb.PersistentClient] = None
        self._collection = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def cohere_client(self) -> cohere.ClientV2:
        if not settings.COHERE_API_KEY:
            raise RAGServiceUnavailable("COHERE_API_KEY is not configured on the server.")
        if self._client is None:
            self._client = cohere.ClientV2(api_key=settings.COHERE_API_KEY)
        return self._client

    @property
    def collection(self):
        if self._collection is None:
            os.makedirs(settings.CHROMA_PERSIST_DIRECTORY, exist_ok=True)
            self._chroma = chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIRECTORY)
            self._collection = self._chroma.get_or_create_collection(
                name=settings.CHROMA_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        try:
            for offset in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                response = self.cohere_client.embed(
                    model=settings.COHERE_EMBEDDING_MODEL,
                    texts=texts[offset:offset + EMBEDDING_BATCH_SIZE],
                    input_type=input_type,
                    embedding_types=["float"],
                    output_dimension=settings.COHERE_EMBEDDING_DIMENSION,
                )
                vectors.extend([list(vector) for vector in response.embeddings.float_])
        except RAGServiceUnavailable:
            raise
        except Exception as error:
            raise RAGServiceUnavailable(f"Cohere embedding request failed: {error}") from error
        if len(vectors) != len(texts):
            raise RAGServiceUnavailable("Cohere returned an unexpected number of embeddings.")
        return vectors

    def _meeting_documents(self, meeting: MeetingModel) -> list[tuple[str, str]]:
        sources: list[tuple[str, str]] = []
        if meeting.transcript and meeting.transcript.strip():
            sources.append(("transcript", meeting.transcript.strip()))
        if meeting.analysis_json:
            sources.append(("analysis", analysis_to_text(meeting.analysis_json)))
        if not sources:
            raise ValueError(f"Meeting {meeting.id} has no transcript or analysis to index.")
        return [
            (source, chunk)
            for source, text in sources
            for chunk in chunk_text(text, settings.RAG_CHUNK_SIZE, settings.RAG_CHUNK_OVERLAP)
        ]

    def index_meeting(self, meeting: MeetingModel) -> None:
        """Upsert transcript and analysis chunks for a meeting."""
        documents = self._meeting_documents(meeting)
        texts = [text for _, text in documents]
        embeddings = self._embed(texts, "search_document")
        ids: list[str] = []
        metadatas: list[dict] = []
        for index, (source, text) in enumerate(documents):
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            ids.append(f"{meeting.id}:{source}:{index}:{content_hash[:16]}")
            metadatas.append(
                {
                    "meeting_id": meeting.id,
                    "source": source,
                    "chunk_index": index,
                    "title": meeting.title or meeting.filename or "Meeting",
                    "meeting_date": meeting.meeting_date or "",
                }
            )

        self.collection.delete(where={"meeting_id": meeting.id})
        self.collection.upsert(ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings)
        logger.info("Indexed %d knowledge chunks for meeting %s.", len(texts), meeting.id)

    def rebuild(self, meetings: list[MeetingModel]) -> None:
        """Re-index all completed meetings, preserving per-meeting scope."""
        self._ready = False
        completed_ids = {meeting.id for meeting in meetings}
        for meeting in meetings:
            self.index_meeting(meeting)

        existing = self.collection.get(include=["metadatas"])
        stale_ids = [
            chunk_id
            for chunk_id, metadata in zip(existing["ids"], existing["metadatas"] or [])
            if metadata["meeting_id"] not in completed_ids
        ]
        if stale_ids:
            self.collection.delete(ids=stale_ids)
        self._ready = True
        logger.info("RAG index rebuild completed for %d meeting(s).", len(meetings))

    def retrieve(self, meeting_id: str, question: str) -> list[dict]:
        if not self._ready:
            raise RAGServiceUnavailable("RAG index is not ready; startup re-indexing has not completed.")
        question_embedding = self._embed([question], "search_query")[0]
        results = self.collection.query(
            query_embeddings=[question_embedding],
            n_results=settings.RAG_RETRIEVAL_COUNT,
            where={"meeting_id": meeting_id},
            include=["documents", "metadatas", "distances"],
        )
        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]
        return [
            {"text": text, "metadata": metadata, "distance": distance}
            for text, metadata, distance in zip(documents, metadatas, distances)
        ]

    def delete_meeting(self, meeting_id: str) -> None:
        if self._collection is not None:
            self._collection.delete(where={"meeting_id": meeting_id})


meeting_rag = MeetingRAG()
