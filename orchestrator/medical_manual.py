"""Medical Manual Q&A retrieval for the Ember Orchestrator
(Nov 27, 2026).

Rom: "Build another use case for MAC to use this medical
manual to demonstrate the long-context reference material.
The idea is for users to ask any medical questions to MAC and
it answers any medical questions based on the manual as a
RAG/KAG."

The manual is The Merck Manual of Diagnosis & Therapy, 19th
Edition (4,114 pages), chunked by chapter/section with
printed-page citations (see medical_manual/chunks.jsonl,
built by build_corpus.py). Retrieval is DETERMINISTIC keyword
BM25 over the chunk corpus — no embeddings model, no Axion,
no knowledge_search: Ember is the only AI in the path, and it
answers ONLY from the passages this module returns, citing
chapter/section/pages. Passages are grouped by chapter
(relevance order, at most MAX_CHAPTERS chapters, anchors
taken round-robin so no chapter crowds out the others) and
the result carries a per-chapter summary list — the answer
is composed as one summary block per relevant chapter, in
medical-practitioner-assistant style. When the manual does
not cover a question, search() returns covered=False with
no passages and Ember must say so rather than answer from
general knowledge.

This is a demo of long-context reference: each answer carries
tens of thousands of tokens of retrieved manual text. It is
NOT medical advice and never produces personalized diagnosis
or treatment recommendations — the tool answers "what does
the manual say about X".
"""
from __future__ import annotations

import json
import math
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MANUAL_TITLE = "The Merck Manual of Diagnosis & Therapy, 19th Edition"

# Long-context reference budget per answer: ~160K chars ≈ 40K
# tokens of retrieved manual text (anchors + neighbours).
REFERENCE_CHAR_BUDGET = 160_000
MAX_ANCHORS = 24
NEIGHBOUR_RADIUS = 2
# Answers are organized per chapter (medical practitioner
# assistant, Nov 27 2026): at most this many chapters are
# summarized, and a chapter only counts as relevant when its
# anchor score reaches this share of the top chapter's.
MAX_CHAPTERS = 5
CHAPTER_MIN_SCORE_RATIO = 0.10

_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "and", "or", "is", "are", "was",
    "were", "be", "been", "being", "what", "which", "who",
    "whom", "whose", "when", "where", "why", "how", "do",
    "does", "did", "can", "could", "should", "would", "may",
    "might", "must", "shall", "will", "it", "its", "this",
    "that", "these", "those", "i", "you", "he", "she", "we",
    "they", "my", "your", "his", "her", "our", "their", "me",
    "him", "them", "us", "if", "then", "than", "so", "such",
    "as", "but", "not", "no", "yes", "any", "some", "all",
    "each", "per", "about", "into", "over", "under", "between",
    "during", "after", "before", "because", "there", "here",
    "say", "says", "tell", "please", "according",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _stem(tok: str) -> str:
    """Tiny deterministic suffix stripper (plurals only) —
    applied identically to index and query tokens."""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith("es"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(str(text or "").lower())
            if len(t) >= 2 and t not in _STOPWORDS]


def manual_dir() -> str:
    return os.environ.get(
        "EMBER_MEDICAL_MANUAL_DIR",
        os.path.join(ROOT, "medical_manual"))


