"""synth.yosys.Equiv: prove a netlist equivalent to the RTL it came from.

Yosys's own equivalence flow (`equiv_make`, `equiv_simple -seq`,
`equiv_induct -seq`, `equiv_status`): the RTL is the gold design, the netlist
(with its target's cell models, flattened into it) the gate design. Signals
are matched by name, so each register of the RTL is checked against the one
synthesis kept, and what is left is proven by induction. A full proof is
`equivalent`. Cells left unproven are searched for a difference from the
all-zero state with a bounded `sat` on a miter: a difference found is
`not-equivalent`, none is `inconclusive` (a match the flow could not make
is not a difference).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from dv_flow.mgr import (FileSet, SeverityE, TaskDataInput, TaskDataResult, TaskMarker,
                         TaskRunCtxt)

from ..execq import exec_expected
from .synth import find_yosys, log_markers

_RANK = {"equivalent": 0, "inconclusive": 1, "not-equivalent": 2, "error": 3}


@dataclass
class Side:
    files: List[Tuple[str, bool, List[str], List[str]]] = field(default_factory=list)
    #: (path, is SV, defines, incdirs)
    models: List[Tuple[str, List[str], List[str]]] = field(default_factory=list)
    #: cell models: (path, defines, incdirs)
    tops: List[str] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)


def _attrs(fs) -> dict:
    out = {}
    for a in getattr(fs, "attributes", None) or []:
        k, _, v = a.partition("=")
        out[k] = v
    return out


def split(input: TaskDataInput) -> Tuple[Side, Side]:
    """(gold, gate): the gate is the netlist `synth.yosys.Synth` outputs for
    simulation (its `format=verilog` netlist and its `yosysCellModels`); the
    gold is every other Verilog/SV input."""
    gold, gate = Side(), Side()
    for fs in getattr(input, "inputs", []) or []:
        if getattr(fs, "type", "std.FileSet") != "std.FileSet":
            continue
        if fs.filetype not in ("verilogSource", "systemVerilogSource"):
            continue
        a = _attrs(fs)
        defines = list(getattr(fs, "defines", []) or [])
        incdirs = [os.path.join(fs.basedir, d) for d in getattr(fs, "incdirs", []) or []]
        paths = [os.path.join(fs.basedir, f) for f in fs.files]
        if "yosysCellModels" in a:
            # Yosys's own internal cells (simcells.v) are not read: the
            # generic netlist's $_AND_ ... are Yosys's.
            gate.models += [(p, defines, incdirs) for p in paths
                            if os.path.basename(p) not in ("simcells.v", "simlib.v")]
        elif a.get("format") == "verilog" and "family" in a:
            gate.files += [(p, False, defines, incdirs) for p in paths]
            gate.attrs = a
            if a.get("top"):
                gate.tops.append(a["top"])
        else:
            gold.files += [(p, fs.filetype == "systemVerilogSource", defines, incdirs)
                           for p in paths]
            for k in ("top", "module"):
                if a.get(k):
                    gold.tops.append(a[k])
    return gold, gate


def _read(path, sv, defines, incdirs, extra="") -> str:
    return " ".join(x for x in ("read_verilog", extra, "-sv" if sv else "",
                                " ".join(f"-D{d}" for d in defines),
                                " ".join(f"-I{d}" for d in incdirs), path) if x)


def script(gold: Side, gate: Side, top: str, seq: int, gold_v: Optional[str]) -> str:
    """The equivalence script; *gold_v* is the gold design converted by sv2v."""
    lines = ([f"read_verilog {gold_v}"] if gold_v else
             [_read(*f) for f in gold.files])
    lines += [f"prep -top {top}", f"rename {top} gold", "design -stash gold"]
    # The cell models include their own .vh files, some twice: -overwrite.
    lines += [_read(p, False, d, i, "-overwrite") for p, d, i in gate.models]
    lines += [_read(*f) for f in gate.files]
    lines += [f"hierarchy -top {top}", "proc", "flatten", "opt_clean",
              f"rename {top} gate", "design -stash gate",
              "design -copy-from gold -as gold gold",
              "design -copy-from gate -as gate gate",
              "equiv_make gold gate equiv", "hierarchy -top equiv", "async2sync",
              f"equiv_simple -seq {seq}", f"equiv_induct -seq {seq}", "equiv_status"]
    return "\n".join(lines) + "\n"


def search_script(top: str, depth: int) -> str:
    """A bounded search for a difference, from the all-zero state, on the
    designs the equivalence script stashed (run after it, in the same Yosys)."""
    return "\n".join([
        "design -reset",
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
        "miter -equiv -flatten -make_outputs -ignore_gold_x gold gate miter",
        "hierarchy -top miter", "async2sync",
        f"sat -seq {depth} -prove trigger 0 -set-init-zero -show-inputs -show-outputs miter",
    ]) + "\n"


_STATUS = re.compile(r"Found (\d+) \$equiv cells in equiv:\s*\n\s*Of those cells (\d+) are "
                     r"proven and (\d+) are unproven")
_UNPROVEN = re.compile(r"^\s*Unproven \$equiv (\S+): (\S+) (\S+)", re.M)


def status(log: str) -> Optional[Tuple[int, int, int]]:
    """(cells, proven, unproven) from `equiv_status`."""
    m = None
    for m in _STATUS.finditer(log):
        pass
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _error(msg: str) -> TaskDataResult:
    return TaskDataResult(status=1, changed=False, output=[],
                          markers=[TaskMarker(msg=msg, severity=SeverityE.Error)])


def _text(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return ""


async def Equiv(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    yosys = find_yosys()
    if yosys is None:
        return _error("yosys not found on PATH")
    p = input.params
    gold, gate = split(input)
    if not gate.files:
        return _error("synth.yosys.Equiv: no netlist input (the verilogSource netlist of "
                      "synth.yosys.Synth, with sim_netlist)")
    if not gold.files:
        return _error("synth.yosys.Equiv: no RTL input (verilogSource / systemVerilogSource)")
    top = p.top or (gate.tops or gold.tops or [None])[0]
    if not top:
        return _error("synth.yosys.Equiv: no top; set `top`")
    if p.frontend not in ("auto", "yosys", "sv2v"):
        return _error(f"frontend {p.frontend!r}: one of auto, yosys, sv2v")
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    markers: List[TaskMarker] = []

    async def sv2v() -> Optional[str]:
        conv = shutil.which("sv2v") or os.path.join(os.path.dirname(yosys), "sv2v")
        if not os.access(conv, os.X_OK):
            markers.append(TaskMarker(msg="sv2v not found on PATH or beside yosys",
                                      severity=SeverityE.Error))
            return None
        out = os.path.join(rundir, "gold.sv2v.v")
        incs = list(dict.fromkeys(i for f in gold.files for i in f[3]))
        defs = list(dict.fromkeys(d for f in gold.files for d in f[2]))
        st = await ctxt.exec([conv, *[f"-I{d}" for d in incs], *[f"-D{d}" for d in defs],
                              *[f[0] for f in gold.files], f"--write={out}"],
                             logfile="sv2v.log", cwd=rundir)
        return out if st == 0 else None

    async def run(gold_v: Optional[str]) -> Tuple[int, str]:
        ys = os.path.join(rundir, "equiv.ys")
        with open(ys, "w") as f:
            f.write(script(gold, gate, top, p.seq, gold_v))
        log = os.path.join(rundir, "equiv.log")
        cmd = [yosys, "-q", "-l", log, ys]
        if p.timeout_s > 0 and shutil.which("timeout"):
            cmd = ["timeout", str(p.timeout_s)] + cmd
        st = await exec_expected(ctxt, cmd, logfile="yosys.out", cwd=rundir)
        return st, _text(log)

    t0 = time.monotonic()
    gold_v = None
    if p.frontend == "sv2v":
        gold_v = await sv2v()
        if gold_v is None:
            return TaskDataResult(status=1, changed=True, output=[], markers=markers)
    st, log = await run(gold_v)
    if st != 0 and status(log) is None and gold_v is None and p.frontend == "auto" \
            and any(f[1] for f in gold.files):
        c = await sv2v()
        if c is not None:
            st2, log2 = await run(c)
            if st2 == 0 or status(log2) is not None:
                markers.append(TaskMarker(msg="Yosys's SV reader failed on the RTL; read "
                                              "through sv2v instead", severity=SeverityE.Info))
                gold_v, st, log = c, st2, log2

    counts = status(log)
    detail, method = "", f"yosys equiv_simple/equiv_induct -seq {p.seq}"
    if counts is None:
        timed_out = st == 124 and p.timeout_s > 0
        v = "inconclusive" if timed_out else "error"
        detail = (f"no verdict after {p.timeout_s}s" if timed_out else
                  "; ".join(m.msg for m in log_markers(log, warnings=False)) or
                  f"yosys exited {st}")
    elif counts[2] == 0:
        v = "equivalent"
    else:
        unproven = [f"{a} vs {b}" for _, a, b in _UNPROVEN.findall(log)][:5]
        detail = f"{counts[2]} of {counts[0]} matched signals unproven ({', '.join(unproven)})"
        v = "inconclusive"
        if p.search_depth > 0:
            ys = os.path.join(rundir, "search.ys")
            with open(ys, "w") as f:
                f.write(script(gold, gate, top, p.seq, gold_v) + search_script(top, p.search_depth))
            slog = os.path.join(rundir, "search.log")
            await exec_expected(ctxt, [yosys, "-q", "-l", slog, ys], logfile="search.out",
                                cwd=rundir)
            text = _text(slog)
            if "SAT proof finished - model found: FAIL!" in text:
                v = "not-equivalent"
                detail += f"; a difference within {p.search_depth} cycles of the all-zero " \
                          f"state (see {slog})"
                method += f", sat -seq {p.search_depth}"
            elif "SAT proof finished - no model found: SUCCESS!" in text:
                detail += f"; no difference within {p.search_depth} cycles"
    secs = round(time.monotonic() - t0, 2)

    netlist = ", ".join(os.path.basename(f[0]) for f in gate.files)
    family = gate.attrs.get("target") or gate.attrs.get("family") or "?"
    what = f"{top}: RTL vs the {family} netlist ({netlist})"
    if v == "equivalent":
        ctxt.info(f"equivalent: {what}; {counts[0]} matched signals proven ({secs}s)")
    elif v == "not-equivalent":
        markers.append(TaskMarker(msg=f"NOT equivalent: {what}; {detail}",
                                  severity=SeverityE.Error if p.gate else SeverityE.Warning))
    elif v == "error":
        markers.append(TaskMarker(msg=f"cannot compare {what}: {detail}",
                                  severity=SeverityE.Error))
    else:
        markers.append(TaskMarker(msg=f"inconclusive: {what}; {detail}",
                                  severity=SeverityE.Error if p.require_proof
                                  else SeverityE.Warning))
    fail = v == "error" or (v == "not-equivalent" and p.gate) or \
        (v == "inconclusive" and p.require_proof)
    report = {"lhs": [f[0] for f in gold.files] if not gold_v else [gold_v],
              "rhs": [f[0] for f in gate.files], "top": top, "target": family,
              "verdict": v, "detail": detail, "method": method, "seconds": secs,
              "cells": counts[0] if counts else None,
              "unproven": counts[2] if counts else None}
    with open(os.path.join(rundir, "equiv.json"), "w") as f:
        json.dump(report, f, indent=2)
    return TaskDataResult(status=1 if fail else 0, changed=True, markers=markers, output=[
        FileSet(src=input.name, filetype="yosysEquivReport", basedir=rundir,
                files=["equiv.json"], attributes=[f"verdict={v}", f"target={family}",
                                                  f"top={top}"])])
