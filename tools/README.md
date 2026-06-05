# WordDict lexicon builder

Deterministic toolchain that scans two online dictionaries and emits a
`lexicon-1.0` library exactly matching `word_dict_structure.json`.

* **Dictionary #1 — Princeton WordNet** (via `nltk`): the word list, part of
  speech, definitions, synonyms, antonyms and taxonomic relations.
* **Dictionary #2 — Datamuse API** (`api.datamuse.com`): a bounded, cached
  enrichment pass that fills antonyms / related / synonym pointers WordNet
  leaves empty.

The output is **100% schema-valid by construction** and gated by an independent
validator (`validate_lexicon.py`) that asserts every consistency invariant.

## Files

| File | Purpose |
|------|---------|
| `build_lexicon.py`  | Main generator (ingest → place → link → enrich → curate → emit). |
| `validate_lexicon.py` | Independent invariant checker. Exits non-zero on any violation. |
| `closed_class.py`   | Authoritative function-word seeds (PRON/DET/NUM/PREP/CONJ/AUX/INTERJ). |
| `datamuse_enrich.py`| Cached, budgeted Datamuse enrichment (dictionary #2). |

## One-time setup

```bash
pip install nltk
python -c "import nltk; nltk.download('wordnet'); nltk.download('omw-1.4')"
```

## Build

```bash
# full build: ~120k entries, with cached Datamuse enrichment, minified
python tools/build_lexicon.py \
    --out libs/word_dict_75k_lib.json \
    --max-entries 120000 \
    --enrich --enrich-budget 30000 \
    --minify

# validate (must print PASSED and exit 0)
python tools/validate_lexicon.py libs/word_dict_75k_lib.json
```

### Key flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--out` | `libs/word_dict_75k_lib.json` | Output path. |
| `--max-entries` | `0` (no cap) | Trim to N entries, keeping the most frequent words + all function words. |
| `--max-tokens` | `3` | Max tokens in a multiword (`underscore_separated`) lemma. |
| `--no-multiword` | off | Single-word lemmas only. |
| `--enrich` | off | Run the Datamuse enrichment pass. |
| `--enrich-budget` | `25000` | Max **new** Datamuse word lookups per run (×3 sub-requests each; stay under Datamuse's 100k/day). |
| `--cache` | `.cache/datamuse.json` | Enrichment cache; re-runs are free and resumable. |
| `--minify` | off | Compact JSON (smaller file). |

## How the schema is satisfied

* **Domains.** WordNet lexicographer files map to the 19 schema domains, e.g.
  `noun.substance`/`noun.food` → `NOUNM` (mass), abstract `noun.*` → `NOUNA`,
  named entities → `NOUNP`, `verb.stative|cognition|emotion|perception` →
  `VERBS` (else `VERBA`). Function-word domains come from `closed_class.py`.
* **Geometry.** `shell` is the domain ring; `theta` spreads words around the
  circle with the golden angle and is nudged to a unique 4-decimal value per
  shell; `kappa` is a deterministic per-word curvature signature;
  `addr = "<shell>@<theta>"` is therefore globally unique.
* **Pointers.** Resolved in two passes against an address index, so every
  pointer targets an existing entry (no dangling refs). `ptr` = synonyms /
  hypernyms; `ptr_orthogonal` (≤5) = cross-POS derivations, coordinate sisters,
  holonyms, Datamuse "triggers"; `ptr_oppositional` = a single antonym.
* **Curation.** `definition_strength`, `orthogonal_status`,
  `oppositional_status`, `needs_curation`, `auto_sources` and
  `sufficiency_score` are derived from the resolved pointer counts so the
  status↔cardinality invariants hold automatically.

## Determinism

Given a fixed WordNet version and Datamuse cache, the build is fully
reproducible: lemmas are processed in sorted order and every tie-break is
explicit. Delete `.cache/datamuse.json` to refetch from Datamuse.
