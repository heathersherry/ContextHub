from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from contexthub.db.repository import ScopedRepo

from .result import MAX_SNIPPET_CHARS, LongDocRetrievalResult
from .ranges import document_ranges, enclosing_section

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "what",
    "when", "where", "why", "with",
}


def _tokenize(text: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.findall(r"[^\W_]+", text.lower()):
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


class KeywordRetriever:
    def __init__(
        self,
        *,
        rg_path: str = "rg",
        max_snippet_chars: int = MAX_SNIPPET_CHARS,
    ):
        self._rg = rg_path
        self._max_snippet_chars = max_snippet_chars

    async def retrieve(
        self,
        db: ScopedRepo,
        query: str,
        doc_contexts: list[dict],
    ) -> list[LongDocRetrievalResult]:
        if not doc_contexts:
            return []

        keyword_groups = self._keyword_groups(query)
        if not keyword_groups:
            return []

        results: list[LongDocRetrievalResult] = []
        for doc in doc_contexts:
            results.extend(await self._retrieve_for_document(db, doc, keyword_groups))
        results.sort(key=lambda item: item.relevance_score, reverse=True)
        return results

    @staticmethod
    def _keyword_groups(query: str) -> list[list[str]]:
        # One literal OR pattern, all query terms, no generation call. Preserve
        # numbers and Unicode terms; do not silently discard late query terms.
        tokens = _tokenize(query)
        return [tokens] if tokens else []

    def _rg_available(self) -> bool:
        return shutil.which(self._rg) is not None or Path(self._rg).exists()

    async def _retrieve_for_document(
        self,
        db: ScopedRepo,
        doc: dict,
        keyword_groups: list[list[str]],
    ) -> list[LongDocRetrievalResult]:
        file_path = doc.get("file_path")
        if not file_path:
            return []
        extracted_path = Path(file_path) / "extracted.txt"

        try:
            text = extracted_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Keyword retriever skipped unreadable extracted.txt for %s", doc.get("uri"))
            return []

        hit_positions = await self._collect_hit_positions(extracted_path, text, keyword_groups)
        if not hit_positions:
            return []

        sections = [dict(row) for row in await db.fetch(
            """SELECT section_id, parent_id, depth, start_offset, end_offset
                 FROM document_sections WHERE context_id = $1
                ORDER BY start_offset, depth, section_id""",
            doc["id"],
        )]
        windows = self._evidence_windows(text, hit_positions, sections)
        if not windows:
            return []

        results = []
        for snippet, offset in windows:
            section = enclosing_section(sections, *offset)
            results.append(LongDocRetrievalResult(
                context_id=doc["id"], uri=doc["uri"], strategy="keyword",
                section_id=section["section_id"] if section else None,
                snippet=snippet, snippet_offset=offset,
                relevance_score=self._score_window(snippet, keyword_groups)))
        return results

    async def _collect_hit_positions(
        self,
        extracted_path: Path,
        text: str,
        keyword_groups: list[list[str]],
    ) -> list[int]:
        positions: set[int] = set()
        rg_available = self._rg_available()
        line_offsets = self._line_offsets(text)
        line_byte_offsets = self._line_byte_offsets(text)
        byte_to_char = self._byte_to_char_offsets(text)
        for group in keyword_groups:
            if rg_available:
                pattern = "|".join(re.escape(token) for token in group)
                if not pattern:
                    continue
                group_positions = await self._run_rg(
                    extracted_path,
                    pattern,
                    line_offsets,
                    line_byte_offsets,
                    byte_to_char,
                    text,
                    group,
                )
            else:
                group_positions = self._scan_text_positions(text, group)
            positions.update(group_positions)
        return sorted(positions)

    async def _run_rg(
        self,
        extracted_path: Path,
        pattern: str,
        line_offsets: list[int],
        line_byte_offsets: list[int],
        byte_to_char: dict[int, int],
        text: str,
        group: list[str],
    ) -> list[int]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._rg,
                "--json",
                "-n",
                "-i",
                pattern,
                str(extracted_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.warning("Keyword retriever falling back to Python scanning")
            return self._scan_text_positions(text, group)

        stdout, _stderr = await proc.communicate()
        if proc.returncode == 1:
            return []
        if proc.returncode not in (0, 1):
            logger.warning("Keyword retriever rg failed for %s with exit code %s", extracted_path, proc.returncode)
            return []

        positions: list[int] = []
        for raw_line in stdout.decode("utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            line_number = data.get("line_number")
            absolute_offset = data.get("absolute_offset")
            line_text = ((data.get("lines") or {}).get("text")) or ""
            submatches = data.get("submatches") or []
            if isinstance(line_number, int) and 1 <= line_number <= len(line_offsets):
                base_offset = line_offsets[line_number - 1]
                base_byte_offset = line_byte_offsets[line_number - 1]
            elif isinstance(absolute_offset, int):
                base_offset = byte_to_char.get(absolute_offset, 0)
                base_byte_offset = absolute_offset
            else:
                base_offset = 0
                base_byte_offset = 0

            if submatches:
                for submatch in submatches:
                    start = submatch.get("start")
                    if isinstance(start, int):
                        char_pos = byte_to_char.get(base_byte_offset + start)
                        if char_pos is not None:
                            positions.append(char_pos)
                continue

            lowered = line_text.lower()
            for token in group:
                cursor = lowered.find(token.lower())
                while cursor >= 0:
                    positions.append(base_offset + cursor)
                    cursor = lowered.find(token.lower(), cursor + 1)

        bounded_positions = [pos for pos in positions if 0 <= pos < len(text)]
        return bounded_positions

    def _scan_text_positions(self, text: str, group: list[str]) -> list[int]:
        positions: list[int] = []
        for token in group:
            positions.extend(match.start() for match in re.finditer(re.escape(token), text, re.IGNORECASE))
        return positions

    def _line_offsets(self, text: str) -> list[int]:
        offsets: list[int] = []
        cursor = 0
        for line in text.splitlines(keepends=True):
            offsets.append(cursor)
            cursor += len(line)
        if not offsets:
            offsets.append(0)
        return offsets

    def _line_byte_offsets(self, text: str) -> list[int]:
        offsets: list[int] = []
        cursor = 0
        for line in text.splitlines(keepends=True):
            offsets.append(cursor)
            cursor += len(line.encode("utf-8"))
        if not offsets:
            offsets.append(0)
        return offsets

    def _byte_to_char_offsets(self, text: str) -> dict[int, int]:
        offsets = {0: 0}
        cursor = 0
        for index, char in enumerate(text, start=1):
            cursor += len(char.encode("utf-8"))
            offsets[cursor] = index
        return offsets

    def _evidence_windows(self, text, hit_positions, sections):
        return [(text[start:end], (start, end))
                for start, end in document_ranges(text, sections, self._max_snippet_chars)
                if any(start <= hit < end for hit in hit_positions)]

    @staticmethod
    def _score_window(snippet: str, keyword_groups: list[list[str]]) -> float:
        all_keywords = {token for group in keyword_groups for token in group}
        lowered = snippet.lower()
        coverage = sum(1 for token in all_keywords if token in lowered)
        density = sum(lowered.count(token) for token in all_keywords) / max(1, len(snippet))
        return coverage + density / (1 + density)
