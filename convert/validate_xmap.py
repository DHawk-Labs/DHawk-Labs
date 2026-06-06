#!/usr/bin/env python3
"""
validate_xmap.py — independent checker for a wpe-5.0-xmap-1.0 cross-coupling map.

Asserts every consistency_invariant declared by the schema (and structural
sanity) with no shared code from the generator, then prints a report and exits
non-zero on any violation. Use it to gate a generated map.

    python convert/validate_xmap.py xmap.json
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter

ADDR_RE = re.compile(r"^\d+@\d+(\.\d+)?$")
TOL = 0.02


def tier_from_c(c):
    # SELF is positional (matrix diagonal only); distinct parts at the same
    # phase are SYNERGISTIC, not SELF.
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


def validate(path):
    errs = Counter()
    samples = []

    def err(code, msg):
        errs[code] += 1
        if len(samples) < 40:
            samples.append(f"[{code}] {msg}")

    doc = json.load(open(path, encoding="utf-8"))
    if doc.get("schema") != "wpe-5.0-xmap-1.0":
        err("SCHEMA", f"schema={doc.get('schema')!r}")
    parts = doc.get("parts", {})
    domains = doc.get("domains_table", {})
    cm = doc.get("coupling_matrix", {}).get("values", {})
    print(f"loaded {len(parts)} parts from {path}")

    addr_to_pid = {}
    addrs = set()
    for pid, p in parts.items():
        # inv: addr == shell@theta
        if p.get("addr") != f"{p.get('shell')}@{p.get('theta')}":
            err("ADDR_FORM", f"{pid}: addr {p.get('addr')} != {p.get('shell')}@{p.get('theta')}")
        if not ADDR_RE.match(str(p.get("addr", ""))):
            err("ADDR_FMT", f"{pid}: bad addr {p.get('addr')}")
        if p.get("addr") in addrs:
            err("ADDR_DUP", f"{pid}: duplicate addr {p.get('addr')}")
        addrs.add(p.get("addr"))
        addr_to_pid[p.get("addr")] = pid
        # inv: shell == min(domains_table[d].shell for d in domain_codes)
        dcs = [d for d in p.get("domain_codes", []) if d in domains]
        if dcs:
            mn = min(domains[d]["shell"] for d in dcs)
            if p.get("shell") != mn:
                err("SHELL_MIN", f"{pid}: shell {p.get('shell')} != min domain shell {mn}")
        # curation
        cur = p.get("curation", {})
        no = len(p.get("ptr_orthogonal", []))
        if cur.get("orthogonal_status") == "filled" and no < 3:
            err("ORTH_FILLED", f"{pid}: orthogonal_status filled but len={no}")
        if cur.get("orthogonal_status") == "partial" and not (1 <= no <= 2):
            err("ORTH_PARTIAL", f"{pid}: orthogonal_status partial but len={no}")
        ss = cur.get("sufficiency_score")
        if not isinstance(ss, (int, float)) or not (0.0 <= ss <= 1.0):
            err("SUFFIC", f"{pid}: sufficiency_score={ss}")
        elif ss < 0.90 and not cur.get("needs_curation"):
            err("NEEDS_CUR", f"{pid}: score {ss} < 0.90 but needs_curation false")

    # pointer resolution + tier-range invariants
    for pid, p in parts.items():
        for field in ("ptr_sequence", "ptr_coupling", "ptr_orthogonal", "ptr_opposition"):
            v = p.get(field, [])
            if not isinstance(v, list):
                err("PTR_TYPE", f"{pid}.{field} not a list")
                continue
            for a in v:
                if a not in addrs:
                    err("DANGLING", f"{pid}.{field} -> {a} not a part addr")
                    continue
                qid = addr_to_pid[a]
                c = cm.get(pid, {}).get(qid, {}).get("c")
                if c is None:
                    continue
                if field == "ptr_orthogonal" and not (-0.09 <= c < 0.34):
                    err("ORTH_RANGE", f"{pid}.ptr_orthogonal -> {qid} c={c} out of [-0.09,0.34)")
                if field == "ptr_opposition" and not (c < -0.09):
                    err("OPP_RANGE", f"{pid}.ptr_opposition -> {qid} c={c} not < -0.09")

    # coupling matrix invariants
    ids = list(parts.keys())
    for i in ids:
        if abs(cm.get(i, {}).get(i, {}).get("c", 0) - 1.0) > 1e-9:
            err("SELF_C", f"{i}: diagonal c != 1.0")
        for j in ids:
            cell = cm.get(i, {}).get(j)
            if not cell:
                err("CM_MISSING", f"missing coupling_matrix[{i}][{j}]")
                continue
            cji = cm.get(j, {}).get(i, {}).get("c")
            if cji is not None and abs(cell["c"] - cji) > 1e-6:
                err("CM_ASYM", f"[{i}][{j}].c={cell['c']} != [{j}][{i}].c={cji}")
            expect = "SELF" if i == j else tier_from_c(cell["c"])
            if cell.get("tier") != expect:
                err("TIER", f"[{i}][{j}] tier {cell.get('tier')} != {expect}")
            # c == cos(delta_theta)
            dt = cell.get("delta_theta")
            if dt is not None and abs(cell["c"] - math.cos(math.radians(dt))) > TOL:
                err("C_COS", f"[{i}][{j}] c={cell['c']} != cos({dt})")

    # cross_coupling_index invariants
    for x in doc.get("cross_coupling_index", []):
        if x.get("source_part") not in parts or x.get("target_part") not in parts:
            err("XC_PART", f"{x.get('id')}: source/target not in parts")
        idt = x.get("interface_delta_theta")
        ics = x.get("interface_coupling_strength")
        if idt is not None and ics is not None:
            if abs(ics - math.cos(math.radians(idt))) > TOL:
                err("XC_COS", f"{x.get('id')}: ics {ics} != cos({idt})")
        si = x.get("shell_influence", {})
        hi, lo, val = si.get("lambda_high"), si.get("lambda_low"), si.get("value")
        if None not in (hi, lo, val):
            exp = (1.0 / lo - 1.0 / hi) if hi > lo else 0.0
            if abs(val - exp) > TOL:
                err("XC_SHELL", f"{x.get('id')}: shell_influence {val} != {round(exp,4)}")
        if x.get("operator") not in ("sequence", "parallel", "coupling"):
            err("OPERATOR", f"{x.get('id')}: operator {x.get('operator')}")

    print("\n=== XMAP INVARIANT REPORT ===")
    total = sum(errs.values())
    if not total:
        print(f"PASSED: {len(parts)} parts, {len(ids)*(len(ids)-1)//2} pairs — all invariants hold.")
        return 0
    for code, n in sorted(errs.items()):
        print(f"  {code:14s} {n}")
    print("\nsamples:")
    for s in samples:
        print("  " + s)
    print(f"\nFAILED: {total} violation(s).")
    return 1


if __name__ == "__main__":
    sys.exit(validate(sys.argv[1] if len(sys.argv) > 1 else "xmap.json"))
