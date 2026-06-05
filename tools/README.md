# WordDict lexicon builder

Deterministic toolchain that scans dictionaries and emits a `lexicon-1.0`
library exactly matching `word_dict_structure.json` — **100% schema-valid by
construction** and gated by an independent validator.

## Sources

| Source | Role |
|--------|------|
| **Princeton WordNet 3.0** (via `nltk`) | Word list, part of speech, definitions, synonyms, antonyms, taxonomy. The structural backbone. |
| **Moby Thesaurus II** (public domain) | Offline synonym/related breadth — no rate limits, fills `ptr`/`ptr_orthogonal` for every common word. |
| **wordfreq** | Real-corpus frequency for ranking the trim and filtering noise. |
| **Datamuse API** | *Optional* online polish (`--enrich`); the offline build is already complete without it. |

The first three are fully offline after a one-time download, so a build needs
no network and is completely reproducible.

## Files

| File | Purpose |
|------|---------|
| `fetch_sources.py`   | Download WordNet (nltk) + Moby thesaurus. Run once. |
| `build_lexicon.py`   | Generator: ingest → place → link → offline-enrich → (datamuse) → curate → emit. |
| `offline_enrich.py`  | Moby synonyms + WordNet antonym closure + coverage report. |
| `validate_lexicon.py`| Independent invariant checker. Exits non-zero on any violation. |
| `closed_class.py`    | Authoritative function-word seeds (PRON/DET/NUM/PREP/CONJ/AUX/INTERJ). |
| `datamuse_enrich.py` | Optional cached/bounded Datamuse polish pass. |

## Quick start

```bash
pip install nltk wordfreq
python tools/fetch_sources.py

# build ~120k entries (offline, no network), minified
python tools/build_lexicon.py --max-entries 120000 --minify

# validate — must print PASSED and exit 0
python tools/validate_lexicon.py libs/word_dict_75k_lib.json
```

Optional online polish (adds antonyms/synonyms WordNet+Moby miss; cached &
resumable, capped to stay under Datamuse's ~100k/day limit):

```bash
python tools/build_lexicon.py --max-entries 120000 --minify --enrich
```

## How the schema is satisfied

* **Domains.** WordNet lexicographer files map to the 19 schema domains:
  `noun.substance|food`→`NOUNM`, abstract `noun.*`→`NOUNA`, named entities→
  `NOUNP`, `verb.stative|cognition|emotion|perception`→`VERBS` (else `VERBA`),
  else concrete→`NOUNC`. Function words come from `closed_class.py`.
* **Geometry.** `shell` = domain ring; `theta` spreads words around the circle
  with the golden angle, nudged to a unique 4-decimal value per shell; `kappa`
  is a deterministic per-word curvature signature; `addr = "<shell>@<theta>"`
  is therefore globally unique.
* **Pointers (two-pass, never dangling).**
  * `ptr` — WordNet synset synonyms, adjective `similar_to` clusters, hypernyms,
    then same-POS Moby synonyms. High precision (curated, same part of speech).
  * `ptr_orthogonal` (≤5) — cross-POS derivations, coordinate sisters, holonyms,
    plus Moby relatives. "Related but on a different axis."
  * `ptr_oppositional` — a single antonym from WordNet's full closure: direct,
    adjective-satellite (`similar_to`→head antonym), and derivational
    propagation across POS (happy/unhappy → happily/unhappily). Curated only —
    no noisy morphological guessing.
* **Curation.** Status/score fields are derived from the resolved pointer counts
  so the status↔cardinality invariants hold automatically.

## Quality controls

* **No noise/garbage.** Roman numerals dropped (keep-list spares real words like
  `mix`/`cd`/`iv`); only same-POS words enter `ptr`; the trim keeps genuinely
  frequent words (wordfreq) and sheds the obscure tail.
* **Determinism.** Fixed WordNet + Moby ⇒ identical output every run; all
  tie-breaks are explicit.
* **Self-check.** Every build prints a coverage report; `validate_lexicon.py`
  asserts all consistency invariants independently of the builder.
