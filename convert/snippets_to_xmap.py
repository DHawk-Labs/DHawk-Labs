#!/usr/bin/env python3
"""
snippets_to_xmap.py — convert prose SNIPPETS into a WPE-5 cross-coupling map.

This is the coupling-map counterpart to prose_to_lexicon.py. Instead of emitting
one descriptor per keyword, it treats each *snippet* (a whole document, or a
section/paragraph of one) as a PART, places it in WPE phase space using its
lexicon resolution, and computes the pairwise COUPLING between parts.

Output schema: wpe-5.0-xmap-1.0 (identical structure to the PanWorld example).

How a snippet becomes a part
----------------------------
* resolve the snippet against WordDict + SciMedDict (reuses encode.Encoder)
* domain_codes = the lexicon domains the snippet uses (content domains)
* shell  = min(shell of those domains)              [schema invariant]
* theta  = frequency-weighted circular mean of its descriptors' thetas
* kappa  = -(1 + 3·concentration)  (more topically focused = deeper/stabler well)
* addr   = "<shell>@<theta>"  (unique per part)

How parts couple
----------------
Each part is a TF-IDF concept vector over the descriptor addresses it resolved,
expanded along the dictionaries' own ptr/orthogonal edges so conceptually
related snippets couple even without identical wording. Then:

    c           = cosine(vec_i, vec_j)            (semantic phase alignment)
    delta_theta = degrees(arccos(c))              (so c == cos(delta_theta))
    tier        = tier_from_c(c)

Pointers per part follow the tier thresholds the schema defines.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "tools"))
import extract                                         # noqa: E402
from lexicon_index import LexiconIndex                 # noqa: E402
from encode import Encoder, FUNCTION_DOMS              # noqa: E402
from build_lexicon import DOMAINS                       # noqa: E402  (shells+desc)

# ---- coupling tiers (verbatim ranges from the wpe-5.0-xmap-1.0 schema) ----
COUPLING_TIERS = {
    "SYNERGISTIC": {"c_range": "[0.87, 1.01)", "delta_theta_range": "Δθ∈[0.0°, 29.5°]",
                    "interpretation": "cos≥0.87 | Δθ≤30° | near-identical phase alignment"},
    "REINFORCING": {"c_range": "[0.61, 0.87)", "delta_theta_range": "Δθ∈[29.6°, 52.4°]",
                    "interpretation": "cos 0.61-0.87 | Δθ 30-52° | strong positive coupling"},
    "MODERATE":    {"c_range": "[0.34, 0.61)", "delta_theta_range": "Δθ∈[52.4°, 70.1°]",
                    "interpretation": "cos 0.34-0.61 | Δθ 52-70° | meaningful coupling with some offset"},
    "WEAK":        {"c_range": "[0.09, 0.34)", "delta_theta_range": "Δθ∈[70.1°, 84.8°]",
                    "interpretation": "cos 0.09-0.34 | Δθ 70-85° | distant but present influence"},
    "ORTHOGONAL":  {"c_range": "[-0.09, 0.09)", "delta_theta_range": "Δθ∈[84.8°, 95.2°]",
                    "interpretation": "cos ~0 | Δθ ~90° | independent axes, no direct coupling"},
    "OPPOSITION":  {"c_range": "[-1.01, -0.09)", "delta_theta_range": "Δθ > 95.2°",
                    "interpretation": "cos < -0.09 | Δθ>95° | active tension or complementary opposition"},
}


def angular_sep(a, b):
    """Phase separation of two angles in degrees, folded to [0, 180]."""
    d = abs(a - b) % 360.0
    return round(d if d <= 180.0 else 360.0 - d, 1)


def tier_from_c(c):
    # 'SELF' is positional (the matrix diagonal), never returned by tier-from-c:
    # two distinct parts may legitimately share a phase and be SYNERGISTIC.
    if c >= 0.87:
        return "SYNERGISTIC"
    if c >= 0.61:
        return "REINFORCING"
    if c >= 0.34:
        return "MODERATE"
    if c >= 0.09:
        return "WEAK"
    if c >= -0.09:
        return "ORTHOGONAL"
    return "OPPOSITION"


# ----------------------------------------------------------------- snippets
_HEADING = re.compile(r"^\s{0,3}(#{1,6}\s+\S|[A-Z][A-Za-z0-9 ,&/-]{2,60}\n=+\s*$)")


def split_snippets(text, mode, min_chars=400):
    """Split one document into snippet texts. Returns [(label, text), ...]."""
    if mode == "paragraphs":
        chunks = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
        if len(chunks) < 2:                       # no blank-line breaks: use lines
            chunks = [c.strip() for c in text.split("\n") if c.strip()]
    elif mode == "sections":
        # merge consecutive non-empty blocks until each chunk is substantial
        raw = [c.strip() for c in re.split(r"\n\s*\n|\n", text) if c.strip()]
        chunks, buf = [], ""
        for c in raw:
            buf = (buf + "\n" + c).strip() if buf else c
            if len(buf) >= min_chars:
                chunks.append(buf); buf = ""
        if buf:
            (chunks.append(buf) if not chunks else
             chunks.__setitem__(-1, chunks[-1] + "\n" + buf))
    else:  # whole
        chunks = [text]
    out = []
    for i, c in enumerate(chunks, 1):
        first = c.strip().splitlines()[0][:60] if c.strip() else f"snippet {i}"
        out.append((first.strip(" #="), c))
    return out


# ------------------------------------------------------------- part encoding
def circular_mean_deg(pairs):
    """Frequency-weighted circular mean of angles (deg). pairs=[(theta,w)]."""
    sx = sum(w * math.cos(math.radians(t)) for t, w in pairs)
    sy = sum(w * math.sin(math.radians(t)) for t, w in pairs)
    if sx == 0 and sy == 0:
        return 0.0
    return round(math.degrees(math.atan2(sy, sx)) % 360.0, 1)


def encode_part(ix, enc, label, text, source_meta):
    """Return (part_dict, concept_weights{(lib,addr):tf})."""
    res = enc.encode(text, source_meta)
    descs = res["descriptors"]
    content = [d for d in descs if d["dom"] not in FUNCTION_DOMS]
    dom_count = Counter(d["dom"] for d in content)
    # concept term-frequency over addresses (content only)
    tf = Counter((d["lib"], d["addr"]) for d in content)

    if not dom_count:
        dom_count["META"] = 1
    domain_codes = [d for d, _ in dom_count.most_common()]
    shell = min(DOMAINS[d]["shell"] for d in domain_codes if d in DOMAINS)
    dom = dom_count.most_common(1)[0][0]
    theta = (circular_mean_deg([(float(d["addr"].split("@")[1]), 1) for d in content])
             if content else 0.0)
    total = sum(dom_count.values())
    concentration = dom_count.most_common(1)[0][1] / total
    kappa = -round(1.0 + 3.0 * concentration, 2)

    part = {
        "label": label,
        "filename": source_meta.get("source") or source_meta.get("title") or label,
        "shell": shell, "theta": theta, "kappa": kappa, "dom": dom,
        "domain_codes": domain_codes,
        "section_count": text.count("\n\n") + 1,
        "component_count_approx": len(descs),
        "unique_concepts": len(tf),
        "_content_coverage": res["stats"]["content_coverage_pct"],
        "_dom_count": dict(dom_count),
        "semantic_role": _role(label, domain_codes),
        "_top_concepts": [
            {"lib": lib, "addr": a, "word": ix.by_addr[(lib, a)]["word"], "count": n}
            for (lib, a), n in tf.most_common(6)
        ],
    }
    return part, tf


def _role(label, domain_codes):
    fam = ", ".join(domain_codes[:3])
    return f"snippet_layer — '{label[:48]}' dominated by domains [{fam}]"


# --------------------------------------------------------------- coupling
def expand_vector(ix, tf, neighbor_weight=0.3):
    """TF vector expanded along dictionary ptr/orthogonal edges."""
    vec = defaultdict(float)
    for (lib, addr), w in tf.items():
        vec[(lib, addr)] += float(w)
        full = ix.by_addr.get((lib, addr), {})
        for a in full.get("ptr", []) + full.get("ptr_orthogonal", []):
            vec[(lib, a)] += neighbor_weight * w
    return vec


def cosine(va, vb):
    if not va or not vb:
        return 0.0
    keys = set(va) & set(vb)
    if not keys:
        return 0.0
    dot = sum(va[k] * vb[k] for k in keys)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


def apply_idf(vectors):
    """In-place TF->TF-IDF using document frequency across parts."""
    N = len(vectors)
    df = Counter()
    for v in vectors:
        for k in v:
            df[k] += 1
    for v in vectors:
        for k in list(v):
            v[k] *= math.log((N + 1) / (df[k])) + 1.0   # smoothed idf
    return vectors


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48] or "snippet"


def _assign_addresses(parts):
    """Set unique addr '<shell>@<theta>' per part (nudge theta on collision)."""
    used = set()
    for p in parts:
        theta = p["theta"]
        while f"{p['shell']}@{theta}" in used:
            theta = round((theta + 0.1) % 360.0, 1)
        p["theta"] = theta
        p["addr"] = f"{p['shell']}@{theta}"
        used.add(p["addr"])


def build_xmap(ix, encoded, source_count, library, coupling="geometric"):
    """encoded = [(part_dict, tf_counter), ...] in input order.

    coupling = 'geometric' : c = cos(Δ master-θ)  (pure WPE phase model)
               'semantic'  : c = cosine of ptr-expanded TF-IDF concept vectors,
                             with Δθ := arccos(c)
    """
    parts = [e[0] for e in encoded]
    ids = [f"P{i}" for i in range(1, len(parts) + 1)]
    for pid, p in zip(ids, parts):
        p["id"] = pid
    _assign_addresses(parts)
    addr_of = {pid: p["addr"] for pid, p in zip(ids, parts)}
    vectors = (apply_idf([expand_vector(ix, e[1]) for e in encoded])
               if coupling == "semantic" else None)

    # ---- symmetric coupling matrix ----
    n = len(parts)
    cmat = {pid: {} for pid in ids}
    for a in range(n):
        for b in range(n):
            if a == b:
                cmat[ids[a]][ids[b]] = {"c": 1.0, "delta_theta": 0.0, "tier": "SELF"}
                continue
            if coupling == "geometric":
                dt = angular_sep(parts[a]["theta"], parts[b]["theta"])
                c = round(math.cos(math.radians(dt)), 4)
            else:
                c = round(max(-1.0, min(1.0, cosine(vectors[a], vectors[b]))), 4)
                dt = round(math.degrees(math.acos(max(-1.0, min(1.0, c)))), 1)
            cmat[ids[a]][ids[b]] = {"c": c, "delta_theta": dt, "tier": tier_from_c(c)}

    # ---- per-part pointers from tiers ----
    for a, pid in enumerate(ids):
        seq, coup, orth, opp = [], [], [], []
        for b, qid in enumerate(ids):
            if a == b:
                continue
            c = cmat[pid][qid]["c"]
            if c >= 0.34:
                (seq if b > a else coup).append((c, addr_of[qid]))
            elif c >= -0.09:
                orth.append((c, addr_of[qid]))
            else:
                opp.append((c, addr_of[qid]))
        seq.sort(reverse=True); coup.sort(reverse=True)
        orth.sort(reverse=True); opp.sort()
        p = parts[a]
        p["ptr_sequence"] = [a for _, a in seq]
        p["ptr_coupling"] = [a for _, a in coup]
        p["ptr_orthogonal"] = [a for _, a in orth[:5]]
        p["ptr_opposition"] = [a for _, a in opp]
        _finish_part(p, ix)

    # ---- domains_table ----
    domains_table = {}
    for d in sorted({d for p in parts for d in p["domain_codes"]}):
        domains_table[d] = {
            "shell": DOMAINS[d]["shell"] if d in DOMAINS else 1,
            "type": "lexicon",
            "description": DOMAINS[d]["description"] if d in DOMAINS else d,
            "parts": [p["id"] for p in parts if d in p["domain_codes"]],
        }

    xc = _cross_coupling_index(parts, cmat, ids)
    doc = {
        "library": library,
        "file": f"{library.replace('.', '_')}.json",
        "schema": "wpe-5.0-xmap-1.0",
        "kind": "cross_coupling_map",
        "wpe_version": "5.0",
        "tme_version": "1.0",
        "temporal_scale": "α=1.0",
        "source_file_count": source_count,
        "unique_part_count": n,
        "coupling_pairs_total": n * (n - 1) // 2,
        "coupling_pairs_with_detail": len(xc),
        "domain_count": len(domains_table),
        "top_level_fields": _TOP_LEVEL_FIELDS,
        "part_key_format": "<PART_ID>",
        "part_key_example": "P4",
        "part_shape": _PART_SHAPE,
        "address_format": {
            "pattern": "<shell>@<theta>",
            "example": parts[0]["addr"] if parts else "9@270.0",
            "uniqueness": "every part has a unique addr; ptr fields reference parts by addr",
            "lookup": "parts_by_addr[addr] -> part entry",
        },
        "reference_graph": _REFERENCE_GRAPH,
        "coupling_tiers": COUPLING_TIERS,
        "domains_table": domains_table,
        "parts": {p["id"]: _emit_part(p) for p in parts},
        "coupling_matrix": {
            "parts_order": ids,
            "cell_schema": {"c": "float cos(Δθ) coupling strength",
                            "delta_theta": "float effective phase separation in degrees",
                            "tier": "string coupling tier label"},
            "values": cmat,
        },
        "cross_coupling_index": xc,
        "shell_hierarchy": _shell_hierarchy(parts, cmat, ids),
        "phase_resonance_bands": _phase_bands(parts, cmat, ids),
        "consistency_invariants": _INVARIANTS,
        "lookup_paths": _LOOKUP_PATHS,
    }
    return doc


def _finish_part(p, ix):
    shells = [ix.by_addr[(c["lib"], c["addr"])]["shell"]
              for c in p["_top_concepts"]] or [p["shell"]]
    p["shell_range"] = {"min": min(shells), "max": max(shells)}
    p["key_subsystems"] = [c["word"] for c in p["_top_concepts"]]
    p["wpe_built_ins_used"] = []
    p["wpe_output"] = f"{p['dom']}:{p['addr']}|{p['kappa']}:'{_slug(p['label'])}_master'"
    n_strong = len(p["ptr_sequence"]) + len(p["ptr_coupling"])
    n_orth = len(p["ptr_orthogonal"])
    cov = p.pop("_content_coverage", 0.0)
    uniq = p.get("unique_concepts", 0)
    score = round(0.6 * (cov / 100.0) + 0.4 * min(uniq / 40.0, 1.0), 2)
    p["curation"] = {
        "coupling_strength": "central" if n_strong >= 2 else "partial" if n_strong == 1 else "weak",
        "orthogonal_status": "filled" if n_orth >= 3 else "partial" if n_orth >= 1 else "empty",
        "oppositional_status": "filled" if p["ptr_opposition"] else "not_applicable",
        "needs_curation": score < 0.90,
        "auto_sources": ["wpe-5.0-xmap", "lexicon-extraction"],
        "sufficiency_score": score,
    }


_PART_KEYS = ["id", "addr", "label", "filename", "shell", "theta", "kappa", "dom",
              "domain_codes", "wpe_built_ins_used", "shell_range", "section_count",
              "component_count_approx", "key_subsystems", "wpe_output",
              "ptr_sequence", "ptr_coupling", "ptr_orthogonal", "ptr_opposition",
              "semantic_role", "curation"]


def _emit_part(p):
    return {k: p[k] for k in _PART_KEYS if k in p}


def _cross_coupling_index(parts, cmat, ids, cap=40):
    by_id = {p["id"]: p for p in parts}
    pairs = []
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            cell = cmat[ids[a]][ids[b]]
            if cell["c"] >= 0.34:
                pairs.append((cell["c"], ids[a], ids[b]))
    pairs.sort(reverse=True)
    out = []
    for c, pi, pj in pairs[:cap]:
        sp, tp = by_id[pi], by_id[pj]
        si = sp["_top_concepts"][0] if sp["_top_concepts"] else None
        ti = tp["_top_concepts"][0] if tp["_top_concepts"] else None
        if not si or not ti:
            continue
        cell = cmat[pi][pj]
        # interface-level numbers
        st = _concept_meta(sp, si); tt = _concept_meta(tp, ti)
        idt = round(abs(st["theta"] - tt["theta"]), 1)
        idt = idt if idt <= 180 else round(360 - idt, 1)
        ics = round(math.cos(math.radians(idt)), 4)
        hi, lo = max(st["shell"], tt["shell"]), min(st["shell"], tt["shell"])
        infl = round(1.0 / lo - 1.0 / hi, 4) if hi > lo else 0.0
        bridges = _key_bridges(sp, tp)
        out.append({
            "id": f"XC.{pi}.{pj}.{_slug(si['word'] + '_' + tj_word(ti))}",
            "source_part": pi, "target_part": pj, "operator": "coupling",
            "source_addr": sp["addr"], "target_addr": tp["addr"],
            "delta_theta_part_level": cell["delta_theta"],
            "coupling_strength_part_level": cell["c"], "tier": cell["tier"],
            "direction": "bidirectional",
            "source_interface": st, "target_interface": tt,
            "interface_delta_theta": idt, "interface_coupling_strength": ics,
            "shell_influence": {"formula": "I(λ_h,λ_l)=1/λ_l−1/λ_h",
                                 "lambda_high": hi, "lambda_low": lo, "value": infl,
                                 "note": f"shell {hi}→{lo} gradient" if hi > lo else "same shell — no gradient"},
            "semantic_label": _slug(si["word"] + "_couples_" + ti["word"]),
            "key_bridges": bridges,
            "wpe_notation": f"${pi}.{si['word']} coupling ${pj}.{ti['word']}",
        })
    return out


def tj_word(ti):
    return ti["word"]


def _concept_meta(part, concept):
    return {"component": f"{part['id']}.{concept['word']}", "dom": part["dom"],
            "shell": part["shell"], "theta": part["theta"], "kappa": part["kappa"],
            "wpe": f"{part['dom']}:{part['shell']}@{part['theta']}|{part['kappa']}"}


def _key_bridges(sp, tp, k=4):
    s = {c["word"] for c in sp["_top_concepts"]}
    t = {c["word"] for c in tp["_top_concepts"]}
    shared = sorted(s & t)
    if shared:
        return [f"{sp['id']}:{w} -> {tp['id']}:{w}" for w in shared[:k]]
    return [f"{sp['id']}:{a['word']} -> {tp['id']}:{b['word']}"
            for a, b in zip(sp["_top_concepts"][:k], tp["_top_concepts"][:k])]


def _shell_hierarchy(parts, cmat, ids):
    labels = {1: "Foundation", 2: "Processing", 3: "Integration", 4: "Context",
              5: "Abstraction", 6: "Meta", 7: "CrossSystem", 8: "Structural",
              9: "GrandMaster", 10: "Apex"}
    levels = {}
    for p in sorted(parts, key=lambda x: x["shell"]):
        levels.setdefault(str(p["shell"]), {"label": labels.get(p["shell"], f"Shell{p['shell']}"),
                                            "description": f"parts whose master node sits on shell {p['shell']}",
                                            "parts_dominant": []})
        levels[str(p["shell"])]["parts_dominant"].append(f"{p['id']}.{p['dom']}")
    grads = []
    seen = set()
    for a in range(len(ids)):
        for b in range(len(ids)):
            if a == b:
                continue
            hi = max(parts[a]["shell"], parts[b]["shell"])
            lo = min(parts[a]["shell"], parts[b]["shell"])
            if hi > lo and cmat[ids[a]][ids[b]]["c"] >= 0.34 and (hi, lo) not in seen:
                seen.add((hi, lo))
                grads.append({"from_shell": hi, "to_shell": lo,
                              "I": round(1.0 / lo - 1.0 / hi, 3),
                              "parts": f"shell {hi} parts provide downward context to shell {lo}"})
    return {"formula": "I(λ_h,λ_l) = 1/λ_l − 1/λ_h  where λ_h > λ_l",
            "semantics": "higher shells exert downward context influence on lower shells; information flows λ_high → λ_low",
            "levels": levels, "key_influence_gradients": grads[:8]}


def _phase_bands(parts, cmat, ids):
    names = ["α_band_[0,60)", "β_band_[60,120)", "γ_band_[120,180)",
             "δ_band_[180,240)", "ε_band_[240,300)", "ζ_band_[300,360)"]
    bands = {}
    id_of = {p["id"]: p for p in parts}
    for bi, name in enumerate(names):
        lo = bi * 60
        members = [p["id"] for p in parts if lo <= p["theta"] < lo + 60]
        entry = {"representative_theta": lo + 30, "parts": members}
        if len(members) >= 2:
            cs = [cmat[members[i]][members[j]]["c"]
                  for i in range(len(members)) for j in range(i + 1, len(members))]
            entry["c_inter"] = round(sum(cs) / len(cs), 2)
            doms = Counter(id_of[m]["dom"] for m in members)
            entry["semantic"] = "_".join(d.lower() for d, _ in doms.most_common(3))
        bands[name] = entry
    return {"description": "Parts whose dominant θ falls in the same 60° arc share phase resonance and exhibit strongest natural coupling",
            "bands": bands}


# --------------------------------------- verbatim schema-description blocks
_TOP_LEVEL_FIELDS = {
    "library": "string — library name", "file": "string — canonical output filename",
    "schema": "wpe-5.0-xmap-1.0", "kind": "cross_coupling_map",
    "parts": "object keyed by part ID (P1…PN); each entry is a part_shape",
    "domains_table": "object mapping domain code -> {shell, type, description, parts}",
    "coupling_tiers": "object defining 6 tier levels by cos(Δθ) range",
    "cross_coupling_index": "array of detailed coupling records between specific interface components",
    "coupling_matrix": "symmetric N×N matrix of {c, delta_theta, tier} for every part pair",
    "shell_hierarchy": "object defining shell levels, influence formula, and key gradients",
    "phase_resonance_bands": "6 phase bands (60° each) with resident parts and inter-part c",
    "consistency_invariants": "list of invariants that must hold across all entries",
    "lookup_paths": "navigation guide for common query patterns",
}
_PART_SHAPE = {
    "id": "string — canonical part identifier e.g. P1, P4",
    "label": "string — human-readable title", "filename": "string — source filename/URL",
    "shell": "int — dominant WPE shell (min over the part's domain shells)",
    "theta": "float — angular coordinate of the part's master output node [0,359]",
    "kappa": "float — curvature of the master node (κ < 0 = stable well)",
    "addr": "string — '<shell>@<theta>' unique address; used in ptr fields",
    "dom": "string — primary domain code for this part",
    "domain_codes": "list[string] — all domain codes the part resolves into",
    "ptr_sequence": "list[addr] — parts this part FEEDS-INTO (directed)",
    "ptr_coupling": "list[addr] — parts bidirectionally coupled (c ≥ 0.34)",
    "ptr_orthogonal": "list[addr] — weakly related parts (-0.09 ≤ c < 0.34)",
    "ptr_opposition": "list[addr] — parts in active tension (c < -0.09)",
    "semantic_role": "string — plain-language role of this part",
    "curation": {"coupling_strength": "central | partial | weak",
                 "orthogonal_status": "filled | partial | empty",
                 "oppositional_status": "filled | empty | not_applicable",
                 "needs_curation": "bool", "auto_sources": "list[string]",
                 "sufficiency_score": "float 0.0-1.0"},
}
_REFERENCE_GRAPH = {
    "ptr_sequence": {"operator": "*", "direction": "outgoing, directed",
                     "semantics": "this part FEEDS-INTO / IS-PREREQUISITE-FOR the target part",
                     "cardinality": "0..N"},
    "ptr_coupling": {"operator": "coupling", "direction": "bidirectional",
                     "semantics": "this part IS-BIDIRECTIONALLY-COUPLED-WITH the target",
                     "cardinality": "0..N"},
    "ptr_orthogonal": {"operator": "parallel (+) or none", "direction": "outgoing reference",
                       "semantics": "this part IS-ON-A-DIFFERENT-AXIS-FROM but still relates to target",
                       "cardinality": "0..5"},
    "ptr_opposition": {"operator": "coupling (tension)", "direction": "bidirectional",
                       "semantics": "this part IS-IN-ACTIVE-TENSION-WITH the target; cos(Δθ) < -0.09",
                       "cardinality": "0..N (list)"},
}
_INVARIANTS = [
    "part.addr == '<part.shell>@<part.theta>'",
    "part.shell == min(domains_table[d].shell for d in part.domain_codes)",
    "every addr in ptr_sequence / ptr_coupling / ptr_orthogonal / ptr_opposition must resolve to an existing part.addr",
    "ptr_opposition is a list of strings (may be empty); entries are addrs of parts with cos(Δθ) < -0.09",
    "ptr_orthogonal has entries with -0.09 ≤ cos(Δθ) < 0.34 (WEAK or ORTHOGONAL tier)",
    "coupling_matrix[Pi][Pj].c == coupling_matrix[Pj][Pi].c  (symmetric)",
    "coupling_matrix[Pi][Pi].c == 1.0 for all Pi (self-coupling = 1)",
    "coupling_matrix[Pi][Pj].tier == tier_from_c(coupling_matrix[Pi][Pj].c)",
    "every cross_coupling_index entry's source_part and target_part must be in parts",
    "interface_coupling_strength = cos(interface_delta_theta)",
    "shell_influence.value = 1/lambda_low − 1/lambda_high when lambda_high > lambda_low; else 0",
    "curation.orthogonal_status='filled' implies len(ptr_orthogonal) >= 3",
    "curation.orthogonal_status='partial' implies 1 <= len(ptr_orthogonal) <= 2",
    "sufficiency_score ∈ [0.0, 1.0]; score < 0.90 implies needs_curation = true",
    "operator ∈ {'sequence','parallel','coupling'}; 'coupling' implies bidirectional; 'sequence' implies directed",
]
_LOOKUP_PATHS = {
    "part_by_id": "doc.parts[<PART_ID>]  e.g. doc.parts['P4']",
    "part_by_addr": "next(p for p in doc.parts.values() if p['addr']==addr)",
    "coupling": "doc.coupling_matrix['Pi']['Pj']  — always symmetric",
    "xc_detail": "doc.cross_coupling_index[i] filtered by source_part/target_part",
    "strongest_couplings": "sorted(doc.coupling_matrix[pi].items(), key=lambda x: x[1]['c'], reverse=True)",
    "parts_in_tier": "[(pi,pj) for pi in parts for pj in parts if coupling_matrix[pi][pj]['tier']==<TIER>]",
    "domain_to_part": "doc.domains_table[<DOM>]['parts']",
    "shell_peers": "[p for p in doc.parts.values() if p['shell']==<SHELL>]",
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sources", nargs="+", help="files/URLs; each is one part, or one source split by --split")
    ap.add_argument("--split", default="whole", choices=["whole", "sections", "paragraphs"])
    ap.add_argument("--min-chars", type=int, default=800,
                    help="minimum characters per section when --split sections")
    ap.add_argument("--lex", nargs="+", default=["libs/scimed_dict_lib.json", "libs/word_dict_75k_lib.json"])
    ap.add_argument("--library", default="ProseWorld.XMap")
    ap.add_argument("--coupling", default="geometric",
                    choices=["geometric", "semantic"],
                    help="geometric = c=cos(Δ master-θ) [WPE default]; "
                         "semantic = concept-vector cosine")
    ap.add_argument("--out", default="xmap.json")
    args = ap.parse_args(argv)

    print(f"loading lexicons {args.lex}", file=sys.stderr)
    ix = LexiconIndex()
    for p in args.lex:
        ix.load(p)
    enc = Encoder(ix)

    encoded = []
    for src in args.sources:
        doc = extract.extract(src)
        for label, text in split_snippets(doc["text"], args.split, args.min_chars):
            meta = dict(doc["meta"]); meta["snippet_label"] = label
            meta["doc_title"] = doc["meta"].get("title")
            if len(text.strip()) < 80:
                continue
            part_label = label if args.split != "whole" else (doc["meta"].get("title") or label)
            encoded.append(encode_part(ix, enc, part_label, text, meta))
            print(f"  part {len(encoded)}: {label[:40]!r} "
                  f"shell={encoded[-1][0]['shell']} theta={encoded[-1][0]['theta']} "
                  f"dom={encoded[-1][0]['dom']}", file=sys.stderr)

    if len(encoded) < 2:
        print("need >= 2 snippets to build a coupling map (use --split)", file=sys.stderr)
        return
    xmap = build_xmap(ix, encoded, source_count=len(args.sources),
                      library=args.library, coupling=args.coupling)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(xmap, fh, ensure_ascii=False, indent=2)
    print(f"\nparts={xmap['unique_part_count']} pairs={xmap['coupling_pairs_total']} "
          f"detail={xmap['coupling_pairs_with_detail']} -> {args.out}")


if __name__ == "__main__":
    main()
