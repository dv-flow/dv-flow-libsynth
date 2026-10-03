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

from ..execq import exec_expected
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


# ----------------------------------------------------------------------
# Exhaustive comparison, for when the solver does not finish
# ----------------------------------------------------------------------

def _ports(sig_text: str):
    """(inputs, outputs) as [(name, width)] from a ModuleSignatureProto."""
    ins, outs = [], []
    for m in re.finditer(r"data_ports \{(.*?)\n\}", sig_text, re.S):
        body = m.group(1)
        name = re.search(r'name: "([^"]+)"', body).group(1)
        width = int(re.search(r"width: (\d+)", body).group(1))
        (ins if "PORT_DIRECTION_INPUT" in body else outs).append((name, width))
    return ins, outs


def exhaustive_tb(ins, ins_r, out_l: str, out_r: str, out_w: int) -> str:
    """A bench that drives every input combination into modules `lhs` and
    `rhs` (inputs matched by position) and counts the mismatches."""
    total = sum(w for _, w in ins)
    decl = "\n".join(f"  bit [{w - 1}:0] i{k};" for k, (_, w) in enumerate(ins))
    cl = ", ".join(f".{n}(i{k})" for k, (n, _) in enumerate(ins))
    cr = ", ".join(f".{n}(i{k})" for k, (n, _) in enumerate(ins_r))
    assign = "{" + ", ".join(f"i{k}" for k in range(len(ins))) + "} = v;"
    show = ", ".join(f"{n}=%0h" for n, _ in ins)
    args = ", ".join(f"i{k}" for k in range(len(ins)))
    return f"""module equiv_tb;
{decl}
  wire [{out_w - 1}:0] yl, yr;
  lhs ul ({cl}, .{out_l}(yl));
  rhs ur ({cr}, .{out_r}(yr));
  longint unsigned bad = 0;
  initial begin
    for (longint unsigned v = 0; v < (64'd1 << {total}); v++) begin
      {assign}
      #1;
      if (yl !== yr) begin
        if (bad == 0) $display("FIRST-MISMATCH {show} lhs=%0h rhs=%0h", {args}, yl, yr);
        bad++;
      end
    end
    $display("MISMATCHES %0d OF %0d", bad, 64'd1 << {total});
    $finish;
  end
endmodule
"""


async def exhaustive(ctxt, rundir: str, n: int, lhs: str, rhs: str, max_bits: int):
    """(verdict, detail), or None when it does not apply (too many input
    bits, a different interface, no Verilator)."""
    cg, vlt = find_tool("codegen_main"), find_tool("verilator")
    if cg is None or vlt is None:
        return None
    d = os.path.join(rundir, f"exhaustive{n}")
    os.makedirs(d, exist_ok=True)
    sigs = []
    for side, ir in (("lhs", lhs), ("rhs", rhs)):
        st = await exec_expected(ctxt, [cg, ir, "--generator=combinational", f"--module_name={side}",
                              f"--output_verilog_path={side}.v",
                              f"--output_signature_path={side}.sig.textproto"],
                             logfile=os.path.join(d, f"{side}.codegen.log"), cwd=d)
        if st != 0:
            return None
        with open(os.path.join(d, f"{side}.sig.textproto")) as f:
            sigs.append(_ports(f.read()))
    (ins, outs), (ins2, outs2) = sigs
    if [w for _, w in ins] != [w for _, w in ins2] or len(outs) != 1 or \
            [w for _, w in outs] != [w for _, w in outs2]:
        return None
    total = sum(w for _, w in ins)
    if total > max_bits:
        return None
    with open(os.path.join(d, "equiv_tb.sv"), "w") as f:
        f.write(exhaustive_tb(ins, ins2, outs[0][0], outs2[0][0], outs[0][1]))
    st = await exec_expected(ctxt, [vlt, "--binary", "--timing", "-Wno-fatal", "-Wno-lint", "-Wno-style",
                          "lhs.v", "rhs.v", "equiv_tb.sv", "--top-module", "equiv_tb",
                          "-o", "equiv_tb"], logfile=os.path.join(d, "build.log"), cwd=d)
    if st != 0:
        return None
    log = os.path.join(d, "sim.log")
    st = await exec_expected(ctxt, [os.path.join(d, "obj_dir", "equiv_tb")], logfile=log, cwd=d)
    with open(log, errors="replace") as f:
        text = f.read()
    m = re.search(r"MISMATCHES (\d+) OF (\d+)", text)
    if st != 0 or not m:
        return None
    if int(m.group(1)) == 0:
        return "equivalent", f"exhaustive: all {m.group(2)} inputs agree"
    first = re.search(r"FIRST-MISMATCH (.*)", text)
    return "not-equivalent", (f"exhaustive: {m.group(1)} of {m.group(2)} inputs differ; "
                              f"first: {first.group(1) if first else '?'}")


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
        st = await exec_expected(ctxt, cmd, logfile=log, cwd=rundir)
        secs = round(time.monotonic() - t0, 2)
        with open(log, errors="replace") as f:
            v, detail = verdict(f.read())
        method = "solver"
        if v == "inconclusive" and p.exhaustive_max_bits > 0:
            # The solver did not finish: a small function can be compared on
            # every input instead.
            ex = await exhaustive(ctxt, rundir, n, lhs, rhs, p.exhaustive_max_bits)
            if ex is not None:
                v, detail = ex
                method = "exhaustive"
                secs = round(time.monotonic() - t0, 2)
        what = f"{name(lhs)} vs {name(rhs)}"
        if v == "equivalent":
            ctxt.info(f"equivalent: {what} ({secs}s{', ' + detail if detail else ''})")
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
                        "method": method, "seconds": secs, "exit": st})
    worst = max((r["verdict"] for r in results), key=_RANK.get)
    report = dict(results[0]) if len(results) == 1 else {"verdict": worst, "pairs": results}
    with open(os.path.join(rundir, "equiv.json"), "w") as f:
        json.dump(report, f, indent=2)
    return TaskDataResult(status=status, changed=True, markers=markers, output=[
        FileSet(src=input.name, filetype="xlsEquivReport", basedir=rundir,
                files=["equiv.json"], attributes=[f"verdict={worst}"])])