class _Index:
    """In-memory BM25 over the chunk corpus (built once)."""

    def __init__(self, directory: str):
        self.directory = directory
        self.chunks: list[dict] = []
        self.meta: dict = {}
        self.postings: dict[str, dict[int, int]] = {}
        self.doc_len: list[int] = []
        self.title_terms: list[set] = []
        self.section_terms: list[set] = []
        self.avgdl = 0.0
        self._load()

    def _load(self):
        meta_path = os.path.join(self.directory, "corpus_meta.json")
        chunks_path = os.path.join(self.directory, "chunks.jsonl")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                self.meta = json.load(f)
        if not os.path.exists(chunks_path):
            return
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.chunks.append(json.loads(line))
        total = 0
        for i, c in enumerate(self.chunks):
            toks = _tokens(c.get("text", ""))
            self.doc_len.append(len(toks))
            total += len(toks)
            self.title_terms.append(set(_tokens(c.get("chapter", ""))))
            self.section_terms.append(set(_tokens(c.get("section", ""))))
            for t in toks:
                self.postings.setdefault(t, {})
                self.postings[t][i] = self.postings[t].get(i, 0) + 1
        self.avgdl = total / len(self.chunks) if self.chunks else 0.0

    @property
    def ready(self) -> bool:
        return bool(self.chunks)

    def _idf(self, term: str) -> float:
        df = len(self.postings.get(term, {}))
        n = len(self.chunks)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, question: str,
               char_budget: int = REFERENCE_CHAR_BUDGET,
               max_anchors: int = MAX_ANCHORS) -> dict:
        q_terms = list(dict.fromkeys(_tokens(question)))
        base = {
            "manual": self.meta.get("title", MANUAL_TITLE),
            "question": str(question or "")[:300],
            "covered": False,
            "passages": [],
            "citations": [],
            "chapters": [],
            "reference_chars": 0,
            "reference_tokens_approx": 0,
            "corpus_chunks": len(self.chunks),
        }
        if not self.ready or not q_terms:
            return base
        k1, b = 1.5, 0.75
        scores: dict[int, float] = {}
        matched: dict[int, set] = {}
        for term in q_terms:
            idf = self._idf(term)
            for i, tf in self.postings.get(term, {}).items():
                dl = self.doc_len[i] or 1
                denom = tf + k1 * (1 - b + b * dl / (self.avgdl or 1))
                scores[i] = scores.get(i, 0.0) + \
                    idf * (tf * (k1 + 1)) / denom
                matched.setdefault(i, set()).add(term)
            # Title/section matches count even when the body
            # token is absent (chapter named for the topic).
            for i in range(len(self.chunks)):
                if term in self.title_terms[i]:
                    scores[i] = scores.get(i, 0.0) + 2.0 * idf
                    matched.setdefault(i, set()).add(term)
                elif term in self.section_terms[i]:
                    scores[i] = scores.get(i, 0.0) + 1.5 * idf
                    matched.setdefault(i, set()).add(term)
        if not scores:
            return base
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        # Coverage honesty: the best chunk must match at least
        # two distinct query terms (body or titles), or one
        # term in a chapter/section title. Otherwise the manual
        # likely does not cover the question.
        top_i, _top_s = ranked[0]
        top_terms = matched.get(top_i, set())
        title_hit = any(t in self.title_terms[top_i]
                        or t in self.section_terms[top_i]
                        for t in top_terms)
        if len(top_terms) < 2 and not title_hit:
            return base
        # Out-of-vocabulary honesty: a query term that never
        # appears in the whole manual (df=0) usually carries
        # the topic ("sedan" in "tire pressure for a sedan") —
        # generic matches on the other words ("correct",
        # "pressure") must not claim coverage. A strong match
        # (>=3 terms in the best chunk) still counts, so a
        # stray unknown word in an otherwise covered question
        # does not sink it.
        oov = sum(1 for t in q_terms
                  if not self.postings.get(t)
                  and not any(t in tt for tt in self.title_terms)
                  and not any(t in st
                              for st in self.section_terms))
        if oov >= 1 and len(top_terms) < 3:
            return base
        # Anchors + neighbours (same chapter only), deduped,
        # within the character budget. Anchors must match at
        # least two distinct query terms (or a title/section
        # term) — single-term matches are noise.
        def _anchor_ok(i: int) -> bool:
            terms = matched.get(i, set())
            if len(terms) >= 2:
                return True
            return any(t in self.title_terms[i]
                       or t in self.section_terms[i] for t in terms)

        anchors = [i for i, _s in ranked if _anchor_ok(i)]
        # Chapter relevance: sum of each chapter's top anchor
        # scores. The answer is organized per chapter (medical
        # practitioner assistant), so selection must not let
        # one dominant chapter crowd out the others: pick the
        # top few chapters, then take anchors round-robin
        # across them so every selected chapter contributes
        # its best passages before any chapter fills the
        # budget.
        by_chapter: dict[str, list[int]] = {}
        for i in anchors:
            by_chapter.setdefault(
                str(self.chunks[i].get("chapter", "")), []).append(i)
        chapter_score = {
            ch: sum(scores[i] for i in idxs[:3])
            for ch, idxs in by_chapter.items()}
        ranked_chapters = sorted(
            chapter_score.items(), key=lambda kv: -kv[1])
        top_score = ranked_chapters[0][1] if ranked_chapters else 0.0
        selected = [ch for ch, s in ranked_chapters[:MAX_CHAPTERS]
                    if s >= CHAPTER_MIN_SCORE_RATIO * top_score]
        if not selected and ranked_chapters:
            selected = [ranked_chapters[0][0]]
        chapter_rank = {ch: r for r, ch in enumerate(selected)}
        chosen: list[int] = []
        seen: set[int] = set()
        total = 0
        used_anchors = 0
        depth = 0
        while used_anchors < max_anchors:
            progressed = False
            for ch in selected:
                if used_anchors >= max_anchors:
                    break
                idxs = by_chapter[ch]
                if depth >= len(idxs):
                    continue
                progressed = True
                i = idxs[depth]
                used_anchors += 1
                for j in range(i - NEIGHBOUR_RADIUS,
                               i + NEIGHBOUR_RADIUS + 1):
                    if j in seen or not (0 <= j < len(self.chunks)):
                        continue
                    if self.chunks[j].get("chapter") != \
                            self.chunks[i].get("chapter"):
                        continue
                    size = len(self.chunks[j].get("text", ""))
                    if total + size > char_budget and chosen:
                        continue
                    seen.add(j)
                    chosen.append(j)
                    total += size
            if not progressed:
                break
            depth += 1
        chosen.sort(key=lambda j: (
            chapter_rank.get(str(self.chunks[j].get("chapter", "")),
                             len(selected)),
            self.chunks[j].get("page_start") or 0, j))
        passages = []
        citations = []
        for j in chosen:
            c = self.chunks[j]
            pages = _page_label(c)
            section = _clean_section(c.get("section", ""))
            passages.append({
                "chapter": c.get("chapter", ""),
                "section": section,
                "pages": pages,
                "text": c.get("text", ""),
            })
            cite = c.get("chapter", "")
            if section:
                cite += f" — {section}"
            if pages:
                cite += f", {pages}"
            if cite and cite not in citations:
                citations.append(cite)
        # Per-chapter summary for the compose step: the
        # relevant chapters in relevance order, with passage
        # counts and page spans, so Ember can write one
        # summary block per chapter.
        chapters_out = []
        for ch in selected:
            mine = [j for j in chosen
                    if str(self.chunks[j].get("chapter", "")) == ch]
            if not mine:
                continue
            starts = [self.chunks[j].get("page_start") for j in mine
                      if self.chunks[j].get("page_start") is not None]
            ends = [self.chunks[j].get("page_end") for j in mine
                    if self.chunks[j].get("page_end") is not None]
            label = ""
            if starts:
                lo, hi = min(starts), max(ends or starts)
                label = f"p. {lo}" if lo == hi else f"pp. {lo}–{hi}"
            sections = []
            for j in mine:
                s = _clean_section(self.chunks[j].get("section", ""))
                if s and s not in sections:
                    sections.append(s)
            chapters_out.append({
                "chapter": ch,
                "chapter_num": self.chunks[mine[0]].get("chapter_num"),
                "passages": len(mine),
                "pages": label,
                "sections": sections,
            })
        base.update({
            "covered": True,
            "passages": passages,
            "citations": citations,
            "chapters": chapters_out,
            "reference_chars": total,
            "reference_tokens_approx": total // 4,
        })
        return base


