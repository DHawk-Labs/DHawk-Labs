"""
lexicon_index.py — fast resolver over one or more lexicon-1.0 dictionaries.

Loads the generated WordDict / SciMedDict libraries and builds the indexes a
text encoder needs:

  * word     -> [entry, ...]          (a word may exist in several domains/libs)
  * addr     -> entry                 (for pointer-graph lookups)
  * phrase   -> entry                 (multiword 'underscore_separated' keys,
                                        indexed by first token for longest-match)

An *entry* here is a small dict: {lib, key, word, addr, dom, shell, theta,
kappa}. The original full entries (with pointers/curation) stay in ``by_addr``.

This module does not modify the dictionaries in any way; it only reads them.
"""

from __future__ import annotations

import json
from collections import defaultdict


class LexiconIndex:
    def __init__(self):
        self.by_word = defaultdict(list)     # word_lc -> [entry]
        self.by_addr = {}                    # (lib, addr) -> full entry
        self.phrase_by_first = defaultdict(list)  # first_tok -> [(tokens, entry)]
        self.libs = []
        self._max_phrase_len = 1

    def load(self, path, lib_name=None):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        lib = lib_name or doc.get("library", path)
        self.libs.append(lib)
        for key, e in doc["entries"].items():
            word = e["word"]
            slim = {
                "lib": lib, "key": key, "word": word, "addr": e["addr"],
                "dom": e["dom"], "shell": e["shell"], "theta": e["theta"],
                "kappa": e["kappa"],
            }
            self.by_word[word].append(slim)
            full = dict(e)
            full["lib"] = lib
            full["key"] = key
            # addresses are only unique WITHIN a library (each dictionary
            # assigns shell@theta independently), so key by (lib, addr).
            self.by_addr[(lib, e["addr"])] = full
            if "_" in word:
                toks = tuple(word.split("_"))
                self.phrase_by_first[toks[0]].append((toks, slim))
                self._max_phrase_len = max(self._max_phrase_len, len(toks))
        # longest phrases first, so greedy matching prefers the longest term
        for first in self.phrase_by_first:
            self.phrase_by_first[first].sort(key=lambda t: -len(t[0]))
        return self

    @property
    def max_phrase_len(self):
        return self._max_phrase_len

    def lookup_word(self, word):
        """Return entries for an exact lowercase word/phrase, or []."""
        return self.by_word.get(word, [])

    def match_phrase(self, tokens, start):
        """Longest multiword entry starting at tokens[start], or (entry, span).

        Returns (entry, n_tokens) for the longest matching phrase, else None.
        """
        first = tokens[start]
        cands = self.phrase_by_first.get(first)
        if not cands:
            return None
        for toks, entry in cands:               # already longest-first
            n = len(toks)
            if start + n <= len(tokens) and tuple(tokens[start:start + n]) == toks:
                return entry, n
        return None

    def stats(self):
        return {
            "libraries": self.libs,
            "unique_words": len(self.by_word),
            "addresses": len(self.by_addr),
            "multiword_first_tokens": len(self.phrase_by_first),
            "max_phrase_len": self._max_phrase_len,
        }
