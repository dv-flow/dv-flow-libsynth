"""synth.xls.Equiv: prove two XLS IR functions equivalent (check_ir_equivalence_main).

The tool prints one of:

    Verified equivalent
    Verified NOT equivalent; results differ for input: bits[7]:0x0
    Error: INVALID_ARGUMENT: Cannot prove equivalence of functions with differing ...

and is killed at `--timeout`, which leaves no verdict.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional, Tuple

from dv_flow.mgr import (FileSet, SeverityE, TaskDataInput, TaskDataResult, TaskMarker,
                         TaskRunCtxt)

from .codegen import find_tool, ir_top

_NOT = re.compile(r"Verified NOT equivalent;?\s*(.*)")
_ERR = re.compile(r"^Error: (.*)$", re.M)


def verdict(text: str) -> Tuple[str, str]:
    """(equivalent | not-equivalent | error | inconclusive, detail)."""
    if "Verified equivalent" in text:
        return "equivalent", ""
    m = _NOT.search(text)
    if m:
        return "not-equivalent", m.group(1).strip()
    m = _ERR.search(text)
    if m:
        return "error", m.group(1).strip()
    return "inconclusive", "no verdict (the solver timed out or was stopped)"


def _packages(input: TaskDataInput) -> List[str]:
    """One IR file per producing task: its optimized IR if it has one."""
    by_src = {}
    for fs in getattr(input, "inputs", []) or []:
        ft = getattr(fs, "filetype", None)
        if ft not in ("xlsOptIR", "xlsIR"):
            continue
        files = [os.path.join(fs.basedir, f) for f in fs.files]
        cur = by_src.get(fs.src)
        if cur is None or (ft == "xlsOptIR" and cur[0] != "xlsOptIR"):
            by_src[fs.src] = (ft, files)
    out = []
    for _, files in by_src.values():
        out += files
    return out


def _by_top(input: TaskDataInput):
    """IR top function -> IR file: each package's optimized IR when its
    producer has one, else its IR."""
    best = {}
    for fs in getattr(input, "inputs", []) or []:
        ft = getattr(fs, "filetype", None)
        if ft not in ("xlsOptIR", "xlsIR"):
            continue
        for f in fs.files:
            path = os.path.join(fs.basedir, f)
            with open(path) as fp:
                top = ir_top(fp.read())
            if top and (top not in best or (ft == "xlsOptIR" and best[top][0] != "xlsOptIR")):
                best[top] = (ft, path)
    return {t: p for t, (_, p) in best.items()}


_RANK = {"equivalent": 0, "inconclusive": 1, "not-equivalent": 2, "error": 3}


async def Equiv(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    tool = find_tool("check_ir_equivalence_main")
    if tool is None:
        return TaskDataResult(status=1, changed=False, output=[], markers=[TaskMarker(
            msg="check_ir_equivalence_main not found on PATH or in $XLS_BIN",
            severity=SeverityE.Error)])
    p = input.params

    def fail(msg):
        return TaskDataResult(status=1, changed=False, output=[], markers=[TaskMarker(
            msg=msg, severity=SeverityE.Error)])

    if p.pairs:
        tops = _by_top(input)
        pairs = []
        for spec in p.pairs:
            lhs, sep, rhs = spec.partition("=")
            if not sep:
                return fail(f"Equiv: pair {spec!r} is not `<top>=<top>`")
            missing = [t for t in (lhs.strip(), rhs.strip()) if t not in tops]
            if missing:
                return fail(f"Equiv: no IR package with top {missing}; have {sorted(tops)}")
            pairs.append((tops[lhs.strip()], tops[rhs.strip()]))
    else:
        irs = _packages(input)
        if len(irs) != 2:
            return fail(f"Equiv compares two IR packages (or give `pairs`); got {len(irs)}: "
                        f"{[os.path.basename(i) for i in irs]}")
        pairs = [(irs[0], irs[1])]

    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    markers: List[TaskMarker] = []
    results, status = [], 0

    def name(path):
        with open(path) as f:
            return f"{os.path.basename(path)} ({ir_top(f.read())})"

    for n, (lhs, rhs) in enumerate(pairs):
        log = os.path.join(rundir, f"equiv{n}.log")
        cmd = [tool, lhs, rhs]
        if p.timeout_s > 0:
            cmd.append(f"--timeout={p.timeout_s}s")
        t0 = time.monotonic()
        st = await ctxt.exec(cmd, logfile=log, cwd=rundir)
        secs = round(time.monotonic() - t0, 2)
        with open(log, errors="replace") as f:
            v, detail = verdict(f.read())
        what = f"{name(lhs)} vs {name(rhs)}"
        if v == "equivalent":
            ctxt.info(f"equivalent: {what} ({secs}s)")
        elif v == "not-equivalent":
            markers.append(TaskMarker(msg=f"NOT equivalent: {what}; {detail}",
                                      severity=SeverityE.Error if p.gate else SeverityE.Warning))
            status |= 1 if p.gate else 0
        elif v == "error":
            markers.append(TaskMarker(msg=f"cannot compare {what}: {detail}",
                                      severity=SeverityE.Error))
            status = 1
        else:
            markers.append(TaskMarker(msg=f"inconclusive after {secs}s: {what}",
                                      severity=SeverityE.Error if p.require_proof
                                      else SeverityE.Warning))
            status |= 1 if p.require_proof else 0
        results.append({"lhs": lhs, "rhs": rhs, "verdict": v, "detail": detail,
                        "seconds": secs, "exit": st})
    worst = max((r["verdict"] for r in results), key=_RANK.get)
    report = dict(results[0]) if len(results) == 1 else {"verdict": worst, "pairs": results}
    with open(os.path.join(rundir, "equiv.json"), "w") as f:
        json.dump(report, f, indent=2)
    return TaskDataResult(status=status, changed=True, markers=markers, output=[
        FileSet(src=input.name, filetype="xlsEquivReport", basedir=rundir,
                files=["equiv.json"], attributes=[f"verdict={worst}"])])
