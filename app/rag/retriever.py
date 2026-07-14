"""Mock parent-child RAG retriever over static markdown policy documents.

Parents are the natural "## " sections of each document (~1,500 chars) — the
units returned to the LLM for full context. Children are ~350-char windows
within each parent, scored against the query via TF-IDF cosine similarity
(no embedding API — keeps the demo fully offline). This is "small-to-large"
retrieval: precise matching on small chunks, full-context generation from
their parents.

Production swap: replace the TF-IDF vectorizer with an embedding model and
back the child index with pgvector on the same Neon instance — the
retrieve_policies() interface and the ParentChunk/ChildChunk shapes stay the
same.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.exceptions import PolicyRetrievalError

_DOCUMENTS_DIR = Path(__file__).parent / "documents"
_GENERAL_DOC = "underwriting_general.md"
_PARENT_MAX_CHARS = 1500
_CHILD_WINDOW_CHARS = 350
_CHILD_STRIDE_CHARS = 300
_MIN_PARENT_CHARS = 40  # drops the near-empty "# Title" fragment before the first "## " header


@dataclass(frozen=True, slots=True)
class ParentChunk:
    id: str
    source: str
    heading: str
    text: str


@dataclass(frozen=True, slots=True)
class ChildChunk:
    id: str
    parent_id: str
    text: str


def _split_into_parents(source: str, raw_text: str) -> list[ParentChunk]:
    """Splits on '## ' headers. A section that still exceeds _PARENT_MAX_CHARS
    is further split into multiple same-heading parent chunks."""
    sections = re.split(r"(?=^## )", raw_text, flags=re.MULTILINE)
    parents: list[ParentChunk] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        heading_match = re.match(r"^##\s+(.+)$", section, flags=re.MULTILINE)
        if heading_match is None:
            # Preamble before the first "## " header (e.g. the "# Title" line) —
            # not a designed parent chunk per the parent-child split, so skip it.
            continue
        heading = heading_match.group(1).strip()

        for start in range(0, len(section), _PARENT_MAX_CHARS):
            chunk_text = section[start : start + _PARENT_MAX_CHARS].strip()
            if len(chunk_text) < _MIN_PARENT_CHARS:
                continue
            parent_id = f"{source}::{heading}::{start}"
            parents.append(
                ParentChunk(id=parent_id, source=source, heading=heading, text=chunk_text)
            )
    return parents


def _split_into_children(parent: ParentChunk) -> list[ChildChunk]:
    text = parent.text
    if len(text) <= _CHILD_WINDOW_CHARS:
        return [ChildChunk(id=f"{parent.id}::0", parent_id=parent.id, text=text)]

    children: list[ChildChunk] = []
    start = 0
    idx = 0
    while start < len(text):
        window = text[start : start + _CHILD_WINDOW_CHARS]
        children.append(ChildChunk(id=f"{parent.id}::{idx}", parent_id=parent.id, text=window))
        if start + _CHILD_WINDOW_CHARS >= len(text):
            break
        start += _CHILD_STRIDE_CHARS
        idx += 1
    return children


class PolicyRetriever:
    """Loads and chunks the markdown corpus once at construction. Each
    retrieve_policies() call re-scores against a state-scoped subset (the
    requested state's doc + the always-included general underwriting doc) —
    this keeps a CA query from ever surfacing NY/TX-only clauses."""

    def __init__(self, documents_dir: Path = _DOCUMENTS_DIR):
        self._documents_dir = Path(documents_dir)
        self._parents_by_source: dict[str, list[ParentChunk]] = {}
        self._children_by_source: dict[str, list[ChildChunk]] = {}
        self._load_documents()

    def _load_documents(self) -> None:
        try:
            md_files = sorted(self._documents_dir.glob("*.md"))
            if not md_files:
                raise PolicyRetrievalError(f"no policy documents found in {self._documents_dir}")
            for path in md_files:
                raw_text = path.read_text(encoding="utf-8")
                parents = _split_into_parents(path.name, raw_text)
                self._parents_by_source[path.name] = parents
                children: list[ChildChunk] = []
                for parent in parents:
                    children.extend(_split_into_children(parent))
                self._children_by_source[path.name] = children
        except OSError as exc:
            raise PolicyRetrievalError(f"failed to load policy documents: {exc}") from exc

    def _scoped_sources(self, state_code: str) -> list[str]:
        state_doc = f"policy_{state_code.upper()}.md"
        sources = []
        if state_doc in self._parents_by_source:
            sources.append(state_doc)
        if _GENERAL_DOC in self._parents_by_source:
            sources.append(_GENERAL_DOC)
        return sources

    def get_state_policies(self, state_code: str) -> list[ParentChunk]:
        """All parent chunks scoped to state_code + the general doc, unranked
        — for callers (e.g. the MCP compliance resource) that want the full
        text rather than a query-ranked top-k."""
        sources = self._scoped_sources(state_code)
        parents: list[ParentChunk] = []
        seen_ids: set[str] = set()
        for source in sources:
            for parent in self._parents_by_source[source]:
                if parent.id not in seen_ids:
                    seen_ids.add(parent.id)
                    parents.append(parent)
        return parents

    def retrieve_policies(self, query: str, state_code: str, k: int = 3) -> list[ParentChunk]:
        """Scores children, takes the top-k, and returns their deduplicated
        parents (in ranked order) — so the result can contain fewer than k
        parents if several top children share one parent."""
        sources = self._scoped_sources(state_code)
        if not sources:
            raise PolicyRetrievalError(f"no policy documents available for state '{state_code}'")

        parents_by_id: dict[str, ParentChunk] = {}
        children: list[ChildChunk] = []
        for source in sources:
            for parent in self._parents_by_source[source]:
                parents_by_id[parent.id] = parent
            children.extend(self._children_by_source[source])

        if not children:
            return []

        try:
            corpus = [c.text for c in children] + [query]
            vectorizer = TfidfVectorizer(stop_words="english")
            tfidf = vectorizer.fit_transform(corpus)
        except ValueError:
            # Empty vocabulary (e.g. the query is only stopwords/punctuation) —
            # fall back to the first k parents of the scoped corpus.
            fallback_ids: list[str] = []
            for source in sources:
                for parent in self._parents_by_source[source]:
                    if parent.id not in fallback_ids:
                        fallback_ids.append(parent.id)
            return [parents_by_id[pid] for pid in fallback_ids[:k]]

        query_vector = tfidf[-1]
        child_vectors = tfidf[:-1]
        scores = cosine_similarity(query_vector, child_vectors).flatten()

        ranked_indices = scores.argsort()[::-1][:k]
        seen_parent_ids: list[str] = []
        for idx in ranked_indices:
            if scores[idx] <= 0:
                continue
            parent_id = children[idx].parent_id
            if parent_id not in seen_parent_ids:
                seen_parent_ids.append(parent_id)

        return [parents_by_id[pid] for pid in seen_parent_ids]


_default_retriever: PolicyRetriever | None = None


def get_retriever() -> PolicyRetriever:
    global _default_retriever
    if _default_retriever is None:
        _default_retriever = PolicyRetriever()
    return _default_retriever


def retrieve_policies(query: str, state_code: str, k: int = 3) -> list[ParentChunk]:
    """Module-level convenience wrapper matching the plan's stated interface."""
    return get_retriever().retrieve_policies(query, state_code, k=k)


def get_state_policies(state_code: str) -> list[ParentChunk]:
    """Module-level convenience wrapper mirroring retrieve_policies() above."""
    return get_retriever().get_state_policies(state_code)
