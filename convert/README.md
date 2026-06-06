# prose → lexicon converter

Turns unstructured content (plain text, web pages, scientific-paper PDFs) into a
structured **lexicon-1.0 encoding**: every recognised word/phrase is resolved to
a dictionary entry and emitted with its `addr` (`<shell>@<theta>`), domain,
library and the pointer subgraph it participates in. An encoded document is
literally a path through the dictionary space built earlier (WordDict +
SciMedDict), so it drops straight into the same system — no format change.

```
source ──extract──▶ clean text ──encode──▶ { descriptors, concept_index,
 (txt/html/pdf/url)                          ptr_graph, oov, stats }
```

## Files

| File | What it is |
|------|------------|
| `extract.py`           | One extractor per **source type** + auto-dispatch. |
| `lexicon_index.py`     | Loads the dictionaries; word / phrase / address resolver. |
| `encode.py`            | The **resolution methods** + structured-output builder. |
| `prose_to_lexicon.py`  | CLI: one source → one `.lex.json`. |
| `test_sources.py`      | Runs every method across every source type and reports. |
| `samples/`, `out/`     | A sample input; written encodings. |

## Source extractors (`extract.py`) — one method per source

| Method | Source | How it works |
|--------|--------|--------------|
| `extract_text(path)` | `.txt` / `.md` | Read + whitespace tidy. |
| `extract_html(html)` | web page | **trafilatura** main-article extraction (strips nav/ads/boilerplate); BeautifulSoup fallback that drops `script/style/nav/header/footer/aside`. |
| `extract_pdf(path)`  | scientific paper | **pypdf** per-page text extraction + metadata title. |
| `fetch(url)`         | any URL | `requests` GET, then routes by `Content-Type` (PDF → `extract_pdf`, else `extract_html`). |
| `extract(source)`    | auto | URL vs local path; `.pdf/.html/.txt` by extension/sniff. |

All return `{"text": str, "meta": {...}}`, so the encoder never cares where the
text came from.

## Resolution methods (`encode.py`) — developed and measured in this order

Each method is an independent, toggleable layer. The numbers are the coverage
gain measured in `test_sources.py` across the four test sources.

1. **exact** — lowercase surface-form lookup. Baseline ≈ 67–87 %.
2. **+lemma** — WordNet lemmatisation tries noun/verb/adj/adverb roots
   (`cells→cell`, `studies→study`, `running→run`). **Biggest gain: +6–8 pts.**
3. **+multiword** — greedy **longest-match** of `underscore_separated` terms via
   a first-token phrase index (`"myocardial infarction" → myocardial_infarction`).
   Captures domain terms as single concepts; measured by **tokens covered**, not
   descriptor count (a phrase covers its whole span). +0.5–1 pt and better
   concept fidelity.
4. **+hyphen** — decomposes unresolved hyphenated compounds into component words
   (`light-dependent → light, dependent`; `self-attention → self, attention`).
   **+1–2.5 pts**, and removes a whole class of OOV.
5. **function-word disambiguation** — when a word resolves in several domains,
   closed-class words (`a/the/in/of/will`) prefer their function reading, content
   words prefer their content sense, and across libraries a domain dictionary
   (SciMedDict) wins ties — so `aspirin` resolves medical, `a` resolves as DET.

### Output document

```jsonc
{
  "source":  { "type": "...", "title": "...", "source": "..." },
  "config":  { "use_lemma": true, "use_multiword": true, ... },
  "stats":   { "word_tokens", "covered_tokens", "coverage_pct",
               "content_coverage_pct", "unique_descriptors", "oov_occurrences",
               "domain_histogram", "library_histogram", "via_histogram" },
  "descriptors":   [ {surface, word, lib, addr, dom, shell, via, pos}, ... ],
  "concept_index": { word: {addr, dom, lib, count} },          // by_word
  "ptr_graph":     { "lib:addr": ["addr", ...] },              // in-doc subgraph
  "oov_top":       [ {term, count}, ... ]                       // coverage gaps
}
```

`ptr_graph` keeps only edges between concepts that are **both present in the
document** — i.e. the slice of the dictionary's pointer graph this text lights
up. (Addresses are unique only *within* a library, so the graph is keyed
`lib:addr`.)

## Usage

```bash
pip install nltk wordfreq beautifulsoup4 lxml pypdf trafilatura requests
python -c "import nltk; nltk.download('wordnet'); nltk.download('omw-1.4')"

# one source -> one encoding (libraries listed in resolution-priority order)
python convert/prose_to_lexicon.py https://en.wikipedia.org/wiki/Photosynthesis \
    --lex libs/scimed_dict_lib.json libs/word_dict_75k_lib.json --full --out out.json

python convert/prose_to_lexicon.py paper.pdf
python convert/prose_to_lexicon.py notes.txt --no-multiword

# benchmark every method across every source type
python convert/test_sources.py
```

