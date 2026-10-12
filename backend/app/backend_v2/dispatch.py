"""Bounded production handlers for A-C. No browser/LLM/LangGraph is invoked."""
from .documents import DocumentStore
from .embedding import EmbeddingUnavailable
from .rag import RagService
from .workers import RetryableWork, WorkResult


def get_handlers(db) -> dict:
    rag = RagService(db)  # Model loads lazily only in an indexing child process.
    documents = DocumentStore(db)

    def index(job):
        if job.payload.get("source_revision_id", job.aggregate_id) != job.aggregate_id:
            return WorkResult("failed", {"reason": "source_identity_mismatch"})
        try:
            result = rag.index_source(job.user_id, job.aggregate_id)
        except EmbeddingUnavailable:
            raise RetryableWork("embedding_unavailable", 60)
        except (ValueError, LookupError):
            return WorkResult("failed", {"reason": "invalid_or_missing_source"})
        state = "obsolete" if result["status"] in {"obsolete", "superseded"} else "completed"
        return WorkResult(state, result)

    def document(job):
        if job.payload.get("file_id", job.aggregate_id) != job.aggregate_id:
            return WorkResult("failed", {"reason": "file_identity_mismatch"})
        try:
            result, persist = documents.parse(job.user_id, job.aggregate_id)
        except (ValueError, LookupError, OSError):
            return WorkResult("failed", {"reason": "invalid_or_missing_document"})
        return WorkResult("completed", result, persist)

    # Workflows have one entry point: WorkerDispatcher's version-pinned engine.
    # Registering a placeholder here used to shadow the injected graph engine.
    return {"document.parse": document, "rag.index": index}
