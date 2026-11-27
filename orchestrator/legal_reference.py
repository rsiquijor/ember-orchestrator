"""Paralegal Assistant retrieval for the Ember Orchestrator
(Nov 27, 2026).

Rom: "Build it with all sources" — a Paralegal Assistant for
MAC mirroring the Medical Manual Q&A pattern, over US
public-domain legal sources: the United States Code (OLRC
USLM XML, release point Public Law 119-142), the Federal
Rules of Civil/Criminal/Appellate/Bankruptcy Procedure and
Evidence (uscourts.gov PDFs), the US Constitution
(archives.gov transcript), and the Code of Federal
Regulations (govinfo bulk XML, 2025 annual edition).

Retrieval is DETERMINISTIC keyword BM25 (SQLite FTS5,
porter stemming) over the chunk corpus in legal_corpus/
(built by build_corpus.py) — no embeddings model, no Axion,
no knowledge_search: Ember is the only AI in the path, and
it answers ONLY from the passages this module returns,
citing per section/rule (e.g. "28 U.S.C. § 1331",
"Fed. R. Civ. P. 26", "U.S. Const. amend. XIV, § 1",
"29 C.F.R. § 1910.95"). Passages are grouped by source/title
(relevance order, at most MAX_GROUPS groups, anchors taken
round-robin so no title crowds out the others) and the
result carries a per-group summary list — the answer is
composed as one summary block per relevant source/title,
in paralegal-assistant style. When the sources do not
cover a question, search() returns covered=False with no
passages and Ember must say so rather than answer from
general knowledge.

This is a demo of long-context reference: each answer
carries tens of thousands of tokens of retrieved legal
text. It is legal INFORMATION only — never legal advice
for a specific case, and never a substitute for a licensed
attorney (serve.py appends the disclaimer deterministically).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORPUS_TITLE = ("US public-domain legal sources (U.S. Code, "
                "Federal Rules, Constitution, CFR)")

# Long-context reference budget per answer: ~160K chars ≈ 40K
# tokens of retrieved legal text (anchors + neighbours).
REFERENCE_CHAR_BUDGET = 160_000
MAX_ANCHORS = 24
NEIGHBOUR_RADIUS = 2
MAX_CANDIDATES = 300
# Answers are organized per source/title (paralegal
# assistant): at most this many groups are summarized, and a
# group only counts as relevant when its anchor score
# reaches this share of the top group's.
MAX_GROUPS = 5
GROUP_MIN_SCORE_RATIO = 0.10

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
    used for the coverage-honesty checks (FTS5 porter does
    the ranking-side stemming)."""
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


def corpus_dir() -> str:
    return os.environ.get(
        "EMBER_LEGAL_CORPUS_DIR",
        os.path.join(ROOT, "legal_corpus"))


