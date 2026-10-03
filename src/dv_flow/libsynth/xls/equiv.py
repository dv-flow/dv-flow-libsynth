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


async def Equiv(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    tool = find_tool("check_ir_equivalence_main")
    if tool is None:
        return TaskDataResult(status=1, changed=False, output=[], markers=[TaskMarker(
            msg="check_ir_equivalence_main not found on PATH or in $XLS_BIN",
            severity=SeverityE.Error)])
    p = input.params
    irs = _packages(input)
    if len(irs) != 2:
        return TaskDataResult(status=1, changed=False, output=[], markers=[TaskMarker(
            msg=f"Equiv compares two IR packages; got {len(irs)}: "
                f"{[os.path.basename(i) for i in irs]}", severity=SeverityE.Error)])
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    log = os.path.join(rundir, "equiv.log")
    cmd = [tool, irs[0], irs[1]]
    if p.timeout_s > 0:
        cmd.append(f"--timeout={p.timeout_s}s")
    t0 = time.monotonic()
    st = await ctxt.exec(cmd, logfile=log, cwd=rundir)
    secs = round(time.monotonic() - t0, 2)
    with open(log, errors="replace") as f:
        v, detail = verdict(f.read())

    def name(path):
        with open(path) as f:
            return f"{os.path.basename(path)} ({ir_top(f.read())})"
    what = f"{name(irs[0])} vs {name(irs[1])}"
    markers: List[TaskMarker] = []
    status = 0
    if v == "equivalent":
        ctxt.info(f"equivalent: {what} ({secs}s)")
    elif v == "not-equivalent":
        markers.append(TaskMarker(msg=f"NOT equivalent: {what}; {detail}",
                                  severity=SeverityE.Error if p.gate else SeverityE.Warning))
        status = 1 if p.gate else 0
    elif v == "error":
        markers.append(TaskMarker(msg=f"cannot compare {what}: {detail}",
                                  severity=SeverityE.Error))
        status = 1
    else:
        markers.append(TaskMarker(msg=f"inconclusive after {secs}s: {what}",
                                  severity=SeverityE.Error if p.require_proof
                                  else SeverityE.Warning))
        status = 1 if p.require_proof else 0
    with open(os.path.join(rundir, "equiv.json"), "w") as f:
        json.dump({"lhs": irs[0], "rhs": irs[1], "verdict": v, "detail": detail,
                   "seconds": secs, "exit": st}, f, indent=2)
    return TaskDataResult(status=status, changed=True, markers=markers, output=[
        FileSet(src=input.name, filetype="xlsEquivReport", basedir=rundir,
                files=["equiv.json"], attributes=[f"verdict={v}"])])
