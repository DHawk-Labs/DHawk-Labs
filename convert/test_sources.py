#!/usr/bin/env python3
"""
test_sources.py — exercise the converter across source types and measure how
each method iteration improves coverage.

For every source it (1) extracts clean text, then (2) encodes it three ways to
show the effect of each added method:

    exact         lowercase surface match only
    +lemma        add WordNet lemmatisation
    +multiword    add greedy longest-match of multiword terms   (full pipeline)

It prints a per-source table and writes the full encoding of the best config to
convert/out/<name>.lex.json.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract                                    # noqa: E402
from lexicon_index import LexiconIndex            # noqa: E402
from encode import Encoder                        # noqa: E402

LEX = ["libs/scimed_dict_lib.json", "libs/word_dict_75k_lib.json"]

SOURCES = [
    ("text",  "convert/samples/mixed_prose.txt"),
    ("html",  "/tmp/photosynthesis.html"),
    ("html",  "/tmp/mi.html"),
    ("pdf",   "/tmp/attention.pdf"),
]

CONFIGS = [
    ("exact",      dict(use_lemma=False, use_multiword=False, use_decompose=False)),
    ("+lemma",     dict(use_lemma=True,  use_multiword=False, use_decompose=False)),
    ("+multiword", dict(use_lemma=True,  use_multiword=True,  use_decompose=False)),
    ("+hyphen",    dict(use_lemma=True,  use_multiword=True,  use_decompose=True)),
]


def run():
    os.makedirs("convert/out", exist_ok=True)
    t = time.time()
    ix = LexiconIndex()
    for p in LEX:
        ix.load(p)
    print(f"loaded {ix.stats()['unique_words']} words from {ix.libs} "
          f"in {time.time()-t:.1f}s\n")

    header = (f"{'source':28s} {'tokens':>7s} "
             f"{'exact':>7s} {'+lemma':>7s} {'+mw':>7s} {'+hyph':>7s}  "
             f"{'content%':>8s} {'OOV':>6s}")
    print(header)
    print("-" * len(header))

    for kind, src in SOURCES:
        if not os.path.exists(src) and kind != "url":
            print(f"{os.path.basename(src):28s}  (missing, skipped)")
            continue
        try:
            doc = extract.extract(src, kind=kind)
        except Exception as e:
            print(f"{os.path.basename(src):28s}  EXTRACT ERROR: {e}")
            continue
        text = doc["text"]
        covs = []
        best = None
        for name, flags in CONFIGS:
            res = Encoder(ix, **flags).encode(text, doc["meta"])
            covs.append(res["stats"]["coverage_pct"])
            best = res
        s = best["stats"]
        label = (doc["meta"].get("title") or os.path.basename(src))[:27]
        print(f"{label:28s} {s['word_tokens']:7d} "
              f"{covs[0]:6.1f}% {covs[1]:6.1f}% {covs[2]:6.1f}% {covs[3]:6.1f}%  "
              f"{s['content_coverage_pct']:7.1f}% {s['oov_unique']:6d}")
        name = os.path.splitext(os.path.basename(src))[0]
        with open(f"convert/out/{name}.lex.json", "w", encoding="utf-8") as fh:
            json.dump(best, fh, ensure_ascii=False, indent=1)
        # show what each source pulled from which library + top OOV
        print(f"   libs={s['library_histogram']} "
              f"via={s['via_histogram']}")
        print(f"   top OOV: "
              f"{', '.join(o['term'] for o in best['oov_top'][:12])}")
    print("\nfull encodings written to convert/out/*.lex.json")


if __name__ == "__main__":
    run()
