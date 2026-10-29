"""synth.xls tasks around ``lower``: Lower (schedule -> V4 + V5) and the link
checks LinkV4V5 (per-stage combinational equivalence, Yosys) and LinkV3V4
(per-op cycle and result equality, Verilator)."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Dict, List, Optional, Tuple

from dv_flow.mgr import FileSet, SeverityE, TaskDataInput, TaskDataResult, TaskMarker, TaskRunCtxt

from ..execq import exec_expected
from . import lower as L


def _err(msg: str) -> TaskDataResult:
    return TaskDataResult(status=1, changed=False, output=[],
                          markers=[TaskMarker(msg=msg, severity=SeverityE.Error)])


def _module_of(fs) -> Optional[str]:
    m = (getattr(fs, "params", None) or {}).get("module")
    if m:
        return m
    for a in getattr(fs, "attributes", None) or []:
        if a.startswith("module="):
            return a[len("module="):]
    return None


def _by_module(input: TaskDataInput, filetype: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for fs in getattr(input, "inputs", []) or []:
        if fs.filetype == filetype:
            mod = _module_of(fs) or ""
            out.setdefault(mod, []).extend(os.path.join(fs.basedir, f) for f in fs.files)
    return out


# ---------------------------------------------------------------------- Lower


async def Lower(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    p = input.params
    scheds = _by_module(input, "xlsSchedule")
    sched_irs = _by_module(input, "xlsScheduledIR")
    opt_irs = _by_module(input, "xlsOptIR")
    mods = list(p.modules) if p.modules else [m for m in scheds if m]
    if not mods:
        return _err("Lower: no xlsSchedule input (from synth.xls.Codegen)")
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    out: List[FileSet] = []
    markers: List[TaskMarker] = []
    for mod in mods:
        if mod not in scheds:
            return _err(f"Lower: no schedule for module {mod}; have {sorted(scheds)}")
        ir = (sched_irs.get(mod) or opt_irs.get(mod) or [None])[0]
        if ir is None:
            return _err(f"Lower: no xlsScheduledIR / xlsOptIR for module {mod}")
        infer = mod not in sched_irs
        if infer:
            markers.append(TaskMarker(
                msg=f"Lower: {mod}: no scheduled IR, lowering {os.path.basename(ir)}; nodes the "
                    "schedule does not name are placed as soon as possible", severity=SeverityE.Warning))
        t0 = time.monotonic()
        try:
            files = L.lower_files(ir, scheds[mod][0], rundir, module=mod, infer=infer)
        except ValueError as e:
            return _err(f"Lower: {mod}: {e}")
        with open(files["structure"]) as f:
            summ = json.load(f)
        ctxt.info(f"{mod}: {summ['stages']} stages, nodes/stage {summ['nodes_per_stage']}, "
                  f"register bits/boundary {[r['bits'] for r in summ['registers']]} "
                  f"({time.monotonic() - t0:.2f}s)")
        base = lambda k: os.path.basename(files[k])
        attrs = [f"module={mod}"]
        out += [FileSet(src=input.name, filetype="systemVerilogSource", basedir=rundir,
                        files=[base("v5")], params={"module": mod}, attributes=attrs + ["view=v5"]),
                FileSet(src=input.name, filetype="xlsV4Model", basedir=rundir,
                        files=[base("v4_stages"), base("v4")], params={"module": mod},
                        attributes=attrs + ["view=v4"]),
                FileSet(src=input.name, filetype="xlsLowerStructure", basedir=rundir,
                        files=[base("structure")], params={"module": mod}, attributes=attrs)]
    if p.passthrough_unlowered:
        for fs in getattr(input, "inputs", []) or []:
            if fs.filetype == "systemVerilogSource" and _module_of(fs) not in mods:
                out.append(fs)
    return TaskDataResult(status=0, changed=True, output=out, markers=markers)


# ------------------------------------------------------------------ LinkV4V5

_RANK = {"equivalent": 0, "inconclusive": 1, "not-equivalent": 2, "error": 3}


def _tool(name: str) -> Optional[str]:
    return shutil.which(name)


def _stage_count(v5: str, mod: str) -> int:
    with open(v5) as f:
        return len(re.findall(rf"^module {re.escape(mod)}__stage\d+\(", f.read(), re.M))


def _stage_ports(v5: str, mod: str, s: int) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    with open(v5) as f:
        text = f.read()
    m = re.search(rf"^module {re.escape(mod)}__stage{s}\((.*?)\);", text, re.M | re.S)
    ins, outs = [], []
    for d, hi, nm in re.findall(r"(input|output)\s+wire\s+\[(\d+):0\]\s+(\w+)", m.group(1)):
        (ins if d == "input" else outs).append((nm, int(hi) + 1))
    return ins, outs


def _v4_wrapper(mod: str, s: int, ins, outs) -> str:
    """Stage s of V4 (the package function) as a module with V5's stage ports."""
    ports = [f"input wire [{w - 1}:0] {n}" for n, w in ins] + [f"output wire [{w - 1}:0] {n}" for n, w in outs]
    pin = "{" + ", ".join(n for n, _ in reversed(ins)) + "}"
    pout = "{" + ", ".join(n for n, _ in reversed(outs)) + "}"
    return (f"module {mod}__stage{s}(\n    " + ",\n    ".join(ports) + ");\n"
            f"    assign {pout} = {mod}_v4_stages_pkg::stage{s}_f({pin});\nendmodule\n")


