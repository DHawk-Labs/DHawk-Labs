"""
offline_enrich.py — fully-offline, no-budget enrichment (secondary sources).

This makes the lexicon *complete* without depending on any rate-limited online
API. Two offline sources are used:

  Moby Thesaurus II (public domain)  -> synonyms / related, for ptr & orthogonal
  WordNet (curated)                  -> exhaustive antonym extraction

Antonyms are the hardest axis. We extract them from WordNet three ways, all of
which are human-curated (so they introduce no noise):

  A. direct           lemma.antonyms()
  B. satellite        an adjective satellite inherits the antonym of its head
                      cluster (via similar_to) — covers thousands of adjectives
                      WordNet only links indirectly
  C. derivational     antonymy is carried across part-of-speech through
                      derivationally-related forms (happy/unhappy ->
                      happily/unhappily, increase/decrease noun<->verb, ...)

Every produced relation is gated to words that are actually entries in the
lexicon, so no pointer can dangle and no junk word is ever introduced.
"""

from __future__ import annotations

import os
from collections import defaultdict


# ---------------------------------------------------------------------------
# Moby Thesaurus — synonym / related co-occurrence graph
# ---------------------------------------------------------------------------
def load_moby(path, cap_per_word=60):
    """Return ``{root_word: [synonym, ...]}`` from Moby (directional).

    Each Moby line is ``root,syn1,syn2,...`` — a curated synonym list for the
    *root*. We index only ``root -> its own synonyms`` (not the reverse), which
    avoids the loose "reverse association" noise that arises from treating every
    co-member as mutually related (e.g. the root 'abandoned' listing 'open'
    must NOT make 'open' a synonym of 'abandoned'). Common words still get
    coverage because they themselves appear as roots.
    """
    if not os.path.exists(path):
        return {}
    graph = {}
    with open(path, encoding="latin-1") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            members = []
            for tok in line.split(","):
                w = tok.strip().lower().replace(" ", "_")
                if w and all(c.isalpha() or c in "-'_" for c in w):
                    members.append(w)
            if len(members) < 2:
                continue
            root, syns = members[0], members[1:]
            # de-dupe, drop self
            seen, out = {root}, []
            for s in syns:
                if s not in seen:
                    seen.add(s)
                    out.append(s)
            if out:
                graph[root] = out[:cap_per_word]
    # NOTE: Moby's native order is alphabetical. We deliberately keep it rather
    # than re-ranking by frequency: many Moby clusters include figurative-sense
    # fillers ('out'/'old'/'hard' for 'cold') that are high-frequency, and a
    # frequency sort would promote exactly those loose words. Precise synonyms
    # come from WordNet (synset + similar_to); Moby is a breadth supplement.
    return graph


def apply_moby(entries, moby, resolve_same_pos, resolve_any_pos):
    """Top up ptr and ptr_orthogonal from a word's own Moby synonym list.

    * ``ptr`` (closest neighbours) is filled **same-part-of-speech only**, so an
      adjective never receives a noun as a "synonym".
    * ``ptr_orthogonal`` (different-axis relatives) may take cross-POS Moby
      relatives once the same-POS synonyms are placed.

    Returns the set of addresses that were changed.
    """
    changed = set()
    for e in entries:
        syns = moby.get(e["word"])
        if not syns:
            continue
        pg = e["_pos_group"]
        self_addr = e["addr"]
        ptr, orth = e["ptr"], e["ptr_orthogonal"]
        touched = False
        # pass 1: same-POS synonyms -> ptr
        for rw in syns:
            if len(ptr) >= 8:
                break
            a = resolve_same_pos(rw, pg, self_addr)
            if a and a != self_addr and a not in ptr and a not in orth:
                ptr.append(a)
                touched = True
        # pass 2: remaining relatives -> orthogonal (cross-POS allowed)
        for rw in syns:
            if len(orth) >= 5:
                break
            a = resolve_any_pos(rw, pg, self_addr)
            if a and a != self_addr and a not in orth and a not in ptr:
                orth.append(a)
                touched = True
        if touched:
            changed.add(self_addr)
    return changed


# ---------------------------------------------------------------------------
# WordNet antonym extraction (direct + satellite + derivational)
# ---------------------------------------------------------------------------
_PG = {"n": "n", "v": "v", "a": "a", "s": "a", "r": "r"}


