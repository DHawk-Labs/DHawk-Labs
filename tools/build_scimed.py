#!/usr/bin/env python3
"""
build_scimed.py — scientific & medical lexicon-1.0 builder.

Same schema, geometry, curation and validation as the general WordDict builder,
but the vocabulary comes from MeSH (Medical Subject Headings, NLM, public
domain) instead of WordNet. MeSH supplies:

  * terms          — descriptor headings + entry-term synonyms
  * synonyms        -> ptr
  * tree numbers    -> hierarchy: broader term -> ptr, sibling terms -> orthogonal
  * categories      -> the 19 schema domains (chemicals->NOUNM, diseases->NOUNA,
                       anatomy/organisms->NOUNC, techniques/disciplines->JARGON,
                       geographicals->NOUNP, publication types->META)

Output is identical in shape to word_dict_75k_lib.json and is checked by the
same independent validator (validate_lexicon.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mesh_ingest                                   # noqa: E402
from build_lexicon import (DOMAINS, assign_geometry,  # noqa: E402
                           kappa_signature)

# MeSH top-level category -> schema domain
CAT_DOMAIN = {
    "A": "NOUNC",   # Anatomy (concrete structures)
    "B": "NOUNC",   # Organisms
    "C": "NOUNA",   # Diseases (abstract conditions)
    "D": "NOUNM",   # Chemicals and Drugs (substances)
    "E": "JARGON",  # Techniques and Equipment
    "F": "NOUNA",   # Psychiatry and Psychology
    "G": "NOUNA",   # Phenomena and Processes
    "H": "JARGON",  # Disciplines and Occupations
    "I": "NOUNA",   # Anthropology, Education, Sociology
    "J": "JARGON",  # Technology, Industry, Agriculture
    "K": "NOUNA",   # Humanities
    "L": "JARGON",  # Information Science
    "M": "NOUNC",   # Named Groups (of people)
    "N": "JARGON",  # Health Care
    "V": "META",    # Publication Characteristics
    "Z": "NOUNP",   # Geographicals
}

# Domains whose POS genuinely cannot take an antonym — must match the set the
# validator enforces for oppositional_status='not_applicable' (proper/mass
# nouns, numerals, publication meta, ...). NOUNC/NOUNA/JARGON *can* host an
# antonym in principle, so their scientific terms (which have none) are marked
# 'empty', not 'not_applicable'.
NO_ANTONYM_DOMS = {"NOUNM", "NOUNP", "META", "NUM", "PRON", "DET", "CONJ",
                   "AUX", "PRIME"}


def _normkey(term):
    """Collapse trivial variants (hyphen/underscore/apostrophe) for dedupe."""
    return term.replace("-", "").replace("_", "").replace("'", "")


def primary_category(cats):
    """Deterministic primary category for a descriptor."""
    if not cats:
        return None
    from collections import Counter
    c = Counter(cats)
    # most frequent, tie-broken by canonical category order
    return min(c, key=lambda k: (-c[k], list(mesh_ingest.MESH_CATEGORIES).index(k)))


def build(args):
    print("[1/6] INGEST  MeSH descriptors ...", flush=True)
    descriptors = list(mesh_ingest.parse(args.mesh))
    print(f"        descriptors: {len(descriptors)}", flush=True)

    # tree number -> owning descriptor term (tree numbers are unique to a term)
    # and parent tree -> [(child tree, term), ...] for O(children) sibling lookup
    tree2term = {}
    parent_children = defaultdict(list)
    for d in descriptors:
        if not d["term"]:
            continue
        for t in d["trees"]:
            tree2term[t] = d["term"]
            if "." in t:
                parent_children[t.rsplit(".", 1)[0]].append((t, d["term"]))

    # assemble unique entries (descriptor terms + synonym terms), deduped by
    # normalized key; a descriptor form always wins over a synonym form.
    entries_by_key = {}        # normkey -> entry dict
    term_to_key = {}           # exact term -> normkey  (resolution index)

    def register(term, dom, owner, role):
        nk = _normkey(term)
        if nk in entries_by_key:
            term_to_key.setdefault(term, nk)
            return
        entries_by_key[nk] = {
            "word": term, "dom": dom, "_owner": owner, "_role": role,
            "_pos_group": dom.lower(),
        }
        term_to_key[term] = nk

    for d in descriptors:
        if not d["term"]:
            continue
        cat = primary_category(d["categories"])
        dom = CAT_DOMAIN.get(cat, "JARGON")
        register(d["term"], dom, d["term"], "descriptor")
        for s in d["synonyms"]:
            register(s, dom, d["term"], "synonym")

    if args.max_entries and len(entries_by_key) > args.max_entries:
        # keep descriptors first, then synonyms, deterministic by term
        items = list(entries_by_key.values())
        items.sort(key=lambda e: (0 if e["_role"] == "descriptor" else 1,
                                  e["word"]))
        items = items[:args.max_entries]
        keep = {_normkey(e["word"]) for e in items}
        entries_by_key = {k: v for k, v in entries_by_key.items() if k in keep}
        term_to_key = {t: k for t, k in term_to_key.items() if k in entries_by_key}
        print(f"        capped to: {len(entries_by_key)}", flush=True)

    # group by domain for geometry
    entries_by_domain = defaultdict(list)
    for e in entries_by_key.values():
        e["_key"] = f"{e['dom']}.{e['word']}"
        entries_by_domain[e["dom"]].append(e)
    total = sum(len(v) for v in entries_by_domain.values())
    print(f"        unique entries: {total}", flush=True)

    print("[2/6] PLACE   assigning geometry ...", flush=True)
    assign_geometry(entries_by_domain)
    all_entries = [e for v in entries_by_domain.values() for e in v]

    # resolution index: term -> addr
    nk_to_addr = {_normkey(e["word"]): e["addr"] for e in all_entries}

    def resolve(term, self_addr):
        a = nk_to_addr.get(_normkey(term))
        return a if (a and a != self_addr) else None

    # descriptor term -> its full record (for synonyms / trees)
    desc_by_term = {d["term"]: d for d in descriptors if d["term"]}

    print("[3/6] LINK    resolving MeSH hierarchy pointers ...", flush=True)
    for e in all_entries:
        self_addr = e["addr"]
        owner = desc_by_term.get(e["_owner"])
        ptr, orth = [], []
        if owner:
            # synonyms of the owning descriptor (closest neighbours)
            fam = [owner["term"]] + owner["synonyms"]
            for term in fam:
                if len(ptr) >= 8:
                    break
                a = resolve(term, self_addr)
                if a and a not in ptr:
                    ptr.append(a)
            # broader terms (tree parents) -> ptr ; siblings -> orthogonal
            for t in owner["trees"]:
                parent = t.rsplit(".", 1)[0] if "." in t else None
                if parent and parent in tree2term:
                    a = resolve(tree2term[parent], self_addr)
                    if a and a not in ptr and len(ptr) < 8:
                        ptr.append(a)
                    # siblings: other trees sharing this parent prefix
                    for tn, term in parent_children.get(parent, ()):
                        if len(orth) >= 5:
                            break
                        if tn != t:
                            a = resolve(term, self_addr)
                            if a and a not in orth and a not in ptr:
                                orth.append(a)
        e["ptr"] = ptr[:8]
        e["ptr_orthogonal"] = orth[:5]
        e["ptr_oppositional"] = ""

    print("[4/6] CURATE  computing curation fields ...", flush=True)
    for e in all_entries:
        _curate(e)

    print("[5/6] REPORT  coverage ...", flush=True)
    _coverage(all_entries)

    print("[6/6] EMIT    writing JSON ...", flush=True)
    doc = _assemble(all_entries, args)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, sort_keys=True,
                  separators=(",", ":") if args.minify else (",", ": "),
                  indent=None if args.minify else 1)
    print(f"        wrote {len(all_entries)} entries -> {args.out} "
          f"({os.path.getsize(args.out)/1_048_576:.1f} MiB)", flush=True)
    return doc


def _curate(e):
    n_ptr = len(e["ptr"])
    n_orth = len(e["ptr_orthogonal"])
    orth_status = ("filled" if n_orth >= 3 else
                   "partial" if n_orth >= 1 else "empty")
    # scientific/medical terms do not take antonyms
    opp_status = "not_applicable" if e["dom"] in NO_ANTONYM_DOMS else "empty"
    def_strength = ("central" if n_ptr >= 3 else
                    "partial" if n_ptr >= 1 else "weak")
    sources = ["mesh"] if e["_role"] == "descriptor" else ["mesh", "mesh_entry"]
    score = round(0.6 * min(n_ptr / 4.0, 1.0) + 0.4 * min(n_orth / 3.0, 1.0), 4)
    e["curation"] = {
        "definition_strength": def_strength,
        "orthogonal_status": orth_status,
        "oppositional_status": opp_status,
        "needs_curation": bool(def_strength == "weak" or score < 0.5),
        "auto_sources": sources,
        "sufficiency_score": score,
    }


def _coverage(entries):
    from collections import Counter
    dom = Counter(e["dom"] for e in entries)
    p1 = sum(1 for e in entries if e["ptr"])
    p3 = sum(1 for e in entries if len(e["ptr"]) >= 3)
    o3 = sum(1 for e in entries if len(e["ptr_orthogonal"]) >= 3)
    n = len(entries)
    print("        domain distribution:")
    for d, k in dom.most_common():
        print(f"          {d:7s} {k}")
    print(f"        ptr>=1 {100*p1/n:.0f}%  ptr>=3 {100*p3/n:.0f}%  "
          f"orth>=3 {100*o3/n:.0f}%")


def _assemble(all_entries, args):
    entries = {}
    for e in all_entries:
        entries[e["_key"]] = {
            "word": e["word"], "shell": e["shell"], "theta": e["theta"],
            "kappa": e["kappa"], "dom": e["dom"], "addr": e["addr"],
            "ptr": e["ptr"], "ptr_orthogonal": e["ptr_orthogonal"],
            "ptr_oppositional": e["ptr_oppositional"], "curation": e["curation"],
        }
    domains_out = {d: {"description": v["description"], "shell": v["shell"]}
                   for d, v in DOMAINS.items()}
    return {
        "library": args.library, "version": args.version,
        "schema": "lexicon-1.0", "kind": "lexicon", "trust_level": "absorbed",
        "domains": domains_out,
        "descriptor_protocol": {"format": "underscore_separated",
                                "case": "lowercase", "stopwords": [],
                                "min_length": 1},
        "entries": entries,
        "exports": ["by_word", "by_address", "ptr_graph"],
        "curation_pass_version": args.version,
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="libs/scimed_dict_lib.json")
    p.add_argument("--mesh", default="data/mesh_d2025.bin")
    p.add_argument("--library", default="SciMedDict")
    p.add_argument("--version", default="1.0.0")
    p.add_argument("--max-entries", type=int, default=0)
    p.add_argument("--minify", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    build(parse_args())