async def LinkV4V5(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    p = input.params
    yosys = _tool("yosys")
    if yosys is None:
        return _err("LinkV4V5: yosys not found on PATH")
    v4s = _by_module(input, "xlsV4Model")
    v5s = {m: [f for f in fs if f.endswith(".v5.sv")] for m, fs in _by_module(input, "systemVerilogSource").items()}
    mods = [m for m in v4s if v5s.get(m)]
    if not mods:
        return _err("LinkV4V5: needs synth.xls.Lower's xlsV4Model and V5 systemVerilogSource")
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    results, markers, status = [], [], 0
    for mod in mods:
        v5 = v5s[mod][0]
        stages_pkg = [f for f in v4s[mod] if f.endswith(".v4_stages.sv")][0]
        for s in range(_stage_count(v5, mod)):
            ins, outs = _stage_ports(v5, mod, s)
            d = os.path.join(rundir, f"{mod}__stage{s}")
            os.makedirs(d, exist_ok=True)
            gold = os.path.join(d, "v4_stage.sv")
            with open(gold, "w") as f:
                f.write(_v4_wrapper(mod, s, ins, outs))
            top = f"{mod}__stage{s}"
            ys = os.path.join(d, "check.ys")
            # gold: V4's stage function; gate: V5's stage module; one miter,
            # flattened, then SAT over all inputs (combinational: depth 1).
            with open(ys, "w") as f:
                f.write("\n".join([
                    f"read_verilog -sv {stages_pkg} {gold}",
                    f"hierarchy -top {top}", "proc; opt_clean", f"rename {top} gold", "design -stash gold",
                    f"read_verilog -sv {v5}", f"hierarchy -top {top}", "proc; opt_clean",
                    f"rename {top} gate", "design -stash gate",
                    "design -copy-from gold -as gold gold", "design -copy-from gate -as gate gate",
                    "miter -equiv -flatten -make_assert -make_outputs gold gate miter",
                    "hierarchy -top miter", "opt -full", "stat",
                    "sat -verify -prove-asserts -set-init-zero -show-ports -enable_undef miter", ""]))
            log = os.path.join(d, "check.log")
            t0 = time.monotonic()
            st = await exec_expected(ctxt, [yosys, "-q", "-l", log, "-s", ys], cwd=d)
            secs = round(time.monotonic() - t0, 2)
            text = open(log, errors="replace").read() if os.path.exists(log) else ""
            if st == 0 and ("SAT proof finished - no model found: SUCCESS" in text
                            or "SAT proof finished - no model found" in text):
                v, detail = "equivalent", "SAT: no input distinguishes the stage outputs"
            elif "model found: FAIL" in text or "SAT proof finished - model found" in text:
                v = "not-equivalent"
                # the SAT model: an input on which the stages differ (binary,
                # x = either value), and the outputs that differ
                cex = re.findall(r"^\s+\\(in_\S+)\s+(\S+)\s+(\S+)\s*(\S*)\s*$", text, re.M)
                detail = {"counterexample": {a[3:]: (b if b else hx) for a, d, hx, b in cex},
                          "note": "binary, MSB first; x: don't care"}
            else:
                v, detail = "error", (text.strip().splitlines() or ["yosys failed"])[-1]
            ins_b, outs_b = sum(w for _, w in ins), sum(w for _, w in outs)
            res = {"module": mod, "stage": s, "verdict": v, "detail": detail, "method": "yosys miter + sat",
                   "inputs_bits": ins_b, "outputs_bits": outs_b, "seconds": secs,
                   "gold": f"V4 {mod}_v4_stages_pkg::stage{s}_f", "gate": f"V5 {top}", "log": log}
            results.append(res)
            what = f"{mod} stage {s} (V4 stage{s}_f vs V5 {top})"
            if v == "equivalent":
                ctxt.info(f"equivalent: {what} ({secs}s)")
            else:
                markers.append(TaskMarker(msg=f"{v}: {what}: {detail}",
                                          severity=SeverityE.Error if p.gate else SeverityE.Warning))
                status |= 1 if p.gate else 0
    worst = max((r["verdict"] for r in results), key=_RANK.get)
    with open(os.path.join(rundir, "link_v4_v5.json"), "w") as f:
        json.dump({"link": "V4<->V5", "verdict": worst, "stages": results}, f, indent=2)
    return TaskDataResult(status=status, changed=True, markers=markers, output=[
        FileSet(src=input.name, filetype="xlsLinkReport", basedir=rundir,
                files=["link_v4_v5.json"], attributes=[f"verdict={worst}"])])


# ------------------------------------------------------------------ LinkV3V4

_OP = re.compile(r"task\s+op\s*\((.*?)\);", re.S)


def _v3_class(texts: List[str], cls: str) -> Tuple[str, List[Tuple[str, str, str, List[Tuple[int, int]]]]]:
    """(clk interface, [(direction, base type, name, unpacked dims)]) of class cls's op()."""
    for t in texts:
        m = re.search(rf"class\s+{re.escape(cls)}\b(.*?)endclass", t, re.S)
        if not m:
            continue
        body = m.group(1)
        pk = [x.group(1) for x in re.finditer(r"^\s*package\s+(\w+)\s*;", t[:m.start()], re.M)]
        if pk and "endpackage" not in t[t.rfind("package " + pk[-1]):m.start()]:
            cls = f"{pk[-1]}::{cls}"
        vif = re.search(r"virtual\s+(\w+)\s+vif\s*;", body).group(1)
        args = []
        for a in L._split_top(_OP.search(body).group(1)):
            am = re.match(r"(input|output)\s+(.*?)\s*(\w+)\s*((?:\[\s*\d+\s*:\s*\d+\s*\])*)$", a.strip(), re.S)
            dims = [(int(x), int(y)) for x, y in re.findall(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", am.group(4))]
            args.append((am.group(1), am.group(2), am.group(3), dims))
        return cls, vif, args
    raise ValueError(f"class {cls} with task op() not found")


def _conv(flat: str, var: str, base: str, dims: List[Tuple[int, int]], ew: int, to_flat: bool) -> List[str]:
    """Statements moving between a flat vector (element 0 at the LSB, row-major)
    and an SV variable with unpacked dims (element 0 = leftmost index)."""
    if not dims:
        return [f"{flat} = {var};" if to_flat else f"{var} = {base}'({flat});"]
    out, idx, k = [], [], "k"
    out.append(f"{k} = 0;")
    for n, (l, r) in enumerate(dims):
        step = 1 if r >= l else -1
        out.append(f"for (int i{n} = {l}; i{n} != {r + step}; i{n} += {step})")
        idx.append(f"[i{n}]")
    el = f"{var}{''.join(idx)}"
    body = f"{flat}[{k}*{ew} +: {ew}] = {el};" if to_flat else f"{el} = {flat}[{k}*{ew} +: {ew}];"
    out.append(f"begin {body} {k}++; end")
    return out


async def LinkV3V4(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    p = input.params
    vlt = _tool("verilator")
    if vlt is None:
        return _err("LinkV3V4: verilator not found on PATH")
    v4s = _by_module(input, "xlsV4Model")
    structs = _by_module(input, "xlsLowerStructure")
    srcs: List[str] = []
    incdirs: List[str] = []
    for fs in getattr(input, "inputs", []) or []:
        if fs.filetype == "systemVerilogSource" and not any(f.endswith(".v5.sv") for f in fs.files):
            srcs += [os.path.join(fs.basedir, f) for f in fs.files]
        if fs.filetype == "systemVerilogInclude":
            incdirs.append(fs.basedir)
    srcs = list(dict.fromkeys(srcs))
    texts = [open(f, errors="replace").read() for f in srcs]
    pairs = []
    for spec in p.pairs:
        a, sep, b = spec.partition("=")
        if not sep or b.strip() not in v4s:
            return _err(f"LinkV3V4: pair {spec!r} is not `<v3 class>=<lowered module>` "
                        f"(modules: {sorted(v4s)})")
        pairs.append((a.strip(), b.strip()))
    if not pairs:
        return _err("LinkV3V4: no pairs")
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    tb = ["module link_v3_v4_tb;", "    logic clk = 0;", "    always #5 clk = ~clk;"]
    body: List[str] = []
    v4files: List[str] = []
    for n, (cls, mod) in enumerate(pairs):
        try:
            qcls, vif, args = _v3_class(texts, cls)
        except (ValueError, AttributeError) as e:
            return _err(f"LinkV3V4: {cls}: {e}")
        v4files += [f for f in v4s[mod] if f.endswith(".v4_stages.sv")] + \
                   [f for f in v4s[mod] if f.endswith(".v4.sv")]
        v4text = open([f for f in v4s[mod] if f.endswith(".v4.sv")][0]).read()
        widths = [(nm, int(hi) + 1) for hi, nm in
                  re.findall(r"bit \[(\d+):0\] (\w+)", re.search(r"function void tick\(bit input_valid, (.*?)\);",
                                                               v4text).group(1))]
        rw = int(re.search(r"output bit \[(\d+):0\] result", v4text).group(1)) + 1
        ins = [a for a in args if a[0] == "input"]
        outa = [a for a in args if a[0] == "output"][0]
        if len(ins) != len(widths):
            return _err(f"LinkV3V4: {cls}.op has {len(ins)} inputs, {mod} has {len(widths)}")
        tb += [f"    {vif} v3if{n}(clk);", f"    {mod}_v4_clk_if v4if{n}(clk);"]
        body += [f"        begin : pair{n}",
                 f"            {qcls} v3 = new(v3if{n});",
                 f"            {mod}_v4_pkg::{mod}_v4 v4 = new(v4if{n});",
                 "            int k, c3, c4, fails;"]
        for i, ((_, base, nm, dims), (_, w)) in enumerate(zip(ins, widths)):
            body.append(f"            bit [{w - 1}:0] f{i};")
            body.append(f"            {base} a{i}{''.join(f'[{l}:{r}]' for l, r in dims)};")
        body += [f"            bit [{rw - 1}:0] r3, r4;",
                 f"            {outa[1]} o3{''.join(f'[{l}:{r}]' for l, r in outa[3])};",
                 "            int fd;",
                 f"            fd = $fopen(\"{cls}__{mod}.trace\", \"w\");",
                 "            v4.reset();", "            fails = 0;",
                 f"            for (int op = 0; op < {p.ops}; op++) begin"]
        for i, (_, w) in enumerate(widths):
            body.append(f"                for (int b = 0; b < {w}; b += 32) f{i}[b +: 32] = $urandom;"
                        if w > 32 else f"                f{i} = {w}'($urandom);")
        # element widths: from the XLS type via the V3 dims
        for i, ((_, base, nm, dims), (_, w)) in enumerate(zip(ins, widths)):
            cnt = 1
            for l, r in dims:
                cnt *= abs(r - l) + 1
            body += ["                " + s for s in _conv(f"f{i}", f"a{i}", base, dims, w // cnt, False)]
        cnt = 1
        for l, r in outa[3]:
            cnt *= abs(r - l) + 1
        body += ["                c3 = v3.m_clks;",
                 "                v3.op(" + ", ".join(f"a{i}" for i in range(len(ins))) + ", o3);",
                 "                c3 = v3.m_clks - c3;"]
        body += ["                " + s for s in _conv("r3", "o3", outa[1], outa[3], rw // cnt, True)]
        body += ["                c4 = v4.m_clks;",
                 "                v4.op(" + ", ".join(f"f{i}" for i in range(len(ins))) + ", r4);",
                 "                c4 = v4.m_clks - c4;",
                 "                $fdisplay(fd, \"op %0d " + " ".join("%h" for _ in ins) +
                 " v3=%h/%0dclk v4=%h/%0dclk %s\", op, " + ", ".join(f"f{i}" for i in range(len(ins))) +
                 ", r3, c3, r4, c4, (r3 == r4 && c3 == c4) ? \"ok\" : \"MISMATCH\");",
                 "                if (r3 != r4 || c3 != c4) begin",
                 "                    if (fails == 0) $display(\"LINK-MISMATCH pair=" + f"{cls}={mod}" +
                 " op=%0d inputs=" + " ".join("%h" for _ in ins) + " v3=%h/%0d v4=%h/%0d\", op, " +
                 ", ".join(f"f{i}" for i in range(len(ins))) + ", r3, c3, r4, c4);",
                 "                    fails++;", "                end",
                 "            end",
                 "            $fclose(fd);",
                 f"            $display(\"LINK-RESULT pair={cls}={mod} ops={p.ops} fails=%0d clks_v3=%0d clks_v4=%0d\","
                 " fails, c3, c4);",
                 "        end"]
    tb += ["    initial begin"] + body + ["        $finish;", "    end", "endmodule", ""]
    tbf = os.path.join(rundir, "link_v3_v4_tb.sv")
    with open(tbf, "w") as f:
        f.write("\n".join(tb))
    log = os.path.join(rundir, "build.log")
    files = srcs + list(dict.fromkeys(v4files)) + [tbf]
    t0 = time.monotonic()
    st = await exec_expected(ctxt, [vlt, "--binary", "--timing", "-Wno-fatal", "-Wno-lint", "-Wno-style",
                                    "-j", "0", "--top-module", "link_v3_v4_tb", "-o", "link_tb"] +
                             [f"-I{d}" for d in incdirs] + files, logfile=log, cwd=rundir)
    if st != 0:
        return _err(f"LinkV3V4: verilator build failed, see {log}")
    slog = os.path.join(rundir, "sim.log")
    st = await exec_expected(ctxt, [os.path.join(rundir, "obj_dir", "link_tb")], logfile=slog, cwd=rundir)
    secs = round(time.monotonic() - t0, 2)
    text = open(slog, errors="replace").read()
    results, markers, status = [], [], 0
    for cls, mod in pairs:
        m = re.search(rf"LINK-RESULT pair={cls}={mod} ops=(\d+) fails=(\d+) clks_v3=(\d+) clks_v4=(\d+)", text)
        mm = re.search(rf"LINK-MISMATCH pair={cls}={mod} (.*)", text)
        if not m:
            v, det = "error", "no result in sim.log"
        else:
            v = "equivalent" if int(m.group(2)) == 0 else "not-equivalent"
            det = {"ops": int(m.group(1)), "mismatches": int(m.group(2)),
                   "clks_per_op_v3": int(m.group(3)), "clks_per_op_v4": int(m.group(4)),
                   "first_mismatch": mm.group(1) if mm else None}
        results.append({"v3": cls, "v4": mod, "verdict": v, "detail": det,
                        "method": "simulation: per-op result and clock count, random inputs",
                        "witness": os.path.join(rundir, f"{cls}__{mod}.trace")})
        if v == "equivalent":
            ctxt.info(f"equivalent: V3 {cls} vs V4 {mod}: {det}")
        else:
            markers.append(TaskMarker(msg=f"{v}: V3 {cls} vs V4 {mod}: {det}",
                                      severity=SeverityE.Error if p.gate else SeverityE.Warning))
            status |= 1 if p.gate else 0
    worst = max((r["verdict"] for r in results), key=_RANK.get)
    with open(os.path.join(rundir, "link_v3_v4.json"), "w") as f:
        json.dump({"link": "V3<->V4", "verdict": worst, "seconds": secs, "pairs": results}, f, indent=2)
    return TaskDataResult(status=status, changed=True, markers=markers, output=[
        FileSet(src=input.name, filetype="xlsLinkReport", basedir=rundir,
                files=["link_v3_v4.json"], attributes=[f"verdict={worst}"])])