def build_antonym_map(entry_pos_set):
    """Return ``{(word_lc, pos_group): antonym_word_lc}`` for entry words.

    Only relations whose *both* endpoints are entries (present in
    ``entry_pos_set``) are kept, so resolution can never dangle.
    """
    from nltk.corpus import wordnet as wn

    # raw[(word,pg)] -> set of candidate antonym words (same pg)
    raw = defaultdict(set)

    def add_pair(w1, p1, w2, p2):
        a = (w1.lower(), p1)
        b = (w2.lower(), p2)
        if a in entry_pos_set and b in entry_pos_set:
            raw[a].add(w2.lower())
            raw[b].add(w1.lower())

    # ---- A: direct + B: satellite (per-synset antonym closure) ----
    for syn in wn.all_synsets():
        pg = _PG[syn.pos()]
        # collect antonyms visible from this synset and its similar_to cluster
        cluster = [syn] + (syn.similar_tos() if syn.pos() in ("a", "s") else [])
        cluster_lemmas = []
        for s in cluster:
            cluster_lemmas.extend(s.lemmas())
        for lem in syn.lemmas():
            for cl in cluster_lemmas:
                for ant in cl.antonyms():
                    add_pair(lem.name(), pg, ant.name(), _PG[ant.synset().pos()])

    # ---- build derivational map for propagation (C) ----
    # deriv[(word,pg)] -> set of (dword, dpg)
    deriv = defaultdict(set)
    for syn in wn.all_synsets():
        pg = _PG[syn.pos()]
        for lem in syn.lemmas():
            lw = lem.name().lower()
            for d in lem.derivationally_related_forms():
                dpg = _PG[d.synset().pos()]
                deriv[(lw, pg)].add((d.name().lower(), dpg))

    # ---- C: propagate antonymy across POS through derivation ----
    # If (a)~(b) are antonyms, a'≅a and b'≅b derivationally into the same POS,
    # and a',b' are both entries, then a'~b'.
    propagated = []
    for (aw, apg), ants in list(raw.items()):
        for bw in list(ants):
            bpg = apg  # raw stores same-pg candidates
            for (apw, appg) in deriv.get((aw, apg), ()):
                for (bpw, bppg) in deriv.get((bw, bpg), ()):
                    if appg == bppg and apw != bpw:
                        propagated.append((apw, appg, bpw, bppg))
    for w1, p1, w2, p2 in propagated:
        add_pair(w1, p1, w2, p2)

    # ---- collapse to a single deterministic antonym per key ----
    out = {}
    for key, cands in raw.items():
        word = key[0]
        # deterministic: shortest, then alphabetical; never the word itself
        best = sorted((c for c in cands if c != word), key=lambda c: (len(c), c))
        if best:
            out[key] = best[0]
    return out


def apply_antonyms(entries, ant_map, resolve):
    """Fill ptr_oppositional from the antonym map where currently empty."""
    changed = set()
    for e in entries:
        if e["ptr_oppositional"]:
            continue
        key = (e["word"], e["_pos_group"])
        aw = ant_map.get(key)
        if not aw:
            continue
        a = resolve(aw, e["_pos_group"], e["addr"])
        if a and a != e["addr"]:
            e["ptr_oppositional"] = a
            changed.add(e["addr"])
    return changed


# ---------------------------------------------------------------------------
# Coverage report — answers "is this complete?"
# ---------------------------------------------------------------------------
def coverage_report(entries):
    from collections import Counter
    per_dom = defaultdict(lambda: Counter())
    tot = Counter()
    # domains where an antonym is semantically expected
    ANT_DOMS = {"ADJ", "ADV", "VERBA", "VERBS", "NOUNA"}
    for e in entries:
        d = e["dom"]
        per_dom[d]["n"] += 1
        tot["n"] += 1
        if len(e["ptr"]) >= 1:
            per_dom[d]["ptr1"] += 1; tot["ptr1"] += 1
        if len(e["ptr"]) >= 3:
            per_dom[d]["ptr3"] += 1; tot["ptr3"] += 1
        if len(e["ptr_orthogonal"]) >= 3:
            per_dom[d]["orth3"] += 1; tot["orth3"] += 1
        if e["ptr_oppositional"]:
            per_dom[d]["opp"] += 1; tot["opp"] += 1
        if d in ANT_DOMS:
            per_dom[d]["ant_dom"] += 1; tot["ant_dom"] += 1
    lines = []
    lines.append("=== COVERAGE REPORT ===")
    lines.append(f"{'domain':8s} {'n':>7s} {'ptr>=1':>7s} {'ptr>=3':>7s} "
                 f"{'orth>=3':>8s} {'antonym':>8s}")
    for d in sorted(per_dom, key=lambda x: -per_dom[x]["n"]):
        c = per_dom[d]
        n = c["n"]
        def pct(k):
            return f"{100*c[k]/n:.0f}%" if n else "-"
        ant = f"{100*c['opp']/c['ant_dom']:.0f}%" if c['ant_dom'] else "n/a"
        lines.append(f"{d:8s} {n:7d} {pct('ptr1'):>7s} {pct('ptr3'):>7s} "
                     f"{pct('orth3'):>8s} {ant:>8s}")
    n = tot["n"]
    lines.append(f"{'TOTAL':8s} {n:7d} {100*tot['ptr1']/n:.0f}%"
                 f"   ptr>=3 {100*tot['ptr3']/n:.0f}%  "
                 f"antonym(of antonym-doms) "
                 f"{100*tot['opp']/max(tot['ant_dom'],1):.0f}%")
    return "\n".join(lines)
