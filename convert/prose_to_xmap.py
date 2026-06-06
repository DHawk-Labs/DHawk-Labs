#!/usr/bin/env python3
"""
prose_to_xmap.py — SINGLE-FILE prose-snippets → WPE-5 cross-coupling map.

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
# 4. Snippets, part placement, coupling
# ===========================================================================
COUPLING_TIERS = {
    "SYNERGISTIC": {"c_range": "[0.87, 1.01)", "delta_theta_range": "Δθ∈[0.0°, 29.5°]",
                    "interpretation": "cos≥0.87 | Δθ≤30° | near-identical phase alignment"},
    "REINFORCING": {"c_range": "[0.61, 0.87)", "delta_theta_range": "Δθ∈[29.6°, 52.4°]",
                    "interpretation": "cos 0.61-0.87 | Δθ 30-52° | strong positive coupling"},
    "MODERATE": {"c_range": "[0.34, 0.61)", "delta_theta_range": "Δθ∈[52.4°, 70.1°]",
                 "interpretation": "cos 0.34-0.61 | Δθ 52-70° | meaningful coupling with some offset"},
    "WEAK": {"c_range": "[0.09, 0.34)", "delta_theta_range": "Δθ∈[70.1°, 84.8°]",
             "interpretation": "cos 0.09-0.34 | Δθ 70-85° | distant but present influence"},
    "ORTHOGONAL": {"c_range": "[-0.09, 0.09)", "delta_theta_range": "Δθ∈[84.8°, 95.2°]",
                   "interpretation": "cos ~0 | Δθ ~90° | independent axes, no direct coupling"},
    "OPPOSITION": {"c_range": "[-1.01, -0.09)", "delta_theta_range": "Δθ > 95.2°",
                   "interpretation": "cos < -0.09 | Δθ>95° | active tension or complementary opposition"},
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


def split_snippets(text, mode, min_chars=800):
    if mode == "paragraphs":
        chunks = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
        if len(chunks) < 2:
            chunks = [c.strip() for c in text.split("\n") if c.strip()]
    elif mode == "sections":
        raw = [c.strip() for c in re.split(r"\n\s*\n|\n", text) if c.strip()]
        chunks, buf = [], ""
        for c in raw:
            buf = (buf + "\n" + c).strip() if buf else c
            if len(buf) >= min_chars:
                chunks.append(buf); buf = ""
        if buf:
            (chunks.append(buf) if not chunks else chunks.__setitem__(-1, chunks[-1] + "\n" + buf))
    else:
        chunks = [text]
    out = []
    for i, c in enumerate(chunks, 1):
        first = c.strip().splitlines()[0][:60] if c.strip() else f"snippet {i}"
        out.append((first.strip(" #="), c))
    return out


def circular_mean_deg(thetas):
    sx = sum(math.cos(math.radians(t)) for t in thetas)
    sy = sum(math.sin(math.radians(t)) for t in thetas)
    if sx == 0 and sy == 0:
        return 0.0
    return round(math.degrees(math.atan2(sy, sx)) % 360.0, 1)


def encode_part(ix, enc, label, text, source_meta):
    res = enc.encode(text)
    content = [d for d in res["descriptors"] if d["dom"] not in FUNCTION_DOMS]
    dom_count = Counter(d["dom"] for d in content) or Counter({"META": 1})
    tf = Counter((d["lib"], d["addr"]) for d in content)
    domain_codes = [d for d, _ in dom_count.most_common()]
    shell = min(DOMAINS[d]["shell"] for d in domain_codes if d in DOMAINS)
    dom = dom_count.most_common(1)[0][0]
    theta = circular_mean_deg([float(d["addr"].split("@")[1]) for d in content]) if content else 0.0
    conc = dom_count.most_common(1)[0][1] / sum(dom_count.values())
    part = {"label": label, "filename": source_meta.get("source") or label,
            "shell": shell, "theta": theta, "kappa": -round(1.0 + 3.0 * conc, 2),
            "dom": dom, "domain_codes": domain_codes,
            "section_count": text.count("\n\n") + 1,
            "component_count_approx": len(res["descriptors"]),
            "unique_concepts": len(tf), "_content_coverage": res["stats"]["content_coverage_pct"],
            "semantic_role": f"snippet_layer — '{label[:48]}' dominated by domains [{', '.join(domain_codes[:3])}]",
            "_top_concepts": [{"lib": lib, "addr": a, "word": ix.by_addr[(lib, a)]["word"], "count": n}
                              for (lib, a), n in tf.most_common(6)]}
    return part, tf


def expand_vector(ix, tf, nb=0.3):
    vec = defaultdict(float)
    for (lib, addr), w in tf.items():
        vec[(lib, addr)] += float(w)
        full = ix.by_addr.get((lib, addr), {})
        for a in full.get("ptr", []) + full.get("ptr_orthogonal", []):
            vec[(lib, a)] += nb * w
    return vec


def cosine(va, vb):
    keys = set(va) & set(vb)
    if not keys:
        return 0.0
    dot = sum(va[k] * vb[k] for k in keys)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


def apply_idf(vectors):
    N = len(vectors)
    df = Counter()
    for v in vectors:
        for k in v:
            df[k] += 1
    for v in vectors:
        for k in list(v):
            v[k] *= math.log((N + 1) / df[k]) + 1.0
    return vectors


# ===========================================================================
# 5. Assemble the wpe-5.0-xmap-1.0 document
# ===========================================================================
def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48] or "snippet"


def _assign_addresses(parts):
    used = set()
    for p in parts:
        theta = p["theta"]
        while f"{p['shell']}@{theta}" in used:
            theta = round((theta + 0.1) % 360.0, 1)
        p["theta"] = theta
        p["addr"] = f"{p['shell']}@{theta}"
        used.add(p["addr"])


def build_xmap(ix, encoded, source_count, library, coupling="geometric"):
    parts = [e[0] for e in encoded]
    ids = [f"P{i}" for i in range(1, len(parts) + 1)]
    for pid, p in zip(ids, parts):
        p["id"] = pid
    _assign_addresses(parts)
    addr_of = {pid: p["addr"] for pid, p in zip(ids, parts)}
    vectors = apply_idf([expand_vector(ix, e[1]) for e in encoded]) if coupling == "semantic" else None

    n = len(parts)
    cmat = {pid: {} for pid in ids}
    for a in range(n):
        for b in range(n):
            if a == b:
                cmat[ids[a]][ids[b]] = {"c": 1.0, "delta_theta": 0.0, "tier": "SELF"}
                continue
            if coupling == "geometric":
                dt = angular_sep(parts[a]["theta"], parts[b]["theta"])
                c = round(math.cos(math.radians(dt)), 4)
            else:
                c = round(max(-1.0, min(1.0, cosine(vectors[a], vectors[b]))), 4)
                dt = round(math.degrees(math.acos(max(-1.0, min(1.0, c)))), 1)
            cmat[ids[a]][ids[b]] = {"c": c, "delta_theta": dt, "tier": tier_from_c(c)}

    for a, pid in enumerate(ids):
        seq, coup, orth, opp = [], [], [], []
        for b, qid in enumerate(ids):
            if a == b:
                continue
            c = cmat[pid][qid]["c"]
            if c >= 0.34:
                (seq if b > a else coup).append((c, addr_of[qid]))
            elif c >= -0.09:
                orth.append((c, addr_of[qid]))
            else:
                opp.append((c, addr_of[qid]))
        seq.sort(reverse=True); coup.sort(reverse=True); orth.sort(reverse=True); opp.sort()
        p = parts[a]
        p["ptr_sequence"] = [x for _, x in seq]
        p["ptr_coupling"] = [x for _, x in coup]
        p["ptr_orthogonal"] = [x for _, x in orth[:5]]
        p["ptr_opposition"] = [x for _, x in opp]
        _finish_part(p, ix)

    domains_table = {}
    for d in sorted({d for p in parts for d in p["domain_codes"]}):
        domains_table[d] = {"shell": DOMAINS[d]["shell"] if d in DOMAINS else 1,
                            "type": "lexicon",
                            "description": DOMAINS[d]["description"] if d in DOMAINS else d,
                            "parts": [p["id"] for p in parts if d in p["domain_codes"]]}

    xc = _cross_coupling_index(parts, cmat, ids)
    return {
        "library": library, "file": f"{library.replace('.', '_')}.json",
        "schema": "wpe-5.0-xmap-1.0", "kind": "cross_coupling_map",
        "wpe_version": "5.0", "tme_version": "1.0", "temporal_scale": "α=1.0",
        "source_file_count": source_count, "unique_part_count": n,
        "coupling_pairs_total": n * (n - 1) // 2, "coupling_pairs_with_detail": len(xc),
        "domain_count": len(domains_table), "top_level_fields": _TOP_LEVEL_FIELDS,
        "part_key_format": "<PART_ID>", "part_key_example": "P4", "part_shape": _PART_SHAPE,
        "address_format": {"pattern": "<shell>@<theta>",
                           "example": parts[0]["addr"] if parts else "9@270.0",
                           "uniqueness": "every part has a unique addr; ptr fields reference parts by addr",
                           "lookup": "parts_by_addr[addr] -> part entry"},
        "reference_graph": _REFERENCE_GRAPH, "coupling_tiers": COUPLING_TIERS,
        "domains_table": domains_table, "parts": {p["id"]: _emit_part(p) for p in parts},
        "coupling_matrix": {"parts_order": ids,
                            "cell_schema": {"c": "float cos(Δθ) coupling strength",
                                            "delta_theta": "float effective phase separation in degrees",
                                            "tier": "string coupling tier label"},
                            "values": cmat},
        "cross_coupling_index": xc,
        "shell_hierarchy": _shell_hierarchy(parts, cmat, ids),
        "phase_resonance_bands": _phase_bands(parts, cmat, ids),
        "consistency_invariants": _INVARIANTS, "lookup_paths": _LOOKUP_PATHS,
    }


def _finish_part(p, ix):
    shells = [ix.by_addr[(c["lib"], c["addr"])]["shell"] for c in p["_top_concepts"]] or [p["shell"]]
    p["shell_range"] = {"min": min(shells), "max": max(shells)}
    p["key_subsystems"] = [c["word"] for c in p["_top_concepts"]]
    p["wpe_built_ins_used"] = []
    p["wpe_output"] = f"{p['dom']}:{p['addr']}|{p['kappa']}:'{_slug(p['label'])}_master'"
    n_strong = len(p["ptr_sequence"]) + len(p["ptr_coupling"])
    n_orth = len(p["ptr_orthogonal"])
    cov = p.pop("_content_coverage", 0.0)
    score = round(0.6 * (cov / 100.0) + 0.4 * min(p.get("unique_concepts", 0) / 40.0, 1.0), 2)
    p["curation"] = {"coupling_strength": "central" if n_strong >= 2 else "partial" if n_strong == 1 else "weak",
                     "orthogonal_status": "filled" if n_orth >= 3 else "partial" if n_orth >= 1 else "empty",
                     "oppositional_status": "filled" if p["ptr_opposition"] else "not_applicable",
                     "needs_curation": score < 0.90,
                     "auto_sources": ["wpe-5.0-xmap", "lexicon-extraction"],
                     "sufficiency_score": score}


_PART_KEYS = ["id", "addr", "label", "filename", "shell", "theta", "kappa", "dom",
              "domain_codes", "wpe_built_ins_used", "shell_range", "section_count",
              "component_count_approx", "key_subsystems", "wpe_output",
              "ptr_sequence", "ptr_coupling", "ptr_orthogonal", "ptr_opposition",
              "semantic_role", "curation"]


def _emit_part(p):
    return {k: p[k] for k in _PART_KEYS if k in p}


def _concept_meta(part):
    return {"component": f"{part['id']}.{part['key_subsystems'][0] if part['key_subsystems'] else part['dom']}",
            "dom": part["dom"], "shell": part["shell"], "theta": part["theta"], "kappa": part["kappa"],
            "wpe": f"{part['dom']}:{part['shell']}@{part['theta']}|{part['kappa']}"}


def _key_bridges(sp, tp, k=4):
    s = {c["word"] for c in sp["_top_concepts"]}
    t = {c["word"] for c in tp["_top_concepts"]}
    shared = sorted(s & t)
    if shared:
        return [f"{sp['id']}:{w} -> {tp['id']}:{w}" for w in shared[:k]]
    return [f"{sp['id']}:{a['word']} -> {tp['id']}:{b['word']}"
            for a, b in zip(sp["_top_concepts"][:k], tp["_top_concepts"][:k])]


def _cross_coupling_index(parts, cmat, ids, cap=40):
    by_id = {p["id"]: p for p in parts}
    pairs = sorted(
        ((cmat[ids[a]][ids[b]]["c"], ids[a], ids[b])
         for a in range(len(ids)) for b in range(a + 1, len(ids))
         if cmat[ids[a]][ids[b]]["c"] >= 0.34),
        reverse=True)
    out = []
    for c, pi, pj in pairs[:cap]:
        sp, tp = by_id[pi], by_id[pj]
        if not sp["_top_concepts"] or not tp["_top_concepts"]:
            continue
        cell = cmat[pi][pj]
        st, tt = _concept_meta(sp), _concept_meta(tp)
        idt = angular_sep(st["theta"], tt["theta"])
        hi, lo = max(st["shell"], tt["shell"]), min(st["shell"], tt["shell"])
        infl = round(1.0 / lo - 1.0 / hi, 4) if hi > lo else 0.0
        sw, tw = sp["key_subsystems"][0], tp["key_subsystems"][0]
        out.append({"id": f"XC.{pi}.{pj}.{_slug(sw + '_' + tw)}",
                    "source_part": pi, "target_part": pj, "operator": "coupling",
                    "source_addr": sp["addr"], "target_addr": tp["addr"],
                    "delta_theta_part_level": cell["delta_theta"],
                    "coupling_strength_part_level": cell["c"], "tier": cell["tier"],
                    "direction": "bidirectional", "source_interface": st, "target_interface": tt,
                    "interface_delta_theta": idt, "interface_coupling_strength": round(math.cos(math.radians(idt)), 4),
                    "shell_influence": {"formula": "I(λ_h,λ_l)=1/λ_l−1/λ_h", "lambda_high": hi,
                                        "lambda_low": lo, "value": infl,
                                        "note": f"shell {hi}→{lo} gradient" if hi > lo else "same shell — no gradient"},
                    "semantic_label": _slug(sw + "_couples_" + tw), "key_bridges": _key_bridges(sp, tp),
                    "wpe_notation": f"${pi}.{sw} coupling ${pj}.{tw}"})
    return out


def _shell_hierarchy(parts, cmat, ids):
    labels = {1: "Foundation", 2: "Processing", 3: "Integration", 4: "Context", 5: "Abstraction",
              6: "Meta", 7: "CrossSystem", 8: "Structural", 9: "GrandMaster", 10: "Apex"}
    levels = {}
    for p in sorted(parts, key=lambda x: x["shell"]):
        lv = levels.setdefault(str(p["shell"]),
                               {"label": labels.get(p["shell"], f"Shell{p['shell']}"),
                                "description": f"parts whose master node sits on shell {p['shell']}",
                                "parts_dominant": []})
        lv["parts_dominant"].append(f"{p['id']}.{p['dom']}")
    grads, seen = [], set()
    for a in range(len(ids)):
        for b in range(len(ids)):
            if a == b:
                continue
            hi, lo = max(parts[a]["shell"], parts[b]["shell"]), min(parts[a]["shell"], parts[b]["shell"])
            if hi > lo and cmat[ids[a]][ids[b]]["c"] >= 0.34 and (hi, lo) not in seen:
                seen.add((hi, lo))
                grads.append({"from_shell": hi, "to_shell": lo, "I": round(1.0 / lo - 1.0 / hi, 3),
                              "parts": f"shell {hi} parts provide downward context to shell {lo}"})
    return {"formula": "I(λ_h,λ_l) = 1/λ_l − 1/λ_h  where λ_h > λ_l",
            "semantics": "higher shells exert downward context influence on lower shells; information flows λ_high → λ_low",
            "levels": levels, "key_influence_gradients": grads[:8]}


def _phase_bands(parts, cmat, ids):
    names = ["α_band_[0,60)", "β_band_[60,120)", "γ_band_[120,180)",
             "δ_band_[180,240)", "ε_band_[240,300)", "ζ_band_[300,360)"]
    id_of = {p["id"]: p for p in parts}
    bands = {}
    for bi, name in enumerate(names):
        lo = bi * 60
        members = [p["id"] for p in parts if lo <= p["theta"] < lo + 60]
        entry = {"representative_theta": lo + 30, "parts": members}
        if len(members) >= 2:
            cs = [cmat[members[i]][members[j]]["c"]
                  for i in range(len(members)) for j in range(i + 1, len(members))]
            entry["c_inter"] = round(sum(cs) / len(cs), 2)
            entry["semantic"] = "_".join(d.lower() for d, _ in Counter(id_of[m]["dom"] for m in members).most_common(3))
        bands[name] = entry
    return {"description": "Parts whose dominant θ falls in the same 60° arc share phase resonance and exhibit strongest natural coupling",
            "bands": bands}


# ---- verbatim schema-description blocks (match the PanWorld layout) -------
_TOP_LEVEL_FIELDS = {
    "library": "string — library name", "file": "string — canonical output filename",
    "schema": "wpe-5.0-xmap-1.0", "kind": "cross_coupling_map",
    "parts": "object keyed by part ID (P1…PN); each entry is a part_shape",
    "domains_table": "object mapping domain code -> {shell, type, description, parts}",
    "coupling_tiers": "object defining 6 tier levels by cos(Δθ) range",
    "cross_coupling_index": "array of detailed coupling records between specific interface components",
    "coupling_matrix": "symmetric N×N matrix of {c, delta_theta, tier} for every part pair",
    "shell_hierarchy": "object defining shell levels, influence formula, and key gradients",
    "phase_resonance_bands": "6 phase bands (60° each) with resident parts and inter-part c",
    "consistency_invariants": "list of invariants that must hold across all entries",
    "lookup_paths": "navigation guide for common query patterns",
}
_PART_SHAPE = {
    "id": "string — canonical part identifier e.g. P1, P4", "label": "string — human-readable title",
    "filename": "string — source filename/URL",
    "shell": "int — dominant WPE shell (min over the part's domain shells)",
    "theta": "float — angular coordinate of the part's master output node [0,359]",
    "kappa": "float — curvature of the master node (κ < 0 = stable well)",
    "addr": "string — '<shell>@<theta>' unique address; used in ptr fields",
    "dom": "string — primary domain code for this part",
    "domain_codes": "list[string] — all domain codes the part resolves into",
    "ptr_sequence": "list[addr] — parts this part FEEDS-INTO (directed)",
    "ptr_coupling": "list[addr] — parts bidirectionally coupled (c ≥ 0.34)",
    "ptr_orthogonal": "list[addr] — weakly related parts (-0.09 ≤ c < 0.34)",
    "ptr_opposition": "list[addr] — parts in active tension (c < -0.09)",
    "semantic_role": "string — plain-language role of this part",
    "curation": {"coupling_strength": "central | partial | weak",
                 "orthogonal_status": "filled | partial | empty",
                 "oppositional_status": "filled | empty | not_applicable",
                 "needs_curation": "bool", "auto_sources": "list[string]",
                 "sufficiency_score": "float 0.0-1.0"},
}
_REFERENCE_GRAPH = {
    "ptr_sequence": {"operator": "*", "direction": "outgoing, directed",
                     "semantics": "this part FEEDS-INTO / IS-PREREQUISITE-FOR the target part", "cardinality": "0..N"},
    "ptr_coupling": {"operator": "coupling", "direction": "bidirectional",
                     "semantics": "this part IS-BIDIRECTIONALLY-COUPLED-WITH the target", "cardinality": "0..N"},
    "ptr_orthogonal": {"operator": "parallel (+) or none", "direction": "outgoing reference",
                       "semantics": "this part IS-ON-A-DIFFERENT-AXIS-FROM but still relates to target", "cardinality": "0..5"},
    "ptr_opposition": {"operator": "coupling (tension)", "direction": "bidirectional",
                       "semantics": "this part IS-IN-ACTIVE-TENSION-WITH the target; cos(Δθ) < -0.09", "cardinality": "0..N (list)"},
}
_INVARIANTS = [
    "part.addr == '<part.shell>@<part.theta>'",
    "part.shell == min(domains_table[d].shell for d in part.domain_codes)",
    "every addr in ptr_sequence / ptr_coupling / ptr_orthogonal / ptr_opposition must resolve to an existing part.addr",
    "ptr_opposition is a list of strings (may be empty); entries are addrs of parts with cos(Δθ) < -0.09",
    "ptr_orthogonal has entries with -0.09 ≤ cos(Δθ) < 0.34 (WEAK or ORTHOGONAL tier)",
    "coupling_matrix[Pi][Pj].c == coupling_matrix[Pj][Pi].c  (symmetric)",
    "coupling_matrix[Pi][Pi].c == 1.0 for all Pi (self-coupling = 1)",
    "coupling_matrix[Pi][Pj].tier == tier_from_c(coupling_matrix[Pi][Pj].c)",
    "every cross_coupling_index entry's source_part and target_part must be in parts",
    "interface_coupling_strength = cos(interface_delta_theta)",
    "shell_influence.value = 1/lambda_low − 1/lambda_high when lambda_high > lambda_low; else 0",
    "curation.orthogonal_status='filled' implies len(ptr_orthogonal) >= 3",
    "curation.orthogonal_status='partial' implies 1 <= len(ptr_orthogonal) <= 2",
    "sufficiency_score ∈ [0.0, 1.0]; score < 0.90 implies needs_curation = true",
    "operator ∈ {'sequence','parallel','coupling'}; 'coupling' implies bidirectional; 'sequence' implies directed",
]
_LOOKUP_PATHS = {
    "part_by_id": "doc.parts[<PART_ID>]  e.g. doc.parts['P4']",
    "part_by_addr": "next(p for p in doc.parts.values() if p['addr']==addr)",
    "coupling": "doc.coupling_matrix['Pi']['Pj']  — always symmetric",
    "xc_detail": "doc.cross_coupling_index[i] filtered by source_part/target_part",
    "strongest_couplings": "sorted(doc.coupling_matrix[pi].items(), key=lambda x: x[1]['c'], reverse=True)",
    "parts_in_tier": "[(pi,pj) for pi in parts for pj in parts if coupling_matrix[pi][pj]['tier']==<TIER>]",
    "domain_to_part": "doc.domains_table[<DOM>]['parts']",
    "shell_peers": "[p for p in doc.parts.values() if p['shell']==<SHELL>]",
}


# ===========================================================================
# 6. CLI
# ===========================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="prose snippets -> wpe-5.0-xmap-1.0 cross-coupling map")
    ap.add_argument("sources", nargs="+", help="files/URLs; each is one part (or split one with --split)")
    ap.add_argument("--lex", nargs="+", required=True, help="lexicon-1.0 dictionary JSON files (priority order)")
    ap.add_argument("--split", default="whole", choices=["whole", "sections", "paragraphs"])
    ap.add_argument("--min-chars", type=int, default=800)
    ap.add_argument("--coupling", default="geometric", choices=["geometric", "semantic"])
    ap.add_argument("--library", default="ProseWorld.XMap")
    ap.add_argument("--out", default="xmap.json")
    args = ap.parse_args(argv)

    ix = LexiconIndex()
    for p in args.lex:
        ix.load(p)
    print(f"lexicons: {ix.stats()}", file=sys.stderr)
    enc = Encoder(ix)

    encoded = []
    for src in args.sources:
        doc = extract(src)
        for label, text in split_snippets(doc["text"], args.split, args.min_chars):
            if len(text.strip()) < 80:
                continue
            meta = dict(doc["meta"])
            part_label = label if args.split != "whole" else (doc["meta"].get("title") or label)
            encoded.append(encode_part(ix, enc, part_label, text, meta))
            print(f"  part {len(encoded)}: {part_label[:40]!r} shell={encoded[-1][0]['shell']} "
                  f"theta={encoded[-1][0]['theta']} dom={encoded[-1][0]['dom']}", file=sys.stderr)

    if len(encoded) < 2:
        sys.exit("need >= 2 snippets to build a coupling map (use --split)")
    xmap = build_xmap(ix, encoded, len(args.sources), args.library, args.coupling)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(xmap, fh, ensure_ascii=False, indent=2)
    print(f"\nparts={xmap['unique_part_count']} pairs={xmap['coupling_pairs_total']} "
          f"detail={xmap['coupling_pairs_with_detail']} coupling={args.coupling} -> {args.out}")


if __name__ == "__main__":
    main()