_SECTION_TAIL = (" and", " or", " the", " of", " to", " in", " a")


def _clean_section(section: str) -> str:
    """Trim heading fragments the chunker occasionally keeps
    (e.g. 'Tables 4-1 and') so citations read cleanly."""
    s = str(section or "").strip()
    changed = True
    while changed:
        changed = False
        for tail in _SECTION_TAIL:
            if s.lower().endswith(tail) and len(s) > len(tail) + 2:
                s = s[: -len(tail)].rstrip(" ,")
                changed = True
    return s


def _page_label(chunk: dict) -> str:
    a, b = chunk.get("page_start"), chunk.get("page_end")
    if a is None:
        return ""
    return f"p. {a}" if a == b else f"pp. {a}–{b}"


_INDEX: _Index | None = None
_INDEX_KEY: tuple | None = None


def get_index(directory: str | None = None) -> _Index:
    """Cached index; rebuilt when the corpus file changes."""
    global _INDEX, _INDEX_KEY
    directory = directory or manual_dir()
    chunks_path = os.path.join(directory, "chunks.jsonl")
    try:
        key = (directory, os.path.getmtime(chunks_path),
               os.path.getsize(chunks_path))
    except OSError:
        key = (directory, None, None)
    if _INDEX is None or _INDEX_KEY != key:
        _INDEX = _Index(directory)
        _INDEX_KEY = key
    return _INDEX


def corpus_stats(directory: str | None = None) -> dict:
    idx = get_index(directory)
    return {
        "ready": idx.ready,
        "title": idx.meta.get("title", MANUAL_TITLE),
        "chunks": len(idx.chunks),
        "chars": idx.meta.get("chars", 0),
        "approx_tokens": idx.meta.get("approx_tokens", 0),
        "chapters": len(idx.meta.get("chapters", [])),
    }


def search_manual(question: str,
                  directory: str | None = None) -> dict:
    return get_index(directory).search(question)
