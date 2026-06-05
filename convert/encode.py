"""
encode.py — convert clean prose into a structured lexicon encoding.

Pipeline:  text -> tokens -> (multiword phrase match | word | lemma) lookup in
the loaded dictionaries -> ordered descriptor stream + concept index + pointer
subgraph + out-of-vocabulary report.

The output is a JSON document keyed to the same lexicon-1.0 addresses
(``<shell>@<theta>``) used by the dictionaries, so an encoded document is
literally a path through the lexicon space — directly consumable by anything
that already speaks the dictionary format.

Resolution is layered, and each layer can be toggled, which is how the methods
were iterated and measured:

  exact      lowercase surface form
  lemma      WordNet lemma (cells->cell, studies->study, running->run)
  multiword  greedy longest-match of 'underscore_separated' terms
             (e.g. "myocardial infarction" -> myocardial_infarction)
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

# function-word domains — resolved, but excluded from "content coverage"
FUNCTION_DOMS = {"PRON", "DET", "PREP", "CONJ", "AUX", "NUM", "INTERJ"}

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")


class Encoder:
    def __init__(self, index, use_lemma=True, use_multiword=True,
                 use_decompose=True, lib_priority=None):
        self.ix = index
        self.use_lemma = use_lemma
        self.use_multiword = use_multiword
        self.use_decompose = use_decompose
        # which library wins when a term exists in several (e.g. SciMed first)
        self.lib_priority = lib_priority or list(index.libs)
        self._lemmatizer = None
        self._lemma_cache = {}

    # ------------------------------------------------------------------ lemma
    def _lemma_forms(self, tok):
        if not self.use_lemma:
            return ()
        if tok in self._lemma_cache:
            return self._lemma_cache[tok]
        if self._lemmatizer is None:
            from nltk.stem import WordNetLemmatizer
            self._lemmatizer = WordNetLemmatizer()
        wl = self._lemmatizer
        forms = []
        for pos in ("n", "v", "a", "r"):
            f = wl.lemmatize(tok, pos)
            if f != tok and f not in forms:
                forms.append(f)
        forms = tuple(forms)
        self._lemma_cache[tok] = forms
        return forms

    def _pick(self, entries):
        """Choose one entry when a term resolves in several libs/domains.

        If a word has a function-word entry at all, it is a closed-class word
        (the/a/in/of/and/will/...), whose function reading dominates running
        prose — so prefer it. Otherwise prefer the content sense. (A full POS
        tagger would disambiguate the residual cases like 'can'/'will' with
        sentence context; this heuristic is correct for the vast majority.)
        """
        has_function = any(e["dom"] in FUNCTION_DOMS for e in entries)
        def rank(e):
            lp = (self.lib_priority.index(e["lib"])
                  if e["lib"] in self.lib_priority else 99)
            if has_function:
                pref = 0 if e["dom"] in FUNCTION_DOMS else 1
            else:
                pref = 0
            return (pref, lp, e["addr"])
        return min(entries, key=rank)

    def _resolve_token(self, tok):
        """Return (entry, via) for a single token, or None."""
        hit = self.ix.lookup_word(tok)
        if hit:
            return self._pick(hit), "exact"
        for f in self._lemma_forms(tok):
            hit = self.ix.lookup_word(f)
            if hit:
                return self._pick(hit), "lemma"
        return None

    # ----------------------------------------------------------------- encode
    def encode(self, text, source_meta=None):
        tokens = _TOKEN_RE.findall(text.lower())
        n = len(tokens)
        descriptors = []
        concept = {}                 # word -> {addr,dom,lib,count}
        oov = Counter()
        present_addrs = set()
        content_resolved = 0
        covered_tokens = 0          # number of source tokens spanned by a match
        i = 0
        while i < n:
            tok = tokens[i]
            entry = None
            via = None
            span = 1

            # 1) longest multiword phrase
            if self.use_multiword and self.ix.max_phrase_len > 1:
                m = self.ix.match_phrase(tokens, i)
                if m:
                    entry, span = m[0], m[1]
                    via = "phrase"

            # 2) single word (exact, then lemma)
            if entry is None:
                r = self._resolve_token(tok)
                if r:
                    entry, via = r

            is_content = (len(tok) >= 2 and not tok.isdigit())

            # 3) hyphenated compound: resolve component words
            #    (light-dependent -> light, dependent ; st-segment -> segment)
            if entry is None and self.use_decompose and "-" in tok:
                parts = [p for p in tok.split("-") if len(p) >= 2]
                part_entries = []
                for p in parts:
                    pr = self._resolve_token(p)
                    if pr:
                        part_entries.append(pr[0])
                if part_entries:
                    any_content = False
                    for pe in part_entries:
                        descriptors.append({
                            "surface": tok, "word": pe["word"], "lib": pe["lib"],
                            "addr": pe["addr"], "dom": pe["dom"],
                            "shell": pe["shell"], "via": "decomp", "pos": i,
                        })
                        present_addrs.add((pe["lib"], pe["addr"]))
                        w = pe["word"]
                        if w not in concept:
                            concept[w] = {"addr": pe["addr"], "dom": pe["dom"],
                                          "lib": pe["lib"], "count": 0}
                        concept[w]["count"] += 1
                        any_content = any_content or pe["dom"] not in FUNCTION_DOMS
                    covered_tokens += 1          # the hyphen token counts once
                    if any_content:              # ... as a single content hit
                        content_resolved += 1
                    i += 1
                    continue

            if entry:
                descriptors.append({
                    "surface": " ".join(tokens[i:i + span]),
                    "word": entry["word"], "lib": entry["lib"],
                    "addr": entry["addr"], "dom": entry["dom"],
                    "shell": entry["shell"], "via": via, "pos": i,
                })
                present_addrs.add((entry["lib"], entry["addr"]))
                covered_tokens += span
                w = entry["word"]
                if w not in concept:
                    concept[w] = {"addr": entry["addr"], "dom": entry["dom"],
                                  "lib": entry["lib"], "count": 0}
                concept[w]["count"] += 1
                if entry["dom"] not in FUNCTION_DOMS:
                    content_resolved += 1
                i += span
            else:
                if is_content:
                    oov[tok] += 1
                i += 1

        # pointer subgraph: pointers among descriptors present in-doc. Pointers
        # reference addresses within the SAME library, so resolve per (lib,addr).
        ptr_graph = {}
        for (lib, addr) in present_addrs:
            full = self.ix.by_addr.get((lib, addr), {})
            outs = []
            for a in (full.get("ptr", []) + full.get("ptr_orthogonal", [])):
                if (lib, a) in present_addrs and a != addr:
                    outs.append(a)
            opp = full.get("ptr_oppositional", "")
            if opp and (lib, opp) in present_addrs:
                outs.append(opp)
            if outs:
                ptr_graph[f"{lib}:{addr}"] = sorted(set(outs))

        resolved = len(descriptors)
        # content tokens = content words that resolved to a content domain PLUS
        # content words that did not resolve at all (OOV). Function words that
        # resolved are intentionally excluded from both sides.
        oov_occurrences = sum(oov.values())
        content_total = content_resolved + oov_occurrences
        dom_hist = Counter(d["dom"] for d in descriptors)
        lib_hist = Counter(d["lib"] for d in descriptors)
        stats = {
            "word_tokens": n,
            "descriptors_emitted": resolved,
            "covered_tokens": covered_tokens,
            "coverage_pct": round(100 * covered_tokens / n, 2) if n else 0.0,
            "content_tokens": content_total,
            "content_resolved": content_resolved,
            "oov_occurrences": oov_occurrences,
            "content_coverage_pct": (round(100 * content_resolved / content_total, 2)
                                     if content_total else 0.0),
            "unique_descriptors": len(concept),
            "oov_unique": len(oov),
            "domain_histogram": dict(dom_hist.most_common()),
            "library_histogram": dict(lib_hist),
            "via_histogram": dict(Counter(d["via"] for d in descriptors)),
        }
        return {
            "source": source_meta or {},
            "lexicons": self.ix.stats(),
            "config": {"use_lemma": self.use_lemma,
                       "use_multiword": self.use_multiword,
                       "lib_priority": self.lib_priority},
            "stats": stats,
            "descriptors": descriptors,
            "concept_index": concept,
            "ptr_graph": ptr_graph,
            "oov_top": [{"term": t, "count": c} for t, c in oov.most_common(50)],
        }


def coverage_only(index, text, **flags):
    """Lightweight: return just the stats block (used for the iteration table)."""
    enc = Encoder(index, **flags)
    return enc.encode(text)["stats"]
