#!/usr/bin/env python3
"""Undefined-name audit for the robot modules (pyflakes-lite, stdlib-only).

WHY THIS EXISTS: the P3 seam extraction moved code out of follow_person_k1.py but three modules
lost their stdlib imports (identity: collections/os, perception: threading/time, triggers:
collections/os/time). py_compile passed (it doesn't execute) and the replay gate passed (the
no-person clip never instantiates GestureTrigger or loads the real ReID engine), so the NameError
only fired ON THE ROBOT at Follower.__init__ (2026-07-07 crash). This static pass catches that
whole class offline.

Flat-scope approximation: collect every name DEFINED anywhere in the module (imports incl.
aliases, assignment/for/with/except targets, def/class names, function params, comprehension
vars, globals) plus builtins; report every Load-context Name not in that set. Flat scoping
cannot false-positive on the missing-module-import class -- only false-negative on
cross-function locals, which are defined somewhere so they don't fire.

Usage:  python import_audit.py <module.py> [more.py ...]     exits 1 on any undefined name
        (run from robot/:  python ../eval/import_audit.py *.py)
"""
import ast, builtins, sys


def defined_names(tree):
    d = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__package__", "__builtins__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                d.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            d.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            d.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            d.update(n.names)
        elif isinstance(n, ast.arg):
            d.add(n.arg)
    return d


def main(paths):
    if not paths:
        raise SystemExit("usage: import_audit.py <module.py> [more.py ...]")
    bad = 0
    for p in paths:
        with open(p, encoding="utf-8-sig") as f:   # -sig: BOM-tolerant (Windows tooling adds BOMs)
            tree = ast.parse(f.read(), p)
        d = defined_names(tree)
        hits = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in d:
                hits.setdefault(n.id, []).append(n.lineno)
        for name, lines in sorted(hits.items()):
            bad += 1
            print("%s: UNDEFINED %-16s lines %s" % (p, name, ",".join(map(str, lines[:8]))))
    print("IMPORT-AUDIT-%s (%d undefined name(s))" % ("FAIL" if bad else "OK", bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
