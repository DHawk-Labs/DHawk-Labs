#!/usr/bin/env python3
"""
fetch_sources.py — download the offline data sources the builder depends on.

* WordNet + OMW are pulled via nltk (Princeton WordNet 3.0).
* Moby Thesaurus II (public domain) is downloaded to data/mthesaur.txt.

The generated dictionary (libs/word_dict_75k_lib.json) is committed, but the
raw third-party corpora are not — run this once to reproduce a build.
"""

from __future__ import annotations

import os
import sys
import urllib.request

MOBY_URL = "https://www.gutenberg.org/files/3202/files/mthesaur.txt"
MOBY_PATH = "data/mthesaur.txt"


def fetch_wordnet():
    import nltk
    for pkg in ("wordnet", "omw-1.4"):
        print(f"nltk: downloading {pkg} ...", flush=True)
        nltk.download(pkg, quiet=True)
    from nltk.corpus import wordnet as wn
    print(f"nltk: WordNet ready ({len(list(wn.all_lemma_names()))} lemmas)")


def fetch_moby(dest=MOBY_PATH):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
        print(f"moby: already present at {dest}")
        return
    print(f"moby: downloading -> {dest} ...", flush=True)
    req = urllib.request.Request(MOBY_URL, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as fh:
        fh.write(r.read())
    print(f"moby: {os.path.getsize(dest)} bytes")


if __name__ == "__main__":
    try:
        fetch_wordnet()
        fetch_moby()
        print("\nAll sources ready. Now run:")
        print("  python tools/build_lexicon.py --max-entries 120000 --minify")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
