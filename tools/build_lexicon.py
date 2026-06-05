#!/usr/bin/env python3
"""
build_lexicon.py — deterministic generator for the WordDict ``lexicon-1.0``
library described in ``word_dict_structure.json``.

Pipeline
--------
1.  INGEST    Pull every clean lemma from WordNet (dictionary #1) and the
              curated closed-class seeds. Classify each (word, POS) into one of
              the 19 schema domains using WordNet lexicographer files.
2.  PLACE     Assign every entry geometric coordinates: ``shell`` (from the
              domain), a unique ``theta`` within the shell, a deterministic
              ``kappa`` signature, and the derived ``addr = "<shell>@<theta>"``.
3.  LINK      Two-pass pointer resolution. Build word/address indexes, then for
              every entry resolve ``ptr`` (synonyms / closest neighbours),
              ``ptr_orthogonal`` (different-axis relatives) and
              ``ptr_oppositional`` (single antonym) to *existing* addresses.
4.  ENRICH    Optional bounded, cached pass over Datamuse (dictionary #2) to
              fill antonyms / related words WordNet left empty.
5.  CURATE    Compute every ``curation.*`` field so the published invariants
              hold *by construction*.
6.  EMIT      Write the schema-shaped JSON, then hand off to validate_lexicon.

The build is fully deterministic given a fixed WordNet version and Datamuse
cache: lemmas are processed in sorted order and all tie-breaks are explicit.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import closed_class  # noqa: E402

# ---------------------------------------------------------------------------
# Domain table — mirrors domains_table in word_dict_structure.json
# ---------------------------------------------------------------------------
DOMAINS = {
    "PRON":   {"shell": 1,  "description": "pronouns"},
    "DET":    {"shell": 2,  "description": "determiners / articles"},
    "NUM":    {"shell": 3,  "description": "numerals (cardinal & ordinal)"},
    "PREP":   {"shell": 4,  "description": "prepositions"},
    "CONJ":   {"shell": 5,  "description": "conjunctions"},
    "AUX":    {"shell": 6,  "description": "auxiliary & modal verbs"},
    "NOUNC":  {"shell": 7,  "description": "concrete / count nouns"},
    "NOUNM":  {"shell": 8,  "description": "mass / substance nouns"},
    "NOUNA":  {"shell": 9,  "description": "abstract nouns"},
    "NOUNP":  {"shell": 10, "description": "proper nouns"},
    "VERBA":  {"shell": 11, "description": "action / dynamic verbs"},
    "VERBS":  {"shell": 12, "description": "stative / cognitive verbs"},
    "ADJ":    {"shell": 13, "description": "adjectives"},
    "ADV":    {"shell": 14, "description": "adverbs"},
    "INTERJ": {"shell": 15, "description": "interjections"},
    "SLANG":  {"shell": 16, "description": "slang"},
    "JARGON": {"shell": 17, "description": "domain jargon"},
    "META":   {"shell": 18, "description": "meta / structural tokens"},
    "PRIME":  {"shell": 19, "description": "primitive seed concepts"},
}

# Classes whose part-of-speech does not admit an antonym (schema invariant).
NO_ANTONYM_DOMS = {
    "NOUNP", "NOUNM", "NUM", "PRON", "DET", "CONJ", "AUX", "META", "PRIME",
}

# WordNet noun lexicographer files -> domain bucket.
MASS_LEXNAMES = {"noun.substance", "noun.food"}
ABSTRACT_LEXNAMES = {
    "noun.act", "noun.attribute", "noun.cognition", "noun.communication",
    "noun.event", "noun.feeling", "noun.motive", "noun.phenomenon",
    "noun.process", "noun.quantity", "noun.relation", "noun.state",
    "noun.time", "noun.Tops",
}
# Everything else noun.* -> NOUNC (concrete/count): animal, artifact, body,
# group, location, object, person, plant, possession, shape.

# WordNet verb lexicographer files that are stative / cognitive -> VERBS.
STATIVE_VERB_LEXNAMES = {
    "verb.stative", "verb.cognition", "verb.emotion", "verb.perception",
}

# ---------------------------------------------------------------------------
# Cleanliness filters — reject noise/garbage outright
# ---------------------------------------------------------------------------
_SINGLE_RE = re.compile(r"^[a-z]+(?:[-'][a-z]+)*$")
_TOKEN_RE = re.compile(r"^[a-z]+(?:[-'][a-z]+)*$")
_SINGLE_LETTER_OK = {"a", "i", "o"}  # the only standalone single letters allowed


_ROMAN_RE = re.compile(
    r"^m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")


def is_roman_numeral(word: str) -> bool:
    """True iff *word* is a well-formed Roman numeral (e.g. 'ix', 'lxxxv')."""
    return len(word) >= 1 and bool(_ROMAN_RE.match(word))


# Tokens that look like Roman numerals but are genuine English words or
# standard abbreviations (intravenous, compact disc, centimetre, the vi editor,
# the Greek letter xi, ...). These are kept; every other valid Roman numeral is
# rejected as noise. A frequency cutoff cannot be used because pure numerals
# like 'ii'/'iii' are *more* frequent in corpora than real abbreviations.
_ROMAN_KEEP = {
    "mix", "cd", "cv", "iv", "cm", "mm", "cc", "ml", "dc", "dl", "md", "mi",
    "vi", "xi",
}


def is_noise(word: str) -> bool:
    """Reject obvious garbage that WordNet carries as lemmas.

    The clear-cut case is Roman numerals (WordNet stores i..M as numerals /
    adjectives). The strict Roman grammar in ``is_roman_numeral`` does not match
    ordinary words such as 'did', 'mild', 'dim' or 'lid', so rejecting every
    matched token except an explicit keep-list removes 'ii', 'iii', 'viii',
    'xiv', 'lxxxv', ... while preserving real words/abbreviations.
    """
    if is_roman_numeral(word) and word not in _ROMAN_KEEP:
        return True
    return False


def clean_single(word: str) -> bool:
    """True iff *word* is an acceptable single-token canonical form."""
    if len(word) == 1:
        return word in _SINGLE_LETTER_OK
    if not _SINGLE_RE.match(word):
        return False
    if "--" in word or "''" in word:
        return False
    if is_noise(word):
        return False
    return True


def clean_multi(word: str, max_tokens: int) -> bool:
    """True iff *word* is an acceptable underscore-joined multiword form."""
    parts = word.split("_")
    if not (2 <= len(parts) <= max_tokens):
        return False
    for p in parts:
        if len(p) == 1:
            if p not in _SINGLE_LETTER_OK:
                return False
        elif not _TOKEN_RE.match(p):
            return False
    return True


# ---------------------------------------------------------------------------
# Stage 1 — INGEST
# ---------------------------------------------------------------------------
class Sense:
    """One WordNet sense attached to a (word, pos) key."""

    __slots__ = ("count", "lexname", "synset", "raw_name", "is_instance")

    def __init__(self, count, lexname, synset, raw_name, is_instance):
        self.count = count
        self.lexname = lexname
        self.synset = synset
        self.raw_name = raw_name
        self.is_instance = is_instance


def ingest_wordnet(include_multi: bool, max_tokens: int):
    """Return ``{(word_lc, pos_group): [Sense, ...]}`` for clean lemmas.

    ``pos_group`` collapses WordNet's adjective-satellite ('s') into 's'->'a'.
    """
    from nltk.corpus import wordnet as wn

    senses = defaultdict(list)
    for syn in wn.all_synsets():
        pos = syn.pos()
        pg = "a" if pos in ("a", "s") else pos
        lexname = syn.lexname()
        # instance hyponyms mark named entities (proper nouns)
        instance_targets = set()
        for inst in syn.instance_hyponyms():
            instance_targets.add(inst.name())
        for lem in syn.lemmas():
            raw = lem.name()
            low = raw.lower()
            if "_" in raw:
                if not include_multi or not clean_multi(low, max_tokens):
                    continue
            else:
                if not clean_single(low):
                    continue
            is_instance = bool(syn.instance_hypernyms())
            try:
                cnt = lem.count()
            except Exception:
                cnt = 0
            senses[(low, pg)].append(
                Sense(cnt, lexname, syn, raw, is_instance)
            )
    return senses


def choose_domain(word_lc, pg, sense_list):
    """Pick the schema domain for a (word, pos_group) from its senses.

    The representative sense is the most frequent one (highest SemCor count),
    tie-broken by synset name for determinism. Its lexname decides the bucket.
    """
    rep = max(sense_list, key=lambda s: (s.count, -_synset_rank(s.synset)))
    if pg == "r":
        return "ADV", rep
    if pg == "a":
        return "ADJ", rep
    if pg == "v":
        dom = "VERBS" if rep.lexname in STATIVE_VERB_LEXNAMES else "VERBA"
        return dom, rep
    # nouns
    # proper noun: any representative sense that is an instance, OR the raw
    # form is capitalized in WordNet (named entity).
    proper = any(s.is_instance or s.raw_name[:1].isupper() for s in sense_list)
    if proper and rep.raw_name[:1].isupper():
        return "NOUNP", rep
    if rep.lexname in MASS_LEXNAMES:
        return "NOUNM", rep
    if rep.lexname in ABSTRACT_LEXNAMES:
        return "NOUNA", rep
    return "NOUNC", rep


def _synset_rank(syn):
    """Stable integer for tie-breaking (lower = preferred)."""
    name = syn.name()
    try:
        return int(name.rsplit(".", 1)[1])
    except Exception:
        return 999


# ---------------------------------------------------------------------------
# Stage 2 — PLACE (geometry)
# ---------------------------------------------------------------------------
GOLDEN_ANGLE = 137.50776405003785  # degrees; golden-angle phase spacing


def kappa_signature(word_lc, shell):
    """Deterministic curvature / radial signature in a bounded float range.

    Combines the shell ring with a per-word fingerprint (length + vowel ratio
    + character checksum) so that the value is reproducible and stable.
    """
    letters = [c for c in word_lc if c.isalpha()]
    n = max(1, len(letters))
    vowels = sum(1 for c in letters if c in "aeiou")
    checksum = sum(ord(c) for c in letters) % 997
    radial = vowels / n                      # 0..1
    fingerprint = checksum / 997.0           # 0..1
    kappa = shell + 0.5 * radial + 0.25 * math.sin(fingerprint * math.pi)
    return round(kappa, 6)


def assign_geometry(entries_by_domain):
    """Mutate entries, filling shell/theta/kappa/addr with unique addresses.

    Within a shell, words are sorted canonically and spread around the circle
    with the golden angle, then nudged to guarantee a unique 4-decimal theta.
    """
    for dom, entries in entries_by_domain.items():
        shell = DOMAINS[dom]["shell"]
        entries.sort(key=lambda e: e["word"])
        used = set()
        m = len(entries)
        for i, e in enumerate(entries):
            base = (i * GOLDEN_ANGLE) % 360.0
            theta = round(base, 4)
            # guarantee uniqueness within the shell
            bump = 0
            while theta in used:
                bump += 1
                theta = round((base + bump * 0.0001) % 360.0, 4)
            used.add(theta)
            e["shell"] = shell
            e["theta"] = theta
            e["kappa"] = kappa_signature(e["word"], shell)
            e["addr"] = f"{shell}@{theta}"


# ---------------------------------------------------------------------------
# Stage 3 — LINK (pointer resolution)
# ---------------------------------------------------------------------------
def build_indexes(all_entries):
    """Return lookup indexes used to resolve pointers to addresses."""
    by_addr = {}
    word_pos_to_addr = {}      # (word_lc, pos_group) -> addr
    word_to_addrs = defaultdict(list)  # word_lc -> [addr, ...]
    for e in all_entries:
        by_addr[e["addr"]] = e
        word_to_addrs[e["word"]].append(e["addr"])
        pg = e["_pos_group"]
        word_pos_to_addr.setdefault((e["word"], pg), e["addr"])
    return by_addr, word_pos_to_addr, word_to_addrs


def _resolve(word_lc, pg, self_addr, word_pos_to_addr, word_to_addrs):
    """Map a related word to an existing entry address (same POS preferred)."""
    addr = word_pos_to_addr.get((word_lc, pg))
    if addr and addr != self_addr:
        return addr
    for a in word_to_addrs.get(word_lc, ()):
        if a != self_addr:
            return a
    return None


_POS_OF_SYNSET = {"n": "n", "v": "v", "a": "a", "s": "a", "r": "r"}


def link_pointers(all_entries, word_pos_to_addr, word_to_addrs):
    """Fill ptr / ptr_orthogonal / ptr_oppositional from WordNet relations."""
    for e in all_entries:
        syn = e.get("_synset")
        word = e["word"]
        if syn is None:
            # closed-class entry: no WordNet relations
            e["ptr"], e["ptr_orthogonal"], e["ptr_oppositional"] = [], [], ""
            continue

        self_addr = e["addr"]
        ptr, orth = [], []
        opp = ""

        # --- ptr: synonyms (same synset), then adjective satellites, then
        #         hypernyms — all curated by WordNet, so high precision -------
        for lem in syn.lemmas():
            lw = lem.name().lower()
            if lw == word:
                continue
            a = _resolve(lw, _POS_OF_SYNSET[syn.pos()], self_addr,
                         word_pos_to_addr, word_to_addrs)
            if a and a not in ptr:
                ptr.append(a)
            if len(ptr) >= 8:
                break
        # adjectives have no hypernyms; their close synonyms live in the
        # similar_to cluster (beautiful -> pretty/lovely/gorgeous, ...)
        if syn.pos() in ("a", "s") and len(ptr) < 8:
            for sim in syn.similar_tos():
                for lem in sim.lemmas():
                    a = _resolve(lem.name().lower(), "a", self_addr,
                                 word_pos_to_addr, word_to_addrs)
                    if a and a not in ptr:
                        ptr.append(a)
                    if len(ptr) >= 8:
                        break
                if len(ptr) >= 8:
                    break
        if len(ptr) < 3:
            for hyper in syn.hypernyms():
                for lem in hyper.lemmas():
                    a = _resolve(lem.name().lower(), _POS_OF_SYNSET[hyper.pos()],
                                 self_addr, word_pos_to_addr, word_to_addrs)
                    if a and a not in ptr:
                        ptr.append(a)
                    if len(ptr) >= 8:
                        break
                if len(ptr) >= 8:
                    break

        # --- ptr_orthogonal: different-axis relatives -----------------------
        # derivationally related forms (cross-POS), also_sees, coordinate
        # sisters, holonyms — all "related but on a different axis".
        ortho_candidates = []
        for lem in syn.lemmas():
            for d in lem.derivationally_related_forms():
                ds = d.synset()
                ortho_candidates.append((d.name().lower(), _POS_OF_SYNSET[ds.pos()]))
        for rel in syn.also_sees():
            for lem in rel.lemmas():
                ortho_candidates.append((lem.name().lower(), _POS_OF_SYNSET[rel.pos()]))
        for rel in (syn.part_holonyms() + syn.member_holonyms()
                    + syn.substance_holonyms()):
            for lem in rel.lemmas():
                ortho_candidates.append((lem.name().lower(), _POS_OF_SYNSET[rel.pos()]))
        # coordinate sisters (share a direct hypernym)
        for hyper in syn.hypernyms():
            for sis in hyper.hyponyms():
                if sis == syn:
                    continue
                lem = sis.lemmas()[0]
                ortho_candidates.append((lem.name().lower(), _POS_OF_SYNSET[sis.pos()]))
        for w, pg in ortho_candidates:
            if len(orth) >= 5:
                break
            a = _resolve(w, pg, self_addr, word_pos_to_addr, word_to_addrs)
            if a and a not in orth and a not in ptr:
                orth.append(a)

        # --- ptr_oppositional: single antonym -------------------------------
        for lem in syn.lemmas():
            ants = lem.antonyms()
            if ants:
                a = _resolve(ants[0].name().lower(),
                             _POS_OF_SYNSET[ants[0].synset().pos()],
                             self_addr, word_pos_to_addr, word_to_addrs)
                if a:
                    opp = a
                    break

        e["ptr"] = ptr[:8]
        e["ptr_orthogonal"] = orth[:5]
        e["ptr_oppositional"] = opp


# ---------------------------------------------------------------------------
# Stage 5 — CURATE
# ---------------------------------------------------------------------------
def curate(e, enriched_addr_set):
    dom = e["dom"]
    n_ptr = len(e["ptr"])
    n_orth = len(e["ptr_orthogonal"])
    has_opp = e["ptr_oppositional"] != ""

    if n_orth >= 3:
        orth_status = "filled"
    elif n_orth >= 1:
        orth_status = "partial"
    else:
        orth_status = "empty"

    if has_opp:
        opp_status = "filled"
    elif dom in NO_ANTONYM_DOMS:
        opp_status = "not_applicable"
    else:
        opp_status = "empty"

    if n_ptr >= 3:
        def_strength = "central"
    elif n_ptr >= 1:
        def_strength = "partial"
    else:
        def_strength = "weak"

    sources = ["wordnet"] if e.get("_synset") is not None else ["closed_class"]
    if e["addr"] in enriched_addr_set:
        sources.append("datamuse")

    # sufficiency: weighted fill across the three pointer axes
    opp_component = 1.0 if (has_opp or opp_status == "not_applicable") else 0.0
    score = (
        0.50 * min(n_ptr / 4.0, 1.0)
        + 0.30 * min(n_orth / 3.0, 1.0)
        + 0.20 * opp_component
    )
    score = round(score, 4)

    needs = (def_strength == "weak") or (score < 0.5) or (
        opp_status == "empty"
    )

    e["curation"] = {
        "definition_strength": def_strength,
        "orthogonal_status": orth_status,
        "oppositional_status": opp_status,
        "needs_curation": bool(needs),
        "auto_sources": sources,
        "sufficiency_score": score,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build(args):
    print("[1/6] INGEST  WordNet lemmas ...", flush=True)
    senses = ingest_wordnet(include_multi=not args.no_multiword,
                            max_tokens=args.max_tokens)
    print(f"        clean (word,pos) keys: {len(senses)}", flush=True)

    entries_by_domain = defaultdict(list)
    seen_entry_keys = set()

    # WordNet-derived entries
    for (word_lc, pg), sense_list in senses.items():
        dom, rep = choose_domain(word_lc, pg, sense_list)
        key = f"{dom}.{word_lc}"
        if key in seen_entry_keys:
            continue
        seen_entry_keys.add(key)
        freq = max((s.count for s in sense_list), default=0)
        entries_by_domain[dom].append({
            "word": word_lc,
            "dom": dom,
            "_pos_group": pg,
            "_synset": rep.synset,
            "_key": key,
            "_freq": freq,
            "_nsense": len(sense_list),
        })

    # Closed-class seeds (function words). These never override an existing
    # content-word entry because they live in their own domains/shells.
    for dom, words in closed_class.all_closed_classes().items():
        for w in words:
            key = f"{dom}.{w}"
            if key in seen_entry_keys:
                continue
            seen_entry_keys.add(key)
            pg = {"PRON": "pron", "DET": "det", "NUM": "num", "PREP": "prep",
                  "CONJ": "conj", "AUX": "aux", "INTERJ": "interj"}[dom]
            entries_by_domain[dom].append({
                "word": w,
                "dom": dom,
                "_pos_group": pg,
                "_synset": None,
                "_key": key,
                "_freq": 0,
                "_nsense": 0,
            })

    total = sum(len(v) for v in entries_by_domain.values())
    print(f"        raw entries: {total}", flush=True)

    # Optional cap to land in the requested size band — keep the most common
    # words (highest WordNet sense count) per domain when trimming.
    if args.max_entries and total > args.max_entries:
        entries_by_domain = _trim_to(entries_by_domain, args.max_entries)
        total = sum(len(v) for v in entries_by_domain.values())
        print(f"        trimmed to: {total}", flush=True)

    print("[2/6] PLACE   assigning geometry ...", flush=True)
    assign_geometry(entries_by_domain)

    all_entries = [e for v in entries_by_domain.values() for e in v]

    print("[3/7] LINK    resolving pointers ...", flush=True)
    by_addr, word_pos_to_addr, word_to_addrs = build_indexes(all_entries)
    link_pointers(all_entries, word_pos_to_addr, word_to_addrs)

    enriched = set()

    # ---- Stage 4: OFFLINE enrichment (no budget, no network) -------------
    # This is the primary completeness pass: Moby for synonyms/related and
    # WordNet's full antonym closure. It runs by default; --no-offline skips.
    if not args.no_offline:
        print("[4/7] OFFLINE Moby + WordNet antonym closure ...", flush=True)
        import offline_enrich

        def resolve_same_pos(word_lc, pg, self_addr):
            a = word_pos_to_addr.get((word_lc, pg))
            return a if (a and a != self_addr) else None

        def resolve(word_lc, pg, self_addr):
            a = word_pos_to_addr.get((word_lc, pg))
            if a and a != self_addr:
                return a
            for cand in word_to_addrs.get(word_lc, ()):
                if cand != self_addr:
                    return cand
            return None

        moby = offline_enrich.load_moby(args.moby)
        print(f"        moby roots: {len(moby)} words", flush=True)
        m_changed = offline_enrich.apply_moby(
            all_entries, moby, resolve_same_pos, resolve)
        print(f"        moby filled ptr/orth on {len(m_changed)} entries",
              flush=True)

        entry_pos_set = {(e["word"], e["_pos_group"]) for e in all_entries}
        ant_map = offline_enrich.build_antonym_map(entry_pos_set)
        print(f"        antonym map: {len(ant_map)} curated pairs", flush=True)
        a_changed = offline_enrich.apply_antonyms(all_entries, ant_map, resolve)
        print(f"        antonyms filled on {len(a_changed)} entries", flush=True)
        enriched |= m_changed | a_changed
    else:
        print("[4/7] OFFLINE skipped (--no-offline)", flush=True)

    # ---- Stage 5: optional Datamuse polish (online, cached) --------------
    if args.enrich:
        print("[5/7] DATAMUSE polish pass ...", flush=True)
        import datamuse_enrich
        d_enriched = datamuse_enrich.run(
            all_entries, word_pos_to_addr, word_to_addrs,
            budget=args.enrich_budget, workers=args.enrich_workers,
            cache_path=args.cache,
        )
        enriched |= d_enriched
        print(f"        datamuse enriched: {len(d_enriched)} entries", flush=True)
    else:
        print("[5/7] DATAMUSE skipped (--enrich not set)", flush=True)

    print("[6/7] CURATE  computing curation fields ...", flush=True)
    for e in all_entries:
        curate(e, enriched)

    # completeness self-check
    try:
        import offline_enrich as _oe
        print(_oe.coverage_report(all_entries), flush=True)
    except Exception as _e:
        print(f"        (coverage report skipped: {_e})", flush=True)

    print("[7/7] EMIT    writing JSON ...", flush=True)
    doc = assemble_document(all_entries, args)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, sort_keys=True,
                  separators=(",", ":") if args.minify else (",", ": "),
                  indent=None if args.minify else 1)
    size = os.path.getsize(args.out)
    print(f"        wrote {len(all_entries)} entries -> {args.out} "
          f"({size/1_048_576:.1f} MiB)", flush=True)
    return doc


CLOSED_DOMS = ("PRON", "DET", "NUM", "PREP", "CONJ", "AUX", "INTERJ")


def _real_freq(word):
    """Real corpus frequency (Zipf 0..8) for a word, robust to multiword."""
    try:
        from wordfreq import zipf_frequency
    except Exception:
        return 0.0
    return zipf_frequency(word.replace("_", " "), "en")


def _trim_to(entries_by_domain, target):
    """Trim to ``target`` entries, preferring genuinely common words.

    Every closed-class (function-word) entry is always kept — those classes are
    small and essential. The remaining budget is filled from the open-class pool
    ranked by a *real* corpus frequency (wordfreq Zipf), then WordNet sense
    richness, then SemCor count, then shorter words, then alphabetically. This
    keeps the words people actually use and sheds the obscure long tail instead
    of an arbitrary alphabetical slice.
    """
    kept = defaultdict(list)
    open_pool = []
    for dom, entries in entries_by_domain.items():
        if dom in CLOSED_DOMS:
            kept[dom] = list(entries)
        else:
            open_pool.extend(entries)
    for e in open_pool:
        e["_zipf"] = _real_freq(e["word"])
    budget = target - sum(len(v) for v in kept.values())
    open_pool.sort(key=lambda e: (-e["_zipf"], -e["_nsense"], -e["_freq"],
                                  len(e["word"]), e["word"]))
    for e in open_pool[:max(0, budget)]:
        kept[e["dom"]].append(e)
    return kept


def assemble_document(all_entries, args):
    entries = {}
    for e in all_entries:
        key = e["_key"]
        entries[key] = {
            "word": e["word"],
            "shell": e["shell"],
            "theta": e["theta"],
            "kappa": e["kappa"],
            "dom": e["dom"],
            "addr": e["addr"],
            "ptr": e["ptr"],
            "ptr_orthogonal": e["ptr_orthogonal"],
            "ptr_oppositional": e["ptr_oppositional"],
            "curation": e["curation"],
        }
    domains_out = {d: {"description": v["description"], "shell": v["shell"]}
                   for d, v in DOMAINS.items()}
    return {
        "library": args.library,
        "version": args.version,
        "schema": "lexicon-1.0",
        "kind": "lexicon",
        "trust_level": "derived",
        "domains": domains_out,
        "descriptor_protocol": {
            "format": "underscore_separated",
            "case": "lowercase",
            "stopwords": [],
            "min_length": 1,
        },
        "entries": entries,
        "exports": ["by_word", "by_address", "ptr_graph"],
        "curation_pass_version": args.version,
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="libs/word_dict_75k_lib.json")
    p.add_argument("--library", default="WordDict.75k")
    p.add_argument("--version", default="1.0.0")
    p.add_argument("--max-entries", type=int, default=0,
                   help="cap total entries (0 = no cap)")
    p.add_argument("--max-tokens", type=int, default=3,
                   help="max tokens in a multiword lemma")
    p.add_argument("--no-multiword", action="store_true",
                   help="exclude underscore_separated multiword lemmas")
    p.add_argument("--moby", default="data/mthesaur.txt",
                   help="path to Moby Thesaurus (mthesaur.txt)")
    p.add_argument("--no-offline", action="store_true",
                   help="skip the offline Moby + antonym enrichment pass")
    p.add_argument("--enrich", action="store_true",
                   help="ALSO run the optional cached Datamuse polish pass")
    p.add_argument("--enrich-budget", type=int, default=25000,
                   help="max Datamuse word lookups this run")
    p.add_argument("--enrich-workers", type=int, default=16)
    p.add_argument("--cache", default=".cache/datamuse.json")
    p.add_argument("--minify", action="store_true",
                   help="emit compact JSON (smaller file)")
    return p.parse_args(argv)


if __name__ == "__main__":
    build(parse_args())
