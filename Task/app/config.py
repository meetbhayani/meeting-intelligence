import os
from dotenv import load_dotenv

load_dotenv()


def _normalize_db_url(url: str) -> str:
    # Render/Heroku hand out "postgres://" URLs; SQLAlchemy 2.x needs an explicit driver.
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings:
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_TRANSCRIPTION_MODEL: str = os.getenv("GEMINI_TRANSCRIPTION_MODEL", "gemini-3.5-transcribe")
    GEMINI_TRANSCRIPTION_FALLBACK_MODELS: list = [
        m.strip()
        for m in os.getenv("GEMINI_TRANSCRIPTION_FALLBACK_MODELS", "gemini-3.5-flash-lite").split(",")
        if m.strip()
    ]
    GEMINI_ANALYSIS_MODEL: str = os.getenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash-lite")
    # Analysis fallback retains the previous Flash -> Flash Lite failover behavior.
    GEMINI_FALLBACK_MODELS: list = [
        m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",") if m.strip()
    ]
    COHERE_API_KEY: str = os.getenv("COHERE_API_KEY", "")
    COHERE_EMBEDDING_MODEL: str = os.getenv("COHERE_EMBEDDING_MODEL", "embed-v4.0")
    COHERE_EMBEDDING_DIMENSION: int = int(os.getenv("COHERE_EMBEDDING_DIMENSION", "1024"))
    CHROMA_PERSIST_DIRECTORY: str = os.getenv("CHROMA_PERSIST_DIRECTORY", "./data/chroma_db")
    CHROMA_COLLECTION_NAME: str = os.getenv("CHROMA_COLLECTION_NAME", "meeting_knowledge")
    RAG_CHUNK_SIZE: int = int(os.getenv("RAG_CHUNK_SIZE", "3500"))
    RAG_CHUNK_OVERLAP: int = int(os.getenv("RAG_CHUNK_OVERLAP", "400"))
    RAG_RETRIEVAL_COUNT: int = int(os.getenv("RAG_RETRIEVAL_COUNT", "6"))
    DATABASE_URL: str = _normalize_db_url(os.getenv("DATABASE_URL", "sqlite:///./data/meetings.db"))
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "./data/uploads")
    MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "100"))
    MAX_TRANSCRIPT_CHARS: int = int(os.getenv("MAX_TRANSCRIPT_CHARS", "300000"))
    CORS_ORIGINS: list = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

    # Extension -> MIME type sent to Gemini.
    AUDIO_MIME_TYPES: dict = {
        ".mp3": "audio/mp3",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
    }

    @property
    def GEMINI_TRANSCRIPTION_MODELS(self) -> list[str]:
        return self._models_for(self.GEMINI_TRANSCRIPTION_MODEL, self.GEMINI_TRANSCRIPTION_FALLBACK_MODELS)

    @property
    def GEMINI_ANALYSIS_MODELS(self) -> list[str]:
        return self._models_for(self.GEMINI_ANALYSIS_MODEL, self.GEMINI_FALLBACK_MODELS)

    @property
    def GEMINI_MODELS(self) -> list[str]:
        """Backward-compatible alias for the analysis model candidates."""
        return self.GEMINI_ANALYSIS_MODELS

    def _models_for(self, primary_model: str, fallback_models: list[str]) -> list[str]:
        return [primary_model] + [m for m in fallback_models if m != primary_model]

    @property
    def ALLOWED_EXTENSIONS(self) -> set:
        return set(self.AUDIO_MIME_TYPES)

    @property
    def MAX_UPLOAD_BYTES(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024


settings = Settings()
