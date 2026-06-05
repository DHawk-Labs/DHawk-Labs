"""
datamuse_enrich.py — bounded, cached enrichment pass (dictionary #2).

WordNet has excellent synonymy but sparse antonymy (~7.6k antonym pairs) and no
general "related word" axis beyond its taxonomic links. The Datamuse API
(https://api.datamuse.com) aggregates several lexical resources and exposes:

    rel_ant  -> antonyms        (fills ptr_oppositional)
    rel_trg  -> "triggers"      (fills ptr_orthogonal — strongly associated)
    rel_syn  -> synonyms        (back-fills ptr when WordNet gave none)

Design constraints (this must never corrupt a one-shot build):

  * Every network result is cached on disk keyed by word, so re-runs are free
    and deterministic. A half-finished run resumes from the cache.
  * A hard request *budget* caps how many *new* lookups a run performs.
  * Only suggestions that resolve to an EXISTING entry address are applied, so
    enrichment can never introduce a dangling pointer.
  * Cardinality invariants are respected: ptr<=8, ptr_orthogonal<=5,
    ptr_oppositional is a single address.
  * Candidates are processed most-common-first so a partial budget still
    improves the highest-value entries.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API = "https://api.datamuse.com/words"

_POS_OF_DOM = {
    "NOUNC": "n", "NOUNM": "n", "NOUNA": "n", "NOUNP": "n",
    "VERBA": "v", "VERBS": "v", "ADJ": "a", "ADV": "r",
}

# Domains worth enriching for antonyms / related words.
_ANTONYM_DOMS = {"ADJ", "ADV", "VERBA", "VERBS", "NOUNA", "NOUNC"}

# Enrichment priority by domain: adjectives/adverbs benefit most (WordNet gives
# them no hypernyms, so their ptr is often empty), so cover them first; then
# verbs, then nouns. Lower number = higher priority.
_DOM_PRIORITY = {
    "ADJ": 0, "ADV": 0,
    "VERBA": 1, "VERBS": 1,
    "NOUNA": 2,
    "NOUNC": 3,
}


def _load_cache(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def _save_cache(path, cache):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _fetch(word, retries=3):
    """Return {'ant':[...], 'trg':[...], 'syn':[...]} for *word* or {} on fail."""
    out = {"ant": [], "trg": [], "syn": []}
    params = {"ant": "rel_ant", "trg": "rel_trg", "syn": "rel_syn"}
    for short, rel in params.items():
        url = API + "?" + urllib.parse.urlencode({rel: word, "max": 12})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                out[short] = [d["word"].lower().replace(" ", "_")
                              for d in data if "word" in d]
                break
            except Exception:
                if attempt == retries - 1:
                    return None  # signal failure -> do not cache
                time.sleep(1.5 * (attempt + 1))
    return out


def run(all_entries, word_pos_to_addr, word_to_addrs, *, budget, workers,
        cache_path):
    cache = _load_cache(cache_path)
    cache_lock = threading.Lock()

    # rank candidates: only those that can benefit, common words first.
    candidates = []
    by_addr = {e["addr"]: e for e in all_entries}
    for e in all_entries:
        dom = e["dom"]
        if dom not in _ANTONYM_DOMS:
            continue
        needs_ant = (e["ptr_oppositional"] == "")
        needs_trg = (len(e["ptr_orthogonal"]) < 3)
        needs_syn = (len(e["ptr"]) < 3)
        if needs_ant or needs_trg or needs_syn:
            candidates.append(e)
    # Spend the budget where it matters most: common words first (high SemCor
    # frequency / sense count), and among those the weakest entries (fewest
    # existing pointers) first, so the most-used words get filled before the
    # obscure tail. Final tie-break alphabetical for determinism.
    candidates.sort(key=lambda e: (
        _DOM_PRIORITY.get(e["dom"], 9),
        -e.get("_freq", 0),
        -e.get("_nsense", 0),
        len(e["ptr"]) + len(e["ptr_orthogonal"]),
        e["word"],
    ))

    enriched = set()
    spent = 0
    to_fetch = []
    for e in candidates:
        w = e["word"]
        if w in cache:
            continue
        to_fetch.append(w)
        if len(to_fetch) >= budget:
            break

    # de-dupe while preserving order
    seen = set()
    uniq = []
    for w in to_fetch:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    to_fetch = uniq

    if to_fetch:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch, w): w for w in to_fetch}
            done = 0
            for fut in as_completed(futs):
                w = futs[fut]
                res = fut.result()
                done += 1
                if res is not None:
                    with cache_lock:
                        cache[w] = res
                if done % 1000 == 0:
                    with cache_lock:
                        _save_cache(cache_path, cache)
                    print(f"        datamuse: {done}/{len(to_fetch)} fetched",
                          flush=True)
        _save_cache(cache_path, cache)
        spent = len(to_fetch)

    def resolve(word_lc, pg, self_addr):
        a = word_pos_to_addr.get((word_lc, pg))
        if a and a != self_addr:
            return a
        for cand in word_to_addrs.get(word_lc, ()):
            if cand != self_addr:
                return cand
        return None

    # apply cached results (covers this run's fetches AND prior runs)
    for e in candidates:
        data = cache.get(e["word"])
        if not data:
            continue
        pg = _POS_OF_DOM.get(e["dom"], "n")
        changed = False

        # antonym
        if e["ptr_oppositional"] == "":
            for cand in data.get("ant", []):
                a = resolve(cand, pg, e["addr"])
                if a:
                    e["ptr_oppositional"] = a
                    changed = True
                    break

        # related (orthogonal) — top up toward 3..5
        if len(e["ptr_orthogonal"]) < 5:
            for cand in data.get("trg", []):
                if len(e["ptr_orthogonal"]) >= 5:
                    break
                a = resolve(cand, pg, e["addr"])
                if a and a not in e["ptr_orthogonal"] and a not in e["ptr"] \
                        and a != e["addr"]:
                    e["ptr_orthogonal"].append(a)
                    changed = True

        # synonyms — back-fill ptr toward 3
        if len(e["ptr"]) < 3:
            for cand in data.get("syn", []):
                if len(e["ptr"]) >= 8:
                    break
                a = resolve(cand, pg, e["addr"])
                if a and a not in e["ptr"] and a != e["addr"] \
                        and a not in e["ptr_orthogonal"]:
                    e["ptr"].append(a)
                    changed = True

        if changed:
            enriched.add(e["addr"])

    print(f"        datamuse: {spent} new lookups this run, "
          f"cache size {len(cache)}", flush=True)
    return enriched
