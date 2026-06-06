#!/usr/bin/env python3
"""
prose_to_anchormap.py — SINGLE-FILE word-based, per-paragraph ANCHOR map.

Self-contained: extraction (text/HTML/PDF/URL) + lexicon resolver + encoder +
geometric coupling-map generator, emitting the exact `wpe-5.0-xmap-1.0` schema.

    python prose_to_xmap.py SOURCE [SOURCE ...] \
        --lex scimed_dict_lib.json word_dict_75k_lib.json \
        [--split whole|sections|paragraphs] [--min-chars 800] \
        [--coupling geometric|semantic] [--out map.json]

Each SOURCE (file or URL) becomes one PART, or use --split to cut one document
into several coupled parts. Coupling is geometric by default:
    delta_theta = |theta_i - theta_j| folded to [0,180];  c = cos(delta_theta).

Optional deps (lazy): nltk (lemmas), beautifulsoup4/trafilatura (HTML),
pypdf (PDF), requests (URLs). Plain-text works with none of them.
Run once:  python -c "import nltk; nltk.download('wordnet'); nltk.download('omw-1.4')"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

# ===========================================================================
# 0. Lexicon domain table (the 19 codes, their shells and descriptions)
# ===========================================================================
DOMAINS = {
    "PRON": {"shell": 1, "description": "pronouns"},
    "DET": {"shell": 2, "description": "determiners / articles"},
    "NUM": {"shell": 3, "description": "numerals (cardinal & ordinal)"},
    "PREP": {"shell": 4, "description": "prepositions"},
    "CONJ": {"shell": 5, "description": "conjunctions"},
    "AUX": {"shell": 6, "description": "auxiliary & modal verbs"},
    "NOUNC": {"shell": 7, "description": "concrete / count nouns"},
    "NOUNM": {"shell": 8, "description": "mass / substance nouns"},
    "NOUNA": {"shell": 9, "description": "abstract nouns"},
    "NOUNP": {"shell": 10, "description": "proper nouns"},
    "VERBA": {"shell": 11, "description": "action / dynamic verbs"},
    "VERBS": {"shell": 12, "description": "stative / cognitive verbs"},
    "ADJ": {"shell": 13, "description": "adjectives"},
    "ADV": {"shell": 14, "description": "adverbs"},
    "INTERJ": {"shell": 15, "description": "interjections"},
    "SLANG": {"shell": 16, "description": "slang"},
    "JARGON": {"shell": 17, "description": "domain jargon"},
    "META": {"shell": 18, "description": "meta / structural tokens"},
    "PRIME": {"shell": 19, "description": "primitive seed concepts"},
}
FUNCTION_DOMS = {"PRON", "DET", "PREP", "CONJ", "AUX", "NUM", "INTERJ"}
CITATION_NOISE = {
    "doi", "isbn", "issn", "pmid", "pmc", "pmcid", "s2cid", "bibcode", "arxiv",
    "jstor", "oclc", "lccn", "ol", "vol", "pp", "ed", "eds", "et", "al", "ibid",
    "op", "cit", "retrieved", "archived", "wayback", "permalink", "url", "urls",
    "http", "https", "www", "isbn-13", "isbn-10",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")


# ===========================================================================
# 1. Lexicon index — load dictionaries; word / phrase / address resolver
# ===========================================================================
class LexiconIndex:
    def __init__(self):
        self.by_word = defaultdict(list)
        self.by_addr = {}                      # (lib, addr) -> full entry
        self.phrase_by_first = defaultdict(list)
        self.libs = []
        self._max_phrase_len = 1

    def load(self, path, lib_name=None):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        lib = lib_name or doc.get("library", path)
        self.libs.append(lib)
        for key, e in doc["entries"].items():
            word = e["word"]
            slim = {"lib": lib, "word": word, "addr": e["addr"], "dom": e["dom"],
                    "shell": e["shell"]}
            self.by_word[word].append(slim)
            full = dict(e); full["lib"] = lib
            self.by_addr[(lib, e["addr"])] = full
            if "_" in word:
                toks = tuple(word.split("_"))
                self.phrase_by_first[toks[0]].append((toks, slim))
                self._max_phrase_len = max(self._max_phrase_len, len(toks))
        for first in self.phrase_by_first:
            self.phrase_by_first[first].sort(key=lambda t: -len(t[0]))
        return self

    @property
    def max_phrase_len(self):
        return self._max_phrase_len

    def lookup_word(self, word):
        return self.by_word.get(word, [])

    def match_phrase(self, tokens, start):
        cands = self.phrase_by_first.get(tokens[start])
        if not cands:
            return None
        for toks, entry in cands:
            n = len(toks)
            if start + n <= len(tokens) and tuple(tokens[start:start + n]) == toks:
                return entry, n
        return None

    def stats(self):
        return {"libraries": self.libs, "unique_words": len(self.by_word),
                "addresses": len(self.by_addr), "max_phrase_len": self._max_phrase_len}


# ===========================================================================
# 2. Encoder — tokenise, lemmatise, multiword, hyphen-decompose, resolve
# ===========================================================================
class Encoder:
    def __init__(self, index, use_lemma=True, use_multiword=True,
                 use_decompose=True, strip_citations=True, lib_priority=None):
        self.ix = index
        self.use_lemma = use_lemma
        self.use_multiword = use_multiword
        self.use_decompose = use_decompose
        self.strip_citations = strip_citations
        self.lib_priority = lib_priority or list(index.libs)
        self._wl = None
        self._lc = {}

    def _lemma_forms(self, tok):
        if not self.use_lemma:
            return ()
        if tok in self._lc:
            return self._lc[tok]
        if self._wl is None:
            from nltk.stem import WordNetLemmatizer
            self._wl = WordNetLemmatizer()
        forms = []
        for pos in ("n", "v", "a", "r"):
            f = self._wl.lemmatize(tok, pos)
            if f != tok and f not in forms:
                forms.append(f)
        self._lc[tok] = tuple(forms)
        return self._lc[tok]

    def _pick(self, entries):
        has_fn = any(e["dom"] in FUNCTION_DOMS for e in entries)
        def rank(e):
            lp = self.lib_priority.index(e["lib"]) if e["lib"] in self.lib_priority else 99
            pref = (0 if e["dom"] in FUNCTION_DOMS else 1) if has_fn else 0
            return (pref, lp, e["addr"])
        return min(entries, key=rank)

    def _resolve_token(self, tok):
        hit = self.ix.lookup_word(tok)
        if hit:
            return self._pick(hit), "exact"
        for f in self._lemma_forms(tok):
            hit = self.ix.lookup_word(f)
            if hit:
                return self._pick(hit), "lemma"
        return None

    def encode(self, text):
        tokens = _TOKEN_RE.findall(text.lower())
        n = len(tokens)
        descriptors, concept, oov = [], {}, Counter()
        present, content_resolved, covered = set(), 0, 0
        i = 0
        while i < n:
            tok = tokens[i]
            if self.strip_citations and tok in CITATION_NOISE:
                i += 1; continue
            entry, via, span = None, None, 1
            if self.use_multiword and self.ix.max_phrase_len > 1:
                m = self.ix.match_phrase(tokens, i)
                if m:
                    entry, span, via = m[0], m[1], "phrase"
            if entry is None:
                r = self._resolve_token(tok)
                if r:
                    entry, via = r
            is_content = (len(tok) >= 2 and not tok.isdigit())
            if entry is None and self.use_decompose and "-" in tok:
                pes = [pr[0] for p in tok.split("-") if len(p) >= 2
                       for pr in [self._resolve_token(p)] if pr]
                if pes:
                    any_c = False
                    for pe in pes:
                        descriptors.append({"surface": tok, "word": pe["word"],
                                            "lib": pe["lib"], "addr": pe["addr"],
                                            "dom": pe["dom"], "shell": pe["shell"],
                                            "via": "decomp", "pos": i})
                        present.add((pe["lib"], pe["addr"]))
                        concept.setdefault(pe["word"], {"addr": pe["addr"], "dom": pe["dom"],
                                                        "lib": pe["lib"], "count": 0})
                        concept[pe["word"]]["count"] += 1
                        any_c = any_c or pe["dom"] not in FUNCTION_DOMS
                    covered += 1
                    if any_c:
                        content_resolved += 1
                    i += 1; continue
            if entry:
                descriptors.append({"surface": " ".join(tokens[i:i + span]),
                                    "word": entry["word"], "lib": entry["lib"],
                                    "addr": entry["addr"], "dom": entry["dom"],
                                    "shell": entry["shell"], "via": via, "pos": i})
                present.add((entry["lib"], entry["addr"]))
                covered += span
                w = entry["word"]
                concept.setdefault(w, {"addr": entry["addr"], "dom": entry["dom"],
                                       "lib": entry["lib"], "count": 0})
                concept[w]["count"] += 1
                if entry["dom"] not in FUNCTION_DOMS:
                    content_resolved += 1
                i += span
            else:
                if is_content:
                    oov[tok] += 1
                i += 1
        ctot = content_resolved + sum(oov.values())
        return {"descriptors": descriptors, "concept_index": concept,
                "stats": {"word_tokens": n, "covered_tokens": covered,
                          "coverage_pct": round(100 * covered / n, 2) if n else 0.0,
                          "content_coverage_pct": round(100 * content_resolved / ctot, 2) if ctot else 0.0}}


# ===========================================================================
# 3. Source extraction (lazy optional deps)
# ===========================================================================
def _tidy(t):
    t = (t or "").replace("\r", "\n")
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def extract(source, kind="auto"):
    if kind == "url" or (kind == "auto" and re.match(r"^https?://", source)):
        import requests
        r = requests.get(source, headers={"User-Agent": "Mozilla/5.0 (prose-to-xmap)"}, timeout=40)
        r.raise_for_status()
        if "pdf" in r.headers.get("Content-Type", "").lower() or source.lower().endswith(".pdf"):
            return _pdf(r.content, source)
        return _html(r.text, source)
    if kind == "pdf" or (kind == "auto" and source.lower().endswith(".pdf")):
        return _pdf(source, source)
    if kind == "html" or (kind == "auto" and source.lower().endswith((".html", ".htm"))):
        with open(source, encoding="utf-8", errors="replace") as fh:
            return _html(fh.read(), source)
    with open(source, encoding="utf-8", errors="replace") as fh:
        return {"text": _tidy(fh.read()), "meta": {"type": "text", "source": source}}


def _html(html, url=None):
    title, text = None, None
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True)
        md = trafilatura.extract_metadata(html)
        title = md.title if md else None
    except Exception:
        text = None
    if not text:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "form"]):
            t.decompose()
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        text = soup.get_text("\n")
    return {"text": _tidy(text), "meta": {"type": "html", "source": url, "title": title}}


def _pdf(path_or_bytes, source=None):
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else path_or_bytes)
    pages = []
    for pg in reader.pages:
        try:
            pages.append(pg.extract_text() or "")
        except Exception:
            pages.append("")
    title = None
    try:
        title = str(reader.metadata.title) if reader.metadata and reader.metadata.title else None
    except Exception:
        pass
    return {"text": _tidy("\n\n".join(pages)), "meta": {"type": "pdf", "source": source, "title": title}}



# ===========================================================================
# 4. Paragraph anchor map  (word-based; one anchor per paragraph)
# ===========================================================================
# domains that can serve as a paragraph "subject" (the anchor)
CONTENT_NOUN_DOMS = {"NOUNC", "NOUNA", "NOUNM", "NOUNP", "JARGON"}

COUPLING_TIERS = {
    "SYNERGISTIC": {"c_range": "[0.87, 1.01)", "interpretation": "cos≥0.87 | Δθ≤30° | near-identical phase"},
    "REINFORCING": {"c_range": "[0.61, 0.87)", "interpretation": "cos 0.61-0.87 | Δθ 30-52° | strong"},
    "MODERATE": {"c_range": "[0.34, 0.61)", "interpretation": "cos 0.34-0.61 | Δθ 52-70° | meaningful"},
    "WEAK": {"c_range": "[0.09, 0.34)", "interpretation": "cos 0.09-0.34 | Δθ 70-85° | distant"},
    "ORTHOGONAL": {"c_range": "[-0.09, 0.09)", "interpretation": "cos ~0 | Δθ ~90° | independent"},
    "OPPOSITION": {"c_range": "[-1.01, -0.09)", "interpretation": "cos < -0.09 | Δθ>95° | tension"},
}


def tier_from_c(c):
    if c >= 0.87: return "SYNERGISTIC"
    if c >= 0.61: return "REINFORCING"
    if c >= 0.34: return "MODERATE"
    if c >= 0.09: return "WEAK"
    if c >= -0.09: return "ORTHOGONAL"
    return "OPPOSITION"


def angular_sep(a, b):
    d = abs(a - b) % 360.0
    return round(d if d <= 180.0 else 360.0 - d, 1)


def _theta(addr):
    return float(addr.split("@")[1])


def split_paragraphs(text, mode, min_chars):
    if mode == "sections":
        raw = [c.strip() for c in re.split(r"\n\s*\n|\n", text) if c.strip()]
        chunks, buf = [], ""
        for c in raw:
            buf = (buf + "\n" + c).strip() if buf else c
            if len(buf) >= min_chars:
                chunks.append(buf); buf = ""
        if buf:
            (chunks.append(buf) if not chunks else chunks.__setitem__(-1, chunks[-1] + "\n" + buf))
    else:  # paragraphs
        chunks = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
        if len(chunks) < 2:
            chunks = [c.strip() for c in text.split("\n") if len(c.strip()) >= 40]
    out = []
    for c in chunks:
        if len(c.strip()) >= 40:
            out.append((c.strip().splitlines()[0][:60].strip(" #="), c))
    return out


def choose_anchor(ix, members):
    """Pick the paragraph subject: the most salient content noun.

    salience = frequency  +  ptr-degree to the rest of the paragraph (how many
    other concepts in the paragraph this one is linked to in the dictionaries).
    Falls back to the most frequent content word if there is no noun.
    """
    if not members:
        return None
    addr_set = {(m["lib"], m["addr"]) for m in members}

    def degree(m):
        full = ix.by_addr.get((m["lib"], m["addr"]), {})
        nb = set(full.get("ptr", []) + full.get("ptr_orthogonal", []))
        return sum(1 for (lib, a) in addr_set if a in nb)

    nouns = [m for m in members if m["dom"] in CONTENT_NOUN_DOMS]
    pool = nouns or members
    best = max(pool, key=lambda m: (m["count"] + 0.5 * degree(m), m["count"], -m["first_pos"]))
    best["salience"] = round(best["count"] + 0.5 * degree(best), 2)
    return best


def encode_paragraph(ix, enc, gid, label, text):
    res = enc.encode(text)
    # unique content descriptors with counts + first position
    agg = {}
    for d in res["descriptors"]:
        if d["dom"] in FUNCTION_DOMS:
            continue
        k = (d["lib"], d["addr"])
        if k not in agg:
            agg[k] = {"word": d["word"], "lib": d["lib"], "addr": d["addr"],
                      "dom": d["dom"], "shell": d["shell"], "theta": _theta(d["addr"]),
                      "count": 0, "first_pos": d["pos"]}
        agg[k]["count"] += 1
    members = list(agg.values())
    anchor = choose_anchor(ix, members)
    if anchor is None:
        return None
    total = sum(m["count"] for m in members) or 1
    a_theta = anchor["theta"]
    mapped, coup, orth, opp = [], [], [], []
    for m in members:
        if (m["lib"], m["addr"]) == (anchor["lib"], anchor["addr"]):
            continue
        dt = angular_sep(a_theta, m["theta"])
        c = round(math.cos(math.radians(dt)), 4)
        tier = tier_from_c(c)
        rec = {"word": m["word"], "lib": m["lib"], "addr": m["addr"], "dom": m["dom"],
               "shell": m["shell"], "theta": m["theta"], "count": m["count"],
               "delta_theta": dt, "c": c, "tier": tier}
        mapped.append(rec)
        (coup if c >= 0.34 else orth if c >= -0.09 else opp).append(m["addr"])
    mapped.sort(key=lambda r: -r["c"])
    conc = anchor["count"] / total
    cov = res["stats"]["content_coverage_pct"]
    score = round(0.6 * (cov / 100.0) + 0.4 * min(len(members) / 40.0, 1.0), 2)
    para = {
        "id": gid, "label": label, "shell": anchor["shell"],
        "anchor": {"word": anchor["word"], "lib": anchor["lib"], "addr": anchor["addr"],
                   "dom": anchor["dom"], "shell": anchor["shell"], "theta": a_theta,
                   "kappa": -round(1.0 + 3.0 * conc, 2), "salience": anchor.get("salience", 0),
                   "reason": "most salient content noun (frequency + in-paragraph ptr-degree)"},
        "anchor_addr": f"{anchor['lib']}:{anchor['addr']}",
        "member_count": len(mapped),
        "members": mapped,
        "ptr_coupling": sorted(set(coup)), "ptr_orthogonal": sorted(set(orth))[:5],
        "ptr_opposition": sorted(set(opp)),
        "semantic_role": f"paragraph anchored on '{anchor['word']}' ({anchor['dom']}); "
                         f"{len(mapped)} concepts mapped from it",
        "curation": {"coupling_strength": "central" if len(coup) >= 3 else "partial" if coup else "weak",
                     "orthogonal_status": "filled" if len(set(orth)) >= 3 else "partial" if orth else "empty",
                     "oppositional_status": "filled" if opp else "not_applicable",
                     "needs_curation": score < 0.90,
                     "auto_sources": ["wpe-5.0-anchormap", "lexicon-extraction"],
                     "sufficiency_score": score},
    }
    return para


def build_anchormap(ix, paragraphs, source_count, library):
    ids = [p["id"] for p in paragraphs]
    # anchor-to-anchor geometric coupling across paragraphs
    amat = {i: {} for i in ids}
    for a, pa in enumerate(paragraphs):
        for b, pb in enumerate(paragraphs):
            if a == b:
                amat[ids[a]][ids[b]] = {"c": 1.0, "delta_theta": 0.0, "tier": "SELF"}
                continue
            dt = angular_sep(pa["anchor"]["theta"], pb["anchor"]["theta"])
            c = round(math.cos(math.radians(dt)), 4)
            amat[ids[a]][ids[b]] = {"c": c, "delta_theta": dt, "tier": tier_from_c(c)}
    domains_table = {}
    for d in sorted({m["dom"] for p in paragraphs for m in p["members"]}
                    | {p["anchor"]["dom"] for p in paragraphs}):
        domains_table[d] = {"shell": DOMAINS[d]["shell"] if d in DOMAINS else 1,
                            "type": "lexicon",
                            "description": DOMAINS[d]["description"] if d in DOMAINS else d,
                            "paragraphs": [p["id"] for p in paragraphs
                                           if p["anchor"]["dom"] == d or any(m["dom"] == d for m in p["members"])]}
    # group paragraphs by anchor shell ("grouped in shells")
    shells = defaultdict(list)
    for p in paragraphs:
        shells[p["shell"]].append({"paragraph": p["id"], "anchor": p["anchor"]["word"],
                                   "anchor_addr": p["anchor"]["addr"]})
    return {
        "library": library, "file": f"{library.replace('.', '_')}.json",
        "schema": "wpe-5.0-anchormap-1.0", "kind": "paragraph_anchor_map",
        "wpe_version": "5.0", "tme_version": "1.0", "temporal_scale": "α=1.0",
        "source_file_count": source_count, "paragraph_count": len(paragraphs),
        "anchor_count": len({p["anchor"]["addr"] for p in paragraphs}),
        "model": ("word-based: every content word resolves to a lexicon descriptor (<shell>@<theta>); "
                  "words are grouped by paragraph; each paragraph's subject is its ANCHOR; every other "
                  "word is mapped from the anchor by geometric coupling c=cos(Δθ). Paragraphs are grouped "
                  "onto shells by their anchor's domain."),
        "coupling_tiers": COUPLING_TIERS,
        "domains_table": domains_table,
        "shell_groups": dict(sorted(shells.items())),
        "paragraphs": {p["id"]: p for p in paragraphs},
        "anchor_coupling_matrix": {"order": ids,
                                   "cell_schema": {"c": "cos(Δθ) between paragraph anchors",
                                                   "delta_theta": "phase sep of anchors (deg)",
                                                   "tier": "tier label"},
                                   "values": amat},
        "consistency_invariants": [
            "anchor.addr resolves to a real lexicon entry; anchor.theta == theta(anchor.addr)",
            "paragraph.shell == anchor.shell",
            "every member maps from the anchor: member.delta_theta == angular_sep(anchor.theta, member.theta)",
            "member.c == cos(member.delta_theta); member.tier == tier_from_c(member.c)",
            "ptr_coupling = members with c≥0.34; ptr_orthogonal = -0.09≤c<0.34; ptr_opposition = c<-0.09",
            "anchor_coupling_matrix symmetric; diagonal c==1.0 (SELF)",
            "curation.orthogonal_status='filled' implies len(ptr_orthogonal)>=3",
            "sufficiency_score ∈ [0,1]; <0.90 implies needs_curation",
        ],
        "lookup_paths": {
            "paragraph": "doc.paragraphs[<GID>]",
            "anchor": "doc.paragraphs[<GID>]['anchor']",
            "mapped_concepts": "doc.paragraphs[<GID>]['members']  (sorted by coupling c desc)",
            "paragraphs_on_shell": "doc.shell_groups[<SHELL>]",
            "anchor_coupling": "doc.anchor_coupling_matrix['values'][Gi][Gj]",
        },
    }


# ===========================================================================
# 5. CLI
# ===========================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="word-based per-paragraph anchor map (wpe-5.0-anchormap-1.0)")
    ap.add_argument("sources", nargs="+", help="files/URLs")
    ap.add_argument("--lex", nargs="+", required=True, help="lexicon-1.0 dictionary JSON files (priority order)")
    ap.add_argument("--split", default="paragraphs", choices=["paragraphs", "sections"])
    ap.add_argument("--min-chars", type=int, default=400, help="min chars per chunk when --split sections")
    ap.add_argument("--library", default="ProseWorld.AnchorMap")
    ap.add_argument("--out", default="anchormap.json")
    args = ap.parse_args(argv)

    ix = LexiconIndex()
    for p in args.lex:
        ix.load(p)
    print(f"lexicons: {ix.stats()}", file=sys.stderr)
    enc = Encoder(ix)

    paragraphs = []
    for src in args.sources:
        doc = extract(src)
        for label, text in split_paragraphs(doc["text"], args.split, args.min_chars):
            gid = f"G{len(paragraphs) + 1}"
            para = encode_paragraph(ix, enc, gid, label, text)
            if para and para["member_count"] >= 1:
                paragraphs.append(para)
                print(f"  {gid}: anchor={para['anchor']['word']!r} ({para['anchor']['dom']} "
                      f"{para['anchor']['addr']}) <- {para['member_count']} concepts", file=sys.stderr)

    if not paragraphs:
        sys.exit("no paragraphs with resolvable content found")
    doc = build_anchormap(ix, paragraphs, len(args.sources), args.library)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"\nparagraphs={doc['paragraph_count']} anchors={doc['anchor_count']} -> {args.out}")


if __name__ == "__main__":
    main()
