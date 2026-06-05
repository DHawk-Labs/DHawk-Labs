#!/usr/bin/env python3
"""
validate_lexicon.py — independent invariant checker for a lexicon-1.0 file.

This reads the generated JSON with *no shared logic* from build_lexicon.py and
asserts every consistency invariant declared in word_dict_structure.json, plus
structural/type checks. It prints a full report and exits non-zero on any
violation, so it can gate a build ("100% correct, not mostly correct").

Usage:
    python3 tools/validate_lexicon.py libs/word_dict_75k_lib.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter

VALID_DEF_STRENGTH = {"central", "partial", "weak"}
VALID_ORTH_STATUS = {"filled", "partial", "empty"}
VALID_OPP_STATUS = {"filled", "empty", "not_applicable"}
ADDR_RE = re.compile(r"^\d+@\d+(\.\d+)?$")
# Lowercase alphanumeric tokens joined by - ' _ . Digits are permitted because
# scientific/medical terms need them (covid-19, vitamin-b12, interleukin-6);
# the general dictionary simply never produces any, so this stays valid there.
WORD_RE = re.compile(r"^[a-z0-9]+(?:[-'_][a-z0-9]+)*$")

# POS classes that legitimately cannot take an antonym (schema invariant text:
# proper nouns, mass nouns, numerals — plus other non-gradable closed classes).
NO_ANTONYM_DOMS = {
    "NOUNP", "NOUNM", "NUM", "PRON", "DET", "CONJ", "AUX", "META", "PRIME",
}


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.counts = Counter()

    def err(self, code, msg):
        self.counts[code] += 1
        if len(self.errors) < 50:
            self.errors.append(f"[{code}] {msg}")

    def warn(self, code, msg):
        self.counts["W:" + code] += 1
        if len(self.warnings) < 25:
            self.warnings.append(f"[{code}] {msg}")


def validate(path):
    r = Report()
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    # ---- top-level structure -------------------------------------------
    for field in ("library", "schema", "kind", "domains", "entries",
                  "descriptor_protocol", "exports"):
        if field not in doc:
            r.err("TOP_MISSING", f"top-level field '{field}' missing")
    if doc.get("schema") != "lexicon-1.0":
        r.err("SCHEMA", f"schema is {doc.get('schema')!r}, expected 'lexicon-1.0'")
    if doc.get("kind") != "lexicon":
        r.err("KIND", f"kind is {doc.get('kind')!r}, expected 'lexicon'")

    domains = doc.get("domains", {})
    shell_of = {}
    for dom, meta in domains.items():
        if "shell" not in meta:
            r.err("DOM_SHELL", f"domain {dom} has no shell")
        else:
            shell_of[dom] = meta["shell"]

    entries = doc.get("entries", {})
    print(f"loaded {len(entries)} entries from {path}")

    # ---- per-entry checks ----------------------------------------------
    addr_to_key = {}
    seen_word_dom = set()
    all_addrs = set()

    # first pass: collect addresses + structural checks
    for key, e in entries.items():
        word = e.get("word")
        dom = e.get("dom")
        shell = e.get("shell")
        theta = e.get("theta")
        addr = e.get("addr")

        # key format: "<DOM>.<word>"
        if "." not in key:
            r.err("KEY_FMT", f"key {key!r} not '<DOM>.<word>'")
        else:
            kdom, kword = key.split(".", 1)
            if kdom != dom:
                r.err("KEY_DOM", f"key {key!r} dom != entry.dom {dom!r}")
            if kword != word:
                r.err("KEY_WORD", f"key {key!r} word != entry.word {word!r}")

        # types
        if not isinstance(word, str) or not word:
            r.err("WORD_TYPE", f"{key}: word not a non-empty string")
        elif word != word.lower():
            r.err("WORD_CASE", f"{key}: word {word!r} not lowercase")
        elif not WORD_RE.match(word):
            r.err("WORD_NOISE", f"{key}: word {word!r} contains noise")
        if not isinstance(shell, int):
            r.err("SHELL_TYPE", f"{key}: shell not int")
        if not isinstance(theta, (int, float)):
            r.err("THETA_TYPE", f"{key}: theta not numeric")

        # invariant: addr == "<shell>@<theta>"
        expect_addr = f"{shell}@{theta}"
        if addr != expect_addr:
            r.err("ADDR_FORM", f"{key}: addr {addr!r} != '{expect_addr}'")
        if not isinstance(addr, str) or not ADDR_RE.match(str(addr)):
            r.err("ADDR_FMT", f"{key}: addr {addr!r} malformed")

        # invariant: shell == domains_table[dom].shell
        if dom in shell_of and shell != shell_of[dom]:
            r.err("SHELL_DOM",
                  f"{key}: shell {shell} != domain {dom} shell {shell_of[dom]}")

        # invariant: word+dom unique
        wd = (word, dom)
        if wd in seen_word_dom:
            r.err("WORD_DOM_DUP", f"{key}: (word,dom) {wd} duplicated")
        seen_word_dom.add(wd)

        # invariant: addr unique
        if addr in all_addrs:
            r.err("ADDR_DUP", f"{key}: addr {addr} duplicated "
                  f"(also {addr_to_key.get(addr)})")
        all_addrs.add(addr)
        addr_to_key[addr] = key

    # second pass: pointer + curation invariants (need full addr set)
    for key, e in entries.items():
        ptr = e.get("ptr")
        orth = e.get("ptr_orthogonal")
        opp = e.get("ptr_oppositional")
        cur = e.get("curation", {})
        dom = e.get("dom")
        self_addr = e.get("addr")

        # types / shapes
        if not isinstance(ptr, list):
            r.err("PTR_TYPE", f"{key}: ptr not a list")
            ptr = []
        if not isinstance(orth, list):
            r.err("ORTH_TYPE", f"{key}: ptr_orthogonal not a list")
            orth = []
        # invariant: ptr_oppositional is a single string (or '')
        if not isinstance(opp, str):
            r.err("OPP_TYPE", f"{key}: ptr_oppositional must be a string")
            opp = ""

        # invariant: ptr_orthogonal length <= 5
        if len(orth) > 5:
            r.err("ORTH_LEN", f"{key}: ptr_orthogonal len {len(orth)} > 5")

        # invariant: every referenced addr resolves; none point to self
        for label, lst in (("ptr", ptr), ("ptr_orthogonal", orth)):
            for a in lst:
                if a not in all_addrs:
                    r.err("DANGLING", f"{key}: {label} addr {a!r} dangling")
                if a == self_addr:
                    r.err("SELF_PTR", f"{key}: {label} points to self")
            if len(set(lst)) != len(lst):
                r.err("PTR_DUP", f"{key}: {label} has duplicate addrs")
        if opp:
            if opp not in all_addrs:
                r.err("DANGLING", f"{key}: ptr_oppositional addr {opp!r} dangling")
            if opp == self_addr:
                r.err("SELF_PTR", f"{key}: ptr_oppositional points to self")

        # ---- curation field validity ----
        ds = cur.get("definition_strength")
        os_ = cur.get("orthogonal_status")
        ops = cur.get("oppositional_status")
        if ds not in VALID_DEF_STRENGTH:
            r.err("CUR_DEF", f"{key}: definition_strength {ds!r} invalid")
        if os_ not in VALID_ORTH_STATUS:
            r.err("CUR_ORTH", f"{key}: orthogonal_status {os_!r} invalid")
        if ops not in VALID_OPP_STATUS:
            r.err("CUR_OPP", f"{key}: oppositional_status {ops!r} invalid")
        if not isinstance(cur.get("needs_curation"), bool):
            r.err("CUR_NEEDS", f"{key}: needs_curation not bool")
        if not isinstance(cur.get("auto_sources"), list):
            r.err("CUR_SRC", f"{key}: auto_sources not a list")
        suf = cur.get("sufficiency_score")
        if not isinstance(suf, (int, float)) or not (0.0 <= suf <= 1.0):
            r.err("CUR_SUF", f"{key}: sufficiency_score {suf!r} out of [0,1]")

        # ---- status<->cardinality invariants ----
        if os_ == "filled" and len(orth) < 3:
            r.err("INV_ORTH_FILLED",
                  f"{key}: orthogonal_status filled but len {len(orth)} < 3")
        if os_ == "partial" and not (1 <= len(orth) <= 2):
            r.err("INV_ORTH_PARTIAL",
                  f"{key}: orthogonal_status partial but len {len(orth)}")
        if os_ == "empty" and len(orth) != 0:
            r.err("INV_ORTH_EMPTY",
                  f"{key}: orthogonal_status empty but len {len(orth)}")
        if ops == "filled" and opp == "":
            r.err("INV_OPP_FILLED",
                  f"{key}: oppositional_status filled but ptr_oppositional ''")
        if ops == "not_applicable" and dom not in NO_ANTONYM_DOMS:
            r.err("INV_OPP_NA",
                  f"{key}: oppositional_status not_applicable but dom {dom} "
                  f"can take antonyms")
        if ops == "empty" and opp != "":
            r.err("INV_OPP_EMPTY",
                  f"{key}: oppositional_status empty but ptr_oppositional set")

    # ---- summary --------------------------------------------------------
    print("\n=== INVARIANT REPORT ===")
    if not r.counts:
        print("entries checked, zero issues.")
    for code, n in sorted(r.counts.items()):
        print(f"  {code:20s} {n}")
    if r.warnings:
        print("\n-- sample warnings --")
        for w in r.warnings:
            print("  " + w)
    total_err = sum(v for k, v in r.counts.items() if not k.startswith("W:"))
    if total_err:
        print(f"\n-- sample errors (first {len(r.errors)}) --")
        for e in r.errors:
            print("  " + e)
        print(f"\nFAILED: {total_err} invariant violation(s).")
        return 1
    print(f"\nPASSED: {len(entries)} entries satisfy all invariants.")
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "libs/word_dict_75k_lib.json"
    sys.exit(validate(path))
