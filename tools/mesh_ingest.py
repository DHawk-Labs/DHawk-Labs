"""
mesh_ingest.py — parse the MeSH ASCII descriptor file (d20XX.bin).

MeSH (Medical Subject Headings, U.S. National Library of Medicine, public
domain) is to scientific/medical vocabulary what WordNet is to general English:
an authoritative, structured lexicon with terms, synonyms (entry terms), a
hierarchy (tree numbers) and subject categories.

Record format (fields are ``KEY = value``, records split on ``*NEWRECORD``):
    MH = Myocardial Infarction          main heading (the canonical term)
    ENTRY = Heart Attack|T047|...       entry term (synonym); take text before '|'
    MN = C14.280.647.500                tree number (category = first letter)
    MS = ...                            scope note (definition)
    UI = D009203                        unique id

This module yields cleaned descriptor records; domain mapping, geometry,
pointers and curation are handled by build_scimed.py.
"""

from __future__ import annotations

import re

# MeSH top-level categories -> human label (for documentation / domains table)
MESH_CATEGORIES = {
    "A": "Anatomy", "B": "Organisms", "C": "Diseases",
    "D": "Chemicals and Drugs",
    "E": "Analytical, Diagnostic and Therapeutic Techniques and Equipment",
    "F": "Psychiatry and Psychology", "G": "Phenomena and Processes",
    "H": "Disciplines and Occupations",
    "I": "Anthropology, Education, Sociology and Social Phenomena",
    "J": "Technology, Industry and Agriculture", "K": "Humanities",
    "L": "Information Science", "M": "Named Groups", "N": "Health Care",
    "V": "Publication Characteristics", "Z": "Geographicals",
}

# Accept letters, digits, internal hyphen/apostrophe; tokens joined by '_'.
# Scientific terms legitimately contain digits (B12, interleukin-6, covid-19),
# so — unlike the general dictionary — digits are allowed.
_TOKEN = re.compile(r"^[a-z0-9]+(?:[-'][a-z0-9]+)*$")


def clean_term(raw, max_tokens=5, max_len=40):
    """Normalise a MeSH term to a canonical form, or return None if it is noise.

    Rejects IUPAC-style names and anything with structural punctuation
    (commas, parentheses, brackets, slashes, +, etc.), over-long strings, and
    tokens that are not alphanumeric words. Spaces become underscores.
    """
    if not raw:
        return None
    s = raw.strip().lower()
    # kill structural punctuation that marks chemical formulae / noise
    if any(c in s for c in ",()[]{}/\\+%;:*@#=<>"):
        return None
    s = s.replace(" ", "_")
    s = re.sub(r"_+", "_", s).strip("_-")
    if not s or len(s) > max_len:
        return None
    parts = s.split("_")
    if len(parts) > max_tokens:
        return None
    if not all(_TOKEN.match(p) for p in parts):
        return None
    if not any(c.isalpha() for c in s):   # must contain a letter
        return None
    return s


def parse(path):
    """Yield descriptor dicts: {term, ui, synonyms, trees, categories, scope}."""
    rec = None
    with open(path, encoding="latin-1") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line == "*NEWRECORD":
                if rec and rec.get("term"):
                    yield _finish(rec)
                rec = {"term": None, "ui": None, "syn_raw": [], "trees": [],
                       "scope": ""}
                continue
            if rec is None or " = " not in line:
                continue
            key, _, val = line.partition(" = ")
            key = key.strip()
            if key == "MH":
                rec["term_raw"] = val.strip()
                rec["term"] = clean_term(val)
            elif key in ("ENTRY", "PRINT ENTRY"):
                rec["syn_raw"].append(val.split("|", 1)[0])
            elif key == "MN":
                rec["trees"].append(val.strip())
            elif key == "MS":
                rec["scope"] = val.strip()
            elif key == "UI":
                rec["ui"] = val.strip()
        if rec and rec.get("term"):
            yield _finish(rec)


def _finish(rec):
    syns = []
    seen = {rec["term"]}
    for raw in rec["syn_raw"]:
        c = clean_term(raw)
        if c and c not in seen:
            seen.add(c)
            syns.append(c)
    cats = []
    for t in rec["trees"]:
        if t and t[0] in MESH_CATEGORIES:
            cats.append(t[0])
    return {
        "term": rec["term"],
        "ui": rec["ui"],
        "synonyms": syns,
        "trees": rec["trees"],
        "categories": cats,
        "scope": rec["scope"],
    }


if __name__ == "__main__":
    import sys
    from collections import Counter
    path = sys.argv[1] if len(sys.argv) > 1 else "data/mesh_d2025.bin"
    n = 0
    syn_total = 0
    cat = Counter()
    samp = []
    for r in parse(path):
        n += 1
        syn_total += len(r["synonyms"])
        if r["categories"]:
            cat[r["categories"][0]] += 1
        if len(samp) < 8:
            samp.append((r["term"], r["categories"][:1], r["synonyms"][:4]))
    print(f"descriptors (clean term): {n}")
    print(f"total clean synonyms:     {syn_total}")
    print("primary-category counts:")
    for c, k in cat.most_common():
        print(f"  {c} {MESH_CATEGORIES[c]:55s} {k}")
    print("samples:")
    for t, c, s in samp:
        print(f"  {t:30s} {c}  syn={s}")
