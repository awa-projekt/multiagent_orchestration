"""Local retrieval: a `KnowledgeBase` of company documents and its `search_knowledge` tool.

Chunks are ranked with BM25 (keyword relevance, no service or extra dependency); an optional
`embed` function adds semantic similarity, merged by reciprocal rank fusion. Folders load
Markdown, text, CSV, HTML, JSON and (with the `files` extra) PDF and Word.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import Field

from awa._internal.logging import get_logger
from awa._internal.text import STOPWORDS, terms, words
from awa._internal.upstream.tools import BaseTool
from awa._internal.util import is_within
from awa.errors import ConfigurationError
from awa.toolkits._common import ToolArgs, build_tool, extra_installed, positive
from awa.toolkits.data import detect_encoding, sniff_delimiter
from awa.toolkits.documents import chunk_text, docx_text, pdf_text
from awa.toolkits.web import html_to_text
from awa.tools.risk import ToolRisk

Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]
"""Maps texts to embedding vectors, one per text (for example an embedding model's
`embed_documents`)."""

DEFAULT_PATTERNS = (
    "*.md",
    "*.markdown",
    "*.txt",
    "*.csv",
    "*.html",
    "*.htm",
    "*.json",
    "*.pdf",
    "*.docx",
)
"""File patterns `KnowledgeBase.load_directory` reads by default."""

_STOPWORDS = STOPWORDS | frozenset(
    (  # noqa: SIM905
        "der die das und ist ein eine zu den dem mit von im auf für nicht wie was wer "
        "le la les et est un une des du en"
    ).split()
)
_RRF_K = 60
_LANGUAGE_WORDS = {  # frequent function words; a corpus's language is the one most of them hit
    "English": "the and of to is in that for with are this be on it not you",
    "German": "der die das und ist nicht mit von zu den dem ein eine auf für sich wir sie",
    "French": "le la les et est une des du pour dans que qui pas sur nous vous",
    "Spanish": "el los las y es una del para con que por no se su al como",
    "Italian": "il di che è per una con non sono della gli le nel alla",
    "Dutch": "het een en van is dat op te zijn niet met voor wij u ook",
}
_LANGUAGE_INDEX = {
    word: language for language, words in _LANGUAGE_WORDS.items() for word in words.split()
}
_LANGUAGE_SHARE = 0.6  # below this share of the function words the corpus counts as mixed
_MAX_LISTED_SOURCES = 30
_EXTRA_MODULES = {".pdf": "pypdf", ".docx": "docx"}
_log = get_logger("toolkits.knowledge")


def tokenize(text: str) -> list[str]:
    """Lowercase word stems of `text`, stop words left out."""

    return terms(text, _STOPWORDS)


class SearchKnowledgeArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=1_000, description="What to look for.")
    k: int | None = Field(default=None, ge=1, le=50, description="Most passages returned.")
    source: str | None = Field(default=None, description="Only search this source (a file path).")


@dataclass(frozen=True)
class KnowledgeHit:
    """A chunk found by `KnowledgeBase.search`.

    `source` is the file path relative to the loaded folder (or the `source` given to `add_text`);
    `score` is comparable only within one search; `chunk` is the position within the source.
    """

    source: str
    text: str
    score: float
    chunk: int
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass
class _Chunk:
    source: str
    position: int
    text: str
    terms: Counter[str]
    length: int
    metadata: Mapping[str, Any]
    vector: list[float] | None = None


class KnowledgeBase:
    """Searchable chunks of text, ranked with BM25 (and optionally embeddings).

    Args:
        chunk_size: Characters per chunk.
        overlap: Characters repeated between neighboring chunks.
        embed: Optional embedding function; every chunk is embedded as it is added and searches
            combine keyword and semantic ranking.
        k1: BM25 term-frequency saturation.
        b: BM25 length normalization.

    Raises:
        ConfigurationError: If a parameter is out of range.

    Example:
        ```python
        kb = KnowledgeBase()
        kb.add_text("Refunds are possible within 30 days.", source="policy.md")
        [hit] = kb.search("refund period")
        ```
    """

    def __init__(
        self,
        *,
        chunk_size: int = 800,
        overlap: int = 80,
        embed: Embedder | None = None,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if chunk_size < 50 or not 0 <= overlap < chunk_size:
            raise ConfigurationError("chunk_size must be >= 50 and 0 <= overlap < chunk_size")
        if k1 < 0 or not 0 <= b <= 1:
            raise ConfigurationError("k1 must be >= 0 and b between 0 and 1")
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.embed = embed
        self.k1 = k1
        self.b = b
        self._chunks: list[_Chunk] = []
        self._frequencies: Counter[str] = Counter()
        self._languages: Counter[str] = Counter()
        self._first_lines: dict[str, str] = {}

    @classmethod
    def from_directory(
        cls,
        path: str | os.PathLike[str],
        *,
        patterns: Sequence[str] = DEFAULT_PATTERNS,
        recursive: bool = True,
        **options: Any,
    ) -> KnowledgeBase:
        """Create a knowledge base and load a folder into it; see `load_directory`."""

        knowledge = cls(**options)
        knowledge.load_directory(path, patterns=patterns, recursive=recursive)
        return knowledge

    def __len__(self) -> int:
        """Return the number of chunks."""

        return len(self._chunks)

    @property
    def language(self) -> str | None:
        """The language most of the indexed text is written in (English, German, French,
        Spanish, Italian or Dutch), detected while indexing; `None` when unclear or mixed."""

        total = sum(self._languages.values())
        if not total:
            return None
        language, hits = self._languages.most_common(1)[0]
        return language if hits / total >= _LANGUAGE_SHARE else None

    @property
    def sources(self) -> list[str]:
        """The distinct sources, in the order they were added."""

        return list(dict.fromkeys(chunk.source for chunk in self._chunks))

    def add_text(self, text: str, *, source: str, metadata: Mapping[str, Any] | None = None) -> int:
        """Chunk and index `text`; returns the number of chunks added."""

        pieces = chunk_text(text, chunk_size=self.chunk_size, overlap=self.overlap)
        vectors = list(self.embed(pieces)) if self.embed is not None and pieces else []
        if vectors and len(vectors) != len(pieces):
            raise ConfigurationError("the embed function returned a wrong number of vectors")
        frozen = MappingProxyType(dict(metadata or {}))
        self._languages.update(_LANGUAGE_INDEX[w] for w in words(text) if w in _LANGUAGE_INDEX)
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if pieces and source not in self._first_lines:
            self._first_lines[source] = first[:120] + ("…" if len(first) > 120 else "")
        for position, piece in enumerate(pieces):
            terms = Counter(tokenize(f"{source} {piece}" if position == 0 else piece))
            self._frequencies.update(terms.keys())
            self._chunks.append(
                _Chunk(
                    source=source,
                    position=position,
                    text=piece,
                    terms=terms,
                    length=sum(terms.values()),
                    metadata=frozen,
                    vector=[float(x) for x in vectors[position]] if vectors else None,
                )
            )
        return len(pieces)

    def add_file(
        self,
        path: str | os.PathLike[str],
        *,
        source: str | None = None,
        max_bytes: int = 20_000_000,
    ) -> int:
        """Read and index one file; returns the number of chunks added.

        Raises:
            ConfigurationError: If the file type is not supported or larger than `max_bytes`.
            OptionalDependencyError: For a PDF or Word file without the `files` extra.
        """

        file = Path(path)
        if file.stat().st_size > max_bytes:
            raise ConfigurationError(f"{file} is larger than {max_bytes} bytes")
        text = _read_document(file)
        return self.add_text(text, source=source or file.name, metadata={"path": str(file)})

    def load_directory(
        self,
        path: str | os.PathLike[str],
        *,
        patterns: Sequence[str] = DEFAULT_PATTERNS,
        recursive: bool = True,
        max_files: int = 10_000,
    ) -> int:
        """Index every matching file under `path` and return the number of files read.

        Hidden files are skipped, and PDF and Word files too (with a logged warning) while the
        `files` extra is not installed. Sources are the paths relative to `path`, with `/`.

        Raises:
            ConfigurationError: If `path` is not a directory or holds more than `max_files` files.
        """

        root = Path(path)
        if not root.is_dir():
            raise ConfigurationError(f"knowledge folder {str(path)!r} is not a directory")
        root = root.resolve()
        files: list[Path] = []
        for pattern in patterns:
            found = root.rglob(pattern) if recursive else root.glob(pattern)
            files.extend(found)
        chosen = sorted(
            {
                file
                for file in files
                if file.is_file()
                and not any(part.startswith(".") for part in file.relative_to(root).parts)
                and is_within(file, root)
            }
        )
        if len(chosen) > max_files:
            raise ConfigurationError(f"{path} holds more than {max_files} matching files")
        read = 0
        for file in chosen:
            module = _EXTRA_MODULES.get(file.suffix.lower())
            if module is not None and not extra_installed(module):
                _log.warning(
                    "Skipping %s: reading %s files needs the files extra", file.name, file.suffix
                )
                continue
            self.add_file(file, source=file.relative_to(root).as_posix())
            read += 1
        return read

    def search(
        self, query: str, *, k: int = 5, sources: Iterable[str] | None = None
    ) -> list[KnowledgeHit]:
        """Return the `k` chunks most relevant to `query`, optionally only from `sources`."""

        allowed = set(sources) if sources is not None else None
        candidates = [
            index
            for index, chunk in enumerate(self._chunks)
            if allowed is None or chunk.source in allowed
        ]
        if not candidates or k < 1:
            return []
        keyword = self._bm25(tokenize(query), candidates)
        ranked = sorted((i for i in candidates if keyword.get(i, 0) > 0), key=lambda i: -keyword[i])
        scores: dict[int, float] = {i: keyword[i] for i in ranked}
        if self.embed is not None:
            semantic = self._semantic(query, candidates)
            by_meaning = sorted(candidates, key=lambda i: -semantic.get(i, -1.0))[: max(k * 4, 20)]
            fused: dict[int, float] = {}
            for rank, index in enumerate(ranked[: max(k * 4, 20)]):
                fused[index] = fused.get(index, 0.0) + 1 / (_RRF_K + rank + 1)
            for rank, index in enumerate(by_meaning):
                fused[index] = fused.get(index, 0.0) + 1 / (_RRF_K + rank + 1)
            scores = fused
            ranked = sorted(fused, key=lambda i: -fused[i])
        return [
            KnowledgeHit(
                source=self._chunks[i].source,
                text=self._chunks[i].text,
                score=round(scores[i], 6),
                chunk=self._chunks[i].position,
                metadata=self._chunks[i].metadata,
            )
            for i in ranked[:k]
        ]

    def _bm25(self, terms: list[str], candidates: list[int]) -> dict[int, float]:
        if not terms:
            return {}
        count = len(self._chunks)
        average = sum(chunk.length for chunk in self._chunks) / count or 1.0
        scores: dict[int, float] = {}
        unique = set(terms)
        for index in candidates:
            chunk = self._chunks[index]
            score = 0.0
            for term in unique:
                frequency = chunk.terms.get(term, 0)
                if not frequency:
                    continue
                documents = self._frequencies[term]
                idf = math.log(1 + (count - documents + 0.5) / (documents + 0.5))
                norm = frequency + self.k1 * (1 - self.b + self.b * chunk.length / average)
                score += idf * frequency * (self.k1 + 1) / norm
            if score:
                scores[index] = score
        return scores

    def _semantic(self, query: str, candidates: list[int]) -> dict[int, float]:
        assert self.embed is not None
        [vector] = list(self.embed([query]))
        query_vector = [float(x) for x in vector]
        return {
            index: _cosine(query_vector, self._chunks[index].vector or []) for index in candidates
        }

    def _no_match(self, query: str, source: str | None) -> str:
        """What the search tool returns for zero hits: what the corpus holds and how to retry.

        The agent rewrites the query itself (the multi-query pattern, without an extra model
        call): it learns the corpus language and sees each document's title line.
        """

        sources = [source] if source in self.sources else self.sources
        listed = sources[:_MAX_LISTED_SOURCES]
        text = _no_match_text(query, self.language, self._first_lines, listed)
        if len(sources) > len(listed):
            text += f"\n… and {len(sources) - len(listed)} more sources."
        return text

    def as_tool(
        self,
        *,
        name: str = "search_knowledge",
        description: str | None = None,
        k: int = 5,
        max_output_chars: int = 20_000,
        frame_output: bool = True,
    ) -> BaseTool:
        """Return a tool that searches this knowledge base.

        `description` should say what the knowledge base holds; `k` is the default number of hits;
        `frame_output` labels the hits as external data.
        """

        positive(k, "k")

        def run(args: SearchKnowledgeArgs) -> str:
            hits = self.search(
                args.query, k=args.k or k, sources=[args.source] if args.source else None
            )
            if not hits:
                return self._no_match(args.query, args.source)
            return "\n\n".join(
                f"[{number}] {hit.source} (passage {hit.chunk + 1}, score {hit.score:.3f})\n"
                f"{hit.text}"
                for number, hit in enumerate(hits, start=1)
            )

        sources = self.sources
        listed = ", ".join(sources[:10]) + (
            f", … ({len(sources)} sources)" if len(sources) > 10 else ""
        )
        text = description or (
            "Search the knowledge base for passages relevant to a question and return them "
            "with their source. Use it before answering questions about internal policies, "
            "products or procedures, and cite the sources."
        )
        if sources:
            text += f" Sources include: {listed}."
        if self.language:
            text += f" The documents are in {self.language}; search with {self.language} terms."
        return build_tool(
            run,
            name=name,
            description=text,
            args_schema=SearchKnowledgeArgs,
            toolkit="knowledge",
            risk=ToolRisk(untrusted_input=True, private_data=True, external_action=False),
            frame=frame_output,
            max_output_chars=max_output_chars,
            source="knowledge",
        )


def _no_match_text(
    query: str, language: str | None, first_lines: Mapping[str, str], sources: Sequence[str]
) -> str:
    lines = [f"No passages match {query!r}."]
    if language:
        lines.append(
            f"The documents are in {language}: search once more with {language} words for the "
            "same request (synonyms, the terms a document would use)."
        )
    else:
        lines.append("Search once more with other words for the same request.")
    lines.append(
        "If that finds nothing either, the documents do not cover it: say so and promise "
        "nothing they do not support. The documents (source: first line):"
    )
    lines.extend(f"- {source}: {first_lines.get(source, '')}" for source in sources)
    return "\n".join(lines)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else -1.0


def _read_document(file: Path) -> str:
    suffix = file.suffix.lower()
    if suffix == ".pdf":
        return "\n\n".join(pdf_text(file))
    if suffix == ".docx":
        return docx_text(file)
    encoding = detect_encoding(file)
    raw = file.read_text(
        encoding="utf-8-sig" if encoding == "utf-8" else encoding, errors="replace"
    )
    if suffix in (".md", ".markdown", ".txt", ".rst", ""):
        return raw
    if suffix == ".csv":
        reader = csv.reader(io.StringIO(raw), delimiter=sniff_delimiter(raw[:16_384]))
        header = next(reader, [])
        rows = [
            "; ".join(f"{h}: {v}" for h, v in zip(header, row, strict=False) if v) for row in reader
        ]
        return "\n\n".join(row for row in rows if row)
    if suffix in (".html", ".htm"):
        title, text, _ = html_to_text(raw)
        return f"{title}\n\n{text}" if title else text
    if suffix == ".json":
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=1)
        except ValueError:
            return raw
    raise ConfigurationError(f"cannot read {file.name}: unsupported file type {suffix!r}")


def knowledge_toolkit(
    path: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
    *,
    patterns: Sequence[str] = DEFAULT_PATTERNS,
    recursive: bool = True,
    chunk_size: int = 800,
    overlap: int = 80,
    k: int = 5,
    name: str = "search_knowledge",
    description: str | None = None,
    embed: Embedder | None = None,
    max_output_chars: int = 20_000,
) -> list[BaseTool]:
    """Load one or more folders into a `KnowledgeBase` and return its search tool.

    PDF and Word files are skipped (with a warning) while the `files` extra is missing.

    Args:
        path: The folder, or several folders.
        patterns: File name patterns to load (`DEFAULT_PATTERNS`).
        chunk_size: Characters per chunk.
        overlap: Characters repeated between neighboring chunks.
        k: Results one search returns.
        description: Tool description; say what the documents are.
        embed: Optional embedding function for semantic ranking.

    Raises:
        ConfigurationError: If a folder does not exist.

    Example:
        ```python
        [search] = knowledge_toolkit("./handbook", description="Search the staff handbook.")
        ```
    """

    knowledge = KnowledgeBase(chunk_size=chunk_size, overlap=overlap, embed=embed)
    folders = [path] if isinstance(path, str | os.PathLike) else list(path)
    for folder in folders:
        knowledge.load_directory(folder, patterns=patterns, recursive=recursive)
    return [
        knowledge.as_tool(
            name=name, description=description, k=k, max_output_chars=max_output_chars
        )
    ]


__all__ = [
    "DEFAULT_PATTERNS",
    "Embedder",
    "KnowledgeBase",
    "KnowledgeHit",
    "knowledge_toolkit",
    "tokenize",
]