class _Index:
    """FTS5 BM25 over the legal chunk corpus.

    Production: legal.db built by build_corpus.py. Fixtures
    (tests): an in-memory FTS5 built from chunks.jsonl."""

    def __init__(self, directory: str):
        self.directory = directory
        self.meta: dict = {}
        self.con: sqlite3.Connection | None = None
        self.n_chunks = 0
        self._load()

    def _load(self):
        meta_path = os.path.join(self.directory, "corpus_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                self.meta = json.load(f)
        db_path = os.path.join(self.directory, "legal.db")
        if os.path.exists(db_path):
            self.con = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True,
                check_same_thread=False)
        else:
            chunks_path = os.path.join(self.directory,
                                       "chunks.jsonl")
            if not os.path.exists(chunks_path):
                return
            self.con = sqlite3.connect(":memory:",
                                       check_same_thread=False)
            self.con.execute(
                "CREATE VIRTUAL TABLE legal_fts USING fts5("
                "citation, heading, group_name, "
                "source UNINDEXED, text, "
                "tokenize='porter unicode61')")
            rows = []
            with open(chunks_path, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    c = json.loads(line)
                    rows.append((i + 1, c.get("citation", ""),
                                 c.get("heading", ""),
                                 c.get("group", ""),
                                 c.get("source", ""),
                                 c.get("text", "")))
            self.con.executemany(
                "INSERT INTO legal_fts(rowid, citation, heading,"
                " group_name, source, text) VALUES (?,?,?,?,?,?)",
                rows)
            self.con.commit()
        try:
            self.n_chunks = self.con.execute(
                "SELECT count(*) FROM legal_fts").fetchone()[0]
        except sqlite3.Error:
            self.n_chunks = 0

    @property
    def ready(self) -> bool:
        return self.con is not None and self.n_chunks > 0

    def _term_df(self, term: str) -> int:
        """Document frequency of one (unstemmed) query term —
        FTS5 porter stems it the same way as the index."""
        try:
            row = self.con.execute(
                "SELECT count(*) FROM legal_fts "
                "WHERE legal_fts MATCH ?", (f'"{term}"',)).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def _fetch(self, rowid: int) -> dict | None:
        row = self.con.execute(
            "SELECT citation, heading, group_name, source, text "
            "FROM legal_fts WHERE rowid = ?", (rowid,)).fetchone()
        if not row:
            return None
        return {"rowid": rowid, "citation": row[0],
                "heading": row[1], "group": row[2],
                "source": row[3], "text": row[4]}

    def search(self, question: str,
               char_budget: int = REFERENCE_CHAR_BUDGET,
               max_anchors: int = MAX_ANCHORS) -> dict:
        q_raw = [t for t in _TOKEN_RE.findall(
            str(question or "").lower())
            if len(t) >= 2 and t not in _STOPWORDS]
        q_terms = list(dict.fromkeys(_stem(t) for t in q_raw))
        base = {
            "corpus": self.meta.get("title", CORPUS_TITLE),
            "question": str(question or "")[:300],
            "covered": False,
            "passages": [],
            "citations": [],
            "groups": [],
            "reference_chars": 0,
            "reference_tokens_approx": 0,
            "corpus_chunks": self.n_chunks,
        }
        if not self.ready or not q_terms:
            return base
        match = " OR ".join(f'"{t}"' for t in
                            dict.fromkeys(q_raw))
        try:
            rows = self.con.execute(
                "SELECT rowid, citation, heading, group_name, "
                "source, text, bm25(legal_fts, 3.0, 3.0, 1.5, "
                "1.0) AS score FROM legal_fts "
                "WHERE legal_fts MATCH ? ORDER BY score "
                "LIMIT ?", (match, MAX_CANDIDATES)).fetchall()
        except sqlite3.Error:
            return base
        if not rows:
            return base
        # FTS5 bm25() is negative (lower = better); relevance
        # is its negation.
        candidates = []
        for rowid, citation, heading, group, source, text, \
                score in rows:
            hay = set(_tokens(text))
            cite_head = set(_tokens(citation)) | \
                set(_tokens(heading))
            head_terms = cite_head | set(_tokens(group))
            matched = {t for t in q_terms if t in hay
                       or t in head_terms}
            candidates.append({
                "rowid": rowid, "citation": citation,
                "heading": heading, "group": group,
                "source": source, "text": text,
                "score": -float(score), "matched": matched,
                "head_terms": head_terms,
                "cite_head_terms": cite_head})
        # Coverage honesty (mirrors the medical manual): the
        # best chunk must match at least two distinct query
        # terms — three when the query has four or more
        # content terms (a state-law question like "speed
        # limit on highways in Texas" matches only two stray
        # words in federal text and must not claim coverage).
        # Short queries (<=2 terms) may instead cover on a
        # single citation/heading hit ("hearsay" -> FRE 802);
        # the exception deliberately does not use group-name
        # tokens and does not apply to longer queries (a
        # "Texas Hill Country" wine regulation heading must
        # not cover a Texas speed-limit question).
        top = candidates[0]
        title_hit = len(q_terms) <= 2 and any(
            t in top["cite_head_terms"] for t in top["matched"])
        min_matched = 3 if len(q_terms) >= 4 else 2
        if len(top["matched"]) < min_matched and not title_hit:
            return base
        # Out-of-vocabulary honesty: a query term that never
        # appears in the whole corpus usually carries the
        # topic — generic matches on the other words must not
        # claim coverage. A strong match (>=3 terms in the
        # best chunk) still counts.
        oov = sum(1 for t in dict.fromkeys(q_raw)
                  if self._term_df(t) == 0)
        if oov >= 1 and len(top["matched"]) < 3:
            return base

        def _anchor_ok(c: dict) -> bool:
            if len(c["matched"]) >= 2:
                return True
            return any(t in c["head_terms"] for t in c["matched"])

        anchors = [c for c in candidates if _anchor_ok(c)]
        if not anchors:
            return base
        # Group relevance: sum of each group's top anchor
        # scores. Selection must not let one dominant title
        # crowd out the others: pick the top few groups, then
        # take anchors round-robin across them.
        by_group: dict[str, list[dict]] = {}
        for c in anchors:
            by_group.setdefault(c["group"], []).append(c)
        # Group ranking is led by the single best anchor (a
        # short, exactly-on-point provision like Amendment IV
        # must not lose to a big title with three mediocre
        # matches), with a small depth bonus for the next two
        # anchors. Heading/citation hits need no extra bonus
        # here: the FTS5 bm25 weights already score citation
        # and heading columns at 3.0.
        def _group_score(cs: list[dict]) -> float:
            top3 = cs[:3]
            return top3[0]["score"] + 0.15 * sum(
                c["score"] for c in top3[1:])

        group_score = {g: _group_score(cs)
                       for g, cs in by_group.items()}
        ranked_groups = sorted(group_score.items(),
                               key=lambda kv: -kv[1])
        top_score = ranked_groups[0][1] if ranked_groups else 0.0
        selected = [g for g, s in ranked_groups[:MAX_GROUPS]
                    if s >= GROUP_MIN_SCORE_RATIO * top_score]
        if not selected and ranked_groups:
            selected = [ranked_groups[0][0]]
        group_rank = {g: r for r, g in enumerate(selected)}
        chosen: list[dict] = []
        seen: set[int] = set()
        total = 0
        used_anchors = 0
        depth = 0
        while used_anchors < max_anchors:
            progressed = False
            for g in selected:
                if used_anchors >= max_anchors:
                    break
                cs = by_group[g]
                if depth >= len(cs):
                    continue
                progressed = True
                anchor = cs[depth]
                used_anchors += 1
                for rid in range(anchor["rowid"] - NEIGHBOUR_RADIUS,
                                 anchor["rowid"] + NEIGHBOUR_RADIUS
                                 + 1):
                    if rid in seen or rid < 1:
                        continue
                    chunk = anchor if rid == anchor["rowid"] \
                        else self._fetch(rid)
                    if chunk is None or chunk["group"] != g:
                        continue
                    size = len(chunk["text"])
                    if total + size > char_budget and chosen:
                        continue
                    seen.add(rid)
                    chosen.append(chunk)
                    total += size
            if not progressed:
                break
            depth += 1
        chosen.sort(key=lambda c: (
            group_rank.get(c["group"], len(selected)),
            c["rowid"]))
        passages = []
        citations = []
        for c in chosen:
            passages.append({
                "source": c["source"],
                "group": c["group"],
                "citation": c["citation"],
                "heading": c["heading"],
                "text": c["text"],
            })
            if c["citation"] and c["citation"] not in citations:
                citations.append(c["citation"])
        groups_out = []
        for g in selected:
            mine = [c for c in chosen if c["group"] == g]
            if not mine:
                continue
            cites = []
            for c in mine:
                if c["citation"] and c["citation"] not in cites:
                    cites.append(c["citation"])
            groups_out.append({
                "group": g,
                "source": mine[0]["source"],
                "passages": len(mine),
                "citations": cites,
            })
        base.update({
            "covered": True,
            "passages": passages,
            "citations": citations,
            "groups": groups_out,
            "reference_chars": total,
            "reference_tokens_approx": total // 4,
        })
        return base


_INDEX: _Index | None = None
_INDEX_KEY: tuple | None = None


def get_index(directory: str | None = None) -> _Index:
    """Cached index; rebuilt when the corpus file changes."""
    global _INDEX, _INDEX_KEY
    directory = directory or corpus_dir()
    key = (directory,)
    for fname in ("legal.db", "chunks.jsonl"):
        path = os.path.join(directory, fname)
        try:
            key += (fname, os.path.getmtime(path),
                    os.path.getsize(path))
        except OSError:
            key += (fname, None, None)
    if _INDEX is None or _INDEX_KEY != key:
        _INDEX = _Index(directory)
        _INDEX_KEY = key
    return _INDEX


def corpus_stats(directory: str | None = None) -> dict:
    idx = get_index(directory)
    return {
        "ready": idx.ready,
        "title": idx.meta.get("title", CORPUS_TITLE),
        "chunks": idx.n_chunks,
        "chars": idx.meta.get("chars", 0),
        "approx_tokens": idx.meta.get("approx_tokens", 0),
        "groups": len(idx.meta.get("groups", [])),
        "per_source": idx.meta.get("per_source", {}),
    }


def search_legal(question: str,
                 directory: str | None = None) -> dict:
    return get_index(directory).search(question)
