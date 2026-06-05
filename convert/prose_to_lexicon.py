#!/usr/bin/env python3
"""
prose_to_lexicon.py — CLI: unstructured source -> structured lexicon encoding.

    python convert/prose_to_lexicon.py <source> [--out out.json] \
        [--kind auto|url|pdf|html|text] \
        [--lex libs/scimed_dict_lib.json libs/word_dict_75k_lib.json] \
        [--no-lemma] [--no-multiword]

<source> may be a URL, a PDF, an HTML file, or a plain-text/markdown file.
Library order is the resolution priority (first listed wins on ties), so put a
domain dictionary first when encoding domain text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract                                    # noqa: E402
from lexicon_index import LexiconIndex            # noqa: E402
from encode import Encoder                        # noqa: E402


def load_lexicons(paths):
    ix = LexiconIndex()
    for p in paths:
        ix.load(p)
    return ix


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source")
    ap.add_argument("--out")
    ap.add_argument("--kind", default="auto",
                    choices=["auto", "url", "pdf", "html", "text"])
    ap.add_argument("--lex", nargs="+",
                    default=["libs/scimed_dict_lib.json",
                             "libs/word_dict_75k_lib.json"])
    ap.add_argument("--no-lemma", action="store_true")
    ap.add_argument("--no-multiword", action="store_true")
    ap.add_argument("--full", action="store_true",
                    help="include the full descriptor stream in the output")
    args = ap.parse_args(argv)

    print(f"loading lexicons: {args.lex}", file=sys.stderr)
    ix = load_lexicons(args.lex)
    print(f"  {ix.stats()}", file=sys.stderr)

    print(f"extracting: {args.source} ({args.kind})", file=sys.stderr)
    doc = extract.extract(args.source, kind=args.kind)
    print(f"  extracted {len(doc['text'])} chars", file=sys.stderr)

    enc = Encoder(ix, use_lemma=not args.no_lemma,
                  use_multiword=not args.no_multiword)
    result = enc.encode(doc["text"], source_meta=doc["meta"])

    if not args.full:
        # keep output compact: drop the per-token stream, keep the summary
        result.pop("descriptors", None)

    out = args.out or (os.path.splitext(os.path.basename(
        args.source.rstrip("/")))[0] or "encoded") + ".lex.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    s = result["stats"]
    print(f"\n== {doc['meta'].get('title') or args.source} ==")
    print(f"  word tokens        {s['word_tokens']}")
    print(f"  resolved           {s['resolved_tokens']}  "
          f"({s['coverage_pct']}%)")
    print(f"  content coverage   {s['content_coverage_pct']}%")
    print(f"  unique descriptors {s['unique_descriptors']}")
    print(f"  by library         {s['library_histogram']}")
    print(f"  by match method    {s['via_histogram']}")
    print(f"  OOV (unique)       {s['oov_unique']}")
    print(f"  -> {out}")
    return result


if __name__ == "__main__":
    main()