## Measured results

| Source (type) | tokens | exact | +lemma | +mw | +hyphen | content % | OOV |
|---|--:|--:|--:|--:|--:|--:|--:|
| mixed prose (txt) | 106 | 86.8 | 99.1 | 99.1 | **100.0** | 100.0 | 0 |
| Photosynthesis (html) | 11,605 | 69.5 | 75.7 | 76.3 | **77.3** | 80.4 | 742 |
| Myocardial infarction (html) | 13,976 | 67.9 | 73.0 | 73.5 | **74.1** | 80.8 | 761 |
| Attention paper (pdf) | 6,279 | 66.9 | 74.9 | 75.1 | **77.5** | 82.4 | 325 |

## What OOV tells you (it's a feature)

The OOV report surfaces exactly what the dictionaries don't cover:

* **Citation / markup noise** from web & PDF sources — `pmid, isbn, doi, pp,
  bmj, arxiv`. Candidate for an extraction-side stop-list.
* **Real domain-term gaps** — `co2, c3, c4, oxygenic` (chemistry notation),
  `pci, st-segment` (cardiology), `encoder, embeddings, softmax` (ML/CS). The
  ML/CS cluster confirms there is no computer-science dictionary yet — a
  domain library would close it, the same way SciMedDict closed the medical gap.

## Next iterations (not yet built)

* POS-tagged disambiguation for the residual ambiguous content words
  (`can`, `will`, `lead`) using sentence context.
* Extraction-side citation/reference stripping for cleaner web encodings.
* Sentence/section structure preservation (currently a flat token stream).

---

# Coupling maps from snippets (`snippets_to_xmap.py`)

The keyword converter above resolves *individual words*. This tool resolves
*snippets* (whole documents, or sections/paragraphs of one) and emits a
**WPE-5 cross-coupling map** — schema `wpe-5.0-xmap-1.0`, structurally identical
to the PanWorld example — describing how the snippets couple to each other.

## How a snippet becomes a PART

Each snippet is resolved against the dictionaries (reusing `encode.Encoder`),
then placed in WPE phase space:

| Field | Derivation |
|-------|------------|
| `domain_codes` | the lexicon domains the snippet resolves into (by frequency) |
| `shell` | `min(shell of domain_codes)` — satisfies the schema invariant |
| `theta` | frequency-weighted **circular mean** of its descriptors' θ |
| `kappa` | `-(1 + 3·concentration)` — more topically focused ⇒ deeper/stabler well |
| `addr` | `"<shell>@<theta>"`, made unique per part |
| `key_subsystems` | the snippet's top resolved concepts |

## How parts couple

Each part becomes a **TF-IDF concept vector** over the descriptor addresses it
resolved, **expanded along the dictionaries' own `ptr`/`ptr_orthogonal` edges**
so snippets about *related* concepts couple even without identical wording.
Then for every pair:

```
c           = cosine(vec_i, vec_j)         # semantic phase alignment  [-1,1]
delta_theta = degrees(arccos(c))           # so c == cos(delta_theta)
tier        = tier_from_c(c)               # SYNERGISTIC … OPPOSITION
```

Per-part pointers follow the schema's tier thresholds:
`ptr_coupling`/`ptr_sequence` (c ≥ 0.34, sequence = directed to later parts),
`ptr_orthogonal` (−0.09 ≤ c < 0.34), `ptr_opposition` (c < −0.09).

The full map also emits `coupling_matrix` (symmetric N×N), `cross_coupling_index`
(interface-level detail with `key_bridges` = the concepts two snippets share),
`shell_hierarchy`, `phase_resonance_bands`, the 16 `consistency_invariants`, and
`lookup_paths` — all in the exact PanWorld layout.

## Usage

```bash
# each source = one part
python convert/snippets_to_xmap.py a.pdf b.html https://example.com/x --out map.json

# one document, split into coupled sections
python convert/snippets_to_xmap.py article.html --split sections --min-chars 1500 --out map.json

# gate it — checks all 16 wpe-5.0-xmap-1.0 invariants
python convert/validate_xmap.py map.json
```

## Validated behaviour

* 4 unrelated documents → all WEAK couplings (clinical↔ML lowest 0.09); correct.
* 1 article split into 50 sections → 1,225 pairs, MODERATE/REINFORCING couplings
  between related sections (e.g. two symptom sections at c≈0.38); 4 detail records.
* Both pass `validate_xmap.py` (all 16 invariants).

`strip_citations=True` (default) drops bibliographic tokens (doi/isbn/pmid/et al)
so couplings reflect content, not reference-list cruft. Feeding clean body prose
(rather than a reference-heavy page) yields the cleanest coupling maps.
