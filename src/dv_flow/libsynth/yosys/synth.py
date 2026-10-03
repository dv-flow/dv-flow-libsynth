"""synth.yosys.Synth: Yosys synthesis, for any target, with numbers and a
netlist a simulator can run.

One task for every target (`target: generic | ice40 | ecp5 | ... | xilinx`):
a flow changes FPGA family by changing one parameter, and the netlist records
`family=` for the back end.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from dv_flow.mgr import (FileSet, SeverityE, TaskDataInput, TaskDataResult, TaskMarker,
                         TaskMarkerLoc, TaskRunCtxt)


@dataclass(frozen=True)
class Target:
    command: str                 # the synth command
    family: str                  # what the netlist records
    family_flag: Optional[str]   # the flag that picks a device/family, if any
    default_family: Optional[str]
    flattens: bool               # flattens unless told -noflatten
    dsp: Optional[str]           # "-dsp" (opt in) / "-nodsp" (opt out) / None
    bram: bool                   # has -nobram
    sim_models: Tuple[str, ...]  # under the Yosys data directory
    sim_defines: Tuple[str, ...] = ()


_LATTICE = dict(command="synth_lattice", family_flag="-family", flattens=True,
                dsp="-nodsp", bram=True)

TARGETS: Dict[str, Target] = {
    "generic": Target("synth", "generic", None, None, False, None, False, ("simcells.v",)),
    # The iCE40 models give input ports default values. Verilator (5.049)
    # drives the connected net with them, against LRM 23.2.2.4 (a default
    # applies to an unconnected port only): a bench's reset is held low.
    # Yosys's own switch turns the defaults off.
    "ice40": Target("synth_ice40", "ice40", "-device", None, True, "-dsp", True,
                    ("ice40/cells_sim.v",), ("NO_ICE40_DEFAULT_ASSIGNMENTS",)),
    "ecp5": Target(family="ecp5", default_family="ecp5",
                   sim_models=("lattice/cells_sim_ecp5.v",), **_LATTICE),
    "nexus": Target(family="nexus", default_family="lifcl",
                    sim_models=("lattice/cells_sim_nexus.v",), **_LATTICE),
    "machxo2": Target(family="machxo2", default_family="xo2",
                      sim_models=("lattice/cells_sim_xo2.v",), **_LATTICE),
    "machxo3": Target(family="machxo3", default_family="xo3",
                      sim_models=("lattice/cells_sim_xo3.v",), **_LATTICE),
    "gowin": Target("synth_gowin", "gowin", "-family", None, True, "-nodsp", True,
                    ("gowin/cells_sim.v",)),
    "gatemate": Target("synth_gatemate", "gatemate", None, None, True, None, True,
                       ("gatemate/cells_sim.v",)),
    "xilinx": Target("synth_xilinx", "xilinx", "-family", None, False, "-nodsp", True,
                     ("xilinx/cells_sim.v",)),
}

FORMATS = {"json": ("write_json", "netlist.json"),
           "verilog": ("write_verilog -noattr", "netlist.v"),
           "blif": ("write_blif", "netlist.blif"),
           "edif": ("write_edif", "netlist.edif"),
           "rtlil": ("write_rtlil", "netlist.il")}


def find_yosys() -> Optional[str]:
    return shutil.which("yosys")


def datdir(yosys: str) -> Optional[str]:
    """Yosys's data directory: next to the binary (a relocated release), else
    what yosys-config says."""
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(yosys))),
                     "share", "yosys")
    if os.path.isfile(os.path.join(d, "simcells.v")):
        return d
    cfg = shutil.which("yosys-config")
    if cfg:
        try:
            d = subprocess.run([cfg, "--datdir"], capture_output=True, text=True,
                               timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if os.path.isfile(os.path.join(d, "simcells.v")):
            return d
    return None


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------

@dataclass
class Sources:
    files: List[Tuple[str, bool]]     # (path, is SystemVerilog)
    incdirs: List[str]
    defines: List[str]
    liberty: List[str]
    modules: List[str]                # fileset `module`/`top` params, a top hint


def collect(input: TaskDataInput) -> Sources:
    out = Sources([], [], [], [], [])
    for fs in getattr(input, "inputs", []) or []:
        if getattr(fs, "type", "std.FileSet") != "std.FileSet":
            continue
        ft = fs.filetype
        if ft in ("verilogSource", "systemVerilogSource"):
            out.files += [(os.path.join(fs.basedir, f), ft == "systemVerilogSource")
                          for f in fs.files]
            out.incdirs += [os.path.join(fs.basedir, d) for d in getattr(fs, "incdirs", [])]
            out.defines += list(getattr(fs, "defines", []))
            for a in getattr(fs, "attributes", None) or []:
                k, _, v = a.partition("=")
                if k in ("top", "module") and v:
                    out.modules.append(v)
        elif ft in ("verilogInclude", "systemVerilogInclude", "verilogIncDir"):
            out.incdirs += [os.path.join(fs.basedir, d)
                            for d in getattr(fs, "incdirs", [])] or [fs.basedir]
        elif ft == "libertyLib":
            out.liberty += [os.path.join(fs.basedir, f) for f in fs.files] or [fs.basedir]
    out.files = list(dict.fromkeys(out.files))
    out.incdirs = list(dict.fromkeys(out.incdirs))
    out.defines = list(dict.fromkeys(out.defines))
    return out


# ----------------------------------------------------------------------
# The script
# ----------------------------------------------------------------------

def synth_command(t: Target, p, top: Optional[str]) -> str:
    cmd = [t.command]
    cmd.append(f"-top {top}" if top else "-auto-top" if t.command == "synth" else "")
    fam = p.device or t.default_family
    if fam:
        if not t.family_flag:
            raise ValueError(f"target {p.target} takes no device")
        cmd.append(f"{t.family_flag} {fam}")
    if t.command == "synth" or t.command == "synth_xilinx":
        if p.flatten:
            cmd.append("-flatten")
    elif not p.flatten:
        cmd.append("-noflatten")
    if t.dsp == "-dsp" and p.dsp:
        cmd.append("-dsp")
    elif t.dsp == "-nodsp" and not p.dsp:
        cmd.append("-nodsp")
    if t.bram and not p.bram:
        cmd.append("-nobram")
    cmd += list(p.synth_args or [])
    return " ".join(c for c in cmd if c)


def read_commands(src: Sources, sv2v_out: Optional[str]) -> List[str]:
    defs = " ".join(f"-D{d}" for d in src.defines)
    incs = " ".join(f"-I{d}" for d in src.incdirs)
    if sv2v_out:
        return [f"read_verilog {defs} {sv2v_out}".replace("  ", " ")]
    return [" ".join(x for x in ("read_verilog", "-sv" if sv else "", defs, incs, path) if x)
            for path, sv in src.files]


def script(src: Sources, t: Target, p, top: Optional[str], rundir: str,
           sv2v_out: Optional[str]) -> str:
    lines = read_commands(src, sv2v_out)
    for lib in src.liberty:
        lines.append(f"read_liberty -lib {lib}")
    lines.append(synth_command(t, p, top))
    if src.liberty and t.command == "synth":
        lines += [f"dfflibmap -liberty {src.liberty[0]}", f"abc -liberty {src.liberty[0]}",
                  "opt_clean"]
    # The user's commands change the netlist, so they come before the writes.
    lines += list(p.args or [])
    stat = f"stat -json" + (f" -liberty {src.liberty[0]}" if src.liberty else "")
    lines.append(f"tee -q -o {os.path.join(rundir, 'stat.json')} {stat}")
    lines.append("stat" + (f" -liberty {src.liberty[0]}" if src.liberty else ""))
    for f in formats(p):
        cmd, name = FORMATS[f]
        lines.append(f"{cmd} {os.path.join(rundir, name)}")
    return "\n".join(lines) + "\n"


def formats(p) -> List[str]:
    fmts = list(dict.fromkeys(list(p.formats or []) + (["verilog"] if p.sim_netlist else [])))
    bad = [f for f in fmts if f not in FORMATS]
    if bad:
        raise ValueError(f"unknown netlist format(s) {bad}; one of {sorted(FORMATS)}")
    return fmts


# ----------------------------------------------------------------------
# The log and the numbers
# ----------------------------------------------------------------------

_ERR = re.compile(r"^(?:(?P<path>[^\s:]+):(?P<line>\d+): )?ERROR: (?P<msg>.*)$")
_WARN = re.compile(r"^Warning: (?P<msg>.*?)(?: at (?P<path>\S+?):(?P<line>\d+)(?:\.\d+-\d+\.\d+)?)?\.?$")


def log_markers(text: str, warnings: bool = True) -> List[TaskMarker]:
    out, cache = [], {}
    for line in text.splitlines():
        m = _ERR.match(line)
        sev = SeverityE.Error
        if not m and warnings:
            m = _WARN.match(line)
            sev = SeverityE.Warning
        if not m:
            continue
        msg = m.group("msg").strip()
        path, ln = m.group("path"), m.group("line")
        if path and ln:
            path, ln, col = _map_source(path, int(ln), cache)
            out.append(TaskMarker(msg=msg, severity=sev,
                                  loc=TaskMarkerLoc(path=path, line=ln, pos=col)))
        else:
            out.append(TaskMarker(msg=msg, severity=sev))
    # Yosys repeats some warnings; keep one of each.
    seen, uniq = set(), []
    for m in out:
        k = (m.msg, m.severity, m.loc.path if m.loc else None, m.loc.line if m.loc else None)
        if k not in seen:
            seen.add(k)
            uniq.append(m)
    return uniq


def _map_source(path: str, line: int, cache: Dict[str, str]) -> Tuple[str, int, int]:
    """A line of XLS-generated Verilog is reported on the source line it came
    from (its `// file:line:col` comment), when it has one."""
    from ..xls.diag import verilog_source
    if path not in cache:
        cache[path] = _read(path)
    loc = verilog_source(cache[path], line) if cache[path] else None
    return (loc[0], loc[1], loc[2]) if loc else (path, line, -1)


def summarize(stat: dict, top: Optional[str]) -> Dict[str, int]:
    """The design's numbers: the top module's, or the totals."""
    mods = stat.get("modules", {})
    design = stat.get("design") or {}
    m = None
    if top and f"\\{top}" in mods:
        m = mods[f"\\{top}"]
    elif design:
        m = design
    elif len(mods) == 1:
        m = next(iter(mods.values()))
    if not m:
        return {}
    out = {"cells": int(m.get("num_cells", 0)), "wire_bits": int(m.get("num_wire_bits", 0))}
    for k, v in (m.get("num_cells_by_type") or {}).items():
        out[f"cells.{k.lstrip('$')}"] = int(v)
    if "area" in m:
        out["area"] = m["area"]
    return out


# ----------------------------------------------------------------------
# The task
# ----------------------------------------------------------------------

def _error(msg: str) -> TaskDataResult:
    return TaskDataResult(status=1, changed=False, output=[],
                          markers=[TaskMarker(msg=msg, severity=SeverityE.Error)])


def _read(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return ""


async def Synth(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    yosys = find_yosys()
    if yosys is None:
        return _error("yosys not found on PATH")
    p = input.params
    t = TARGETS.get(p.target)
    if t is None:
        return _error(f"unknown target {p.target!r}; one of {sorted(TARGETS)}")
    src = collect(input)
    if not src.files:
        return _error("Synth: no verilogSource or systemVerilogSource input")
    top = p.top or (src.modules[-1] if len(set(src.modules)) == 1 else None)
    if p.frontend not in ("auto", "yosys", "sv2v"):
        return _error(f"frontend {p.frontend!r}: one of auto, yosys, sv2v")
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    try:
        fmts = formats(p)
    except ValueError as e:
        return _error(str(e))

    markers: List[TaskMarker] = []

    async def run(sv2v_out: Optional[str]) -> Tuple[int, str]:
        try:
            text = script(src, t, p, top, rundir, sv2v_out)
        except ValueError as e:
            return -1, str(e)
        ys = os.path.join(rundir, "synth.ys")
        with open(ys, "w") as f:
            f.write(text)
        log = os.path.join(rundir, "synth.log")
        st = await ctxt.exec([yosys, "-q", "-l", log, ys], logfile="yosys.out", cwd=rundir)
        return st, _read(log)

    async def sv2v() -> Optional[str]:
        conv = shutil.which("sv2v") or os.path.join(os.path.dirname(yosys), "sv2v")
        if not os.access(conv, os.X_OK):
            markers.append(TaskMarker(msg="sv2v not found on PATH or beside yosys",
                                      severity=SeverityE.Error))
            return None
        out = os.path.join(rundir, "sv2v.v")
        cmd = [conv, *[f"-I{d}" for d in src.incdirs], *[f"-D{d}" for d in src.defines],
               *[pth for pth, _ in src.files], f"--write={out}"]
        st = await ctxt.exec(cmd, logfile="sv2v.log", cwd=rundir)
        if st != 0:
            markers.append(TaskMarker(msg=f"sv2v failed (see {os.path.join(rundir, 'sv2v.log')})",
                                      severity=SeverityE.Error))
            return None
        return out

    frontend = p.frontend
    if frontend == "sv2v":
        conv = await sv2v()
        if conv is None:
            return TaskDataResult(status=1, changed=True, output=[], markers=markers)
        st, log = await run(conv)
    else:
        st, log = await run(None)
        if st == -1:
            return _error(log)
        if st != 0 and frontend == "auto" and any(sv for _, sv in src.files):
            # Yosys's own reader takes much of SV but not all of it (XLS's
            # multi-dimensional arrays, for one). Try again through sv2v.
            first = log_markers(log, warnings=False)
            conv = await sv2v()
            if conv is not None:
                st2, log2 = await run(conv)
                if st2 == 0:
                    why = first[0].msg if first else "a read error"
                    markers.append(TaskMarker(
                        msg=f"Yosys's SV reader failed ({why}); read through sv2v instead",
                        severity=SeverityE.Info))
                    frontend, st, log = "sv2v", st2, log2
    markers += log_markers(log, warnings=p.warnings)
    if st != 0:
        if not any(m.severity == SeverityE.Error for m in markers):
            markers.append(TaskMarker(msg=f"yosys exited {st} (see {rundir}/synth.log)",
                                      severity=SeverityE.Error))
        return TaskDataResult(status=st or 1, changed=True, output=[], markers=markers)

    try:
        with open(os.path.join(rundir, "stat.json")) as f:
            nums = summarize(json.load(f), top)
    except (OSError, ValueError):
        nums = {}
    if nums:
        ctxt.info(f"{top or 'design'} on {p.target}: {nums.get('cells', 0)} cells")

    attrs = [f"family={t.family}", f"target={p.target}", f"frontend={frontend}"] \
        + ([f"top={top}"] if top else []) + ([f"device={p.device}"] if p.device else [])
    out = [FileSet(src=input.name, filetype="yosysNetlist", basedir=rundir,
                   files=[FORMATS[f][1]], attributes=[f"format={f}"] + attrs)
           for f in fmts if f != "verilog" or not p.sim_netlist]
    out.append(FileSet(src=input.name, filetype="yosysStat", basedir=rundir,
                       files=["stat.json"], attributes=attrs))
    if p.sim_netlist:
        # The netlist as a simulator takes it: the target's cell models, then
        # the netlist, whose top keeps the RTL module's name and ports.
        dd = datdir(yosys)
        if dd is None:
            markers.append(TaskMarker(msg="Yosys data directory not found; the simulation "
                                          "netlist has no cell models", severity=SeverityE.Warning))
        else:
            out.append(FileSet(src=input.name, filetype="verilogSource", basedir=dd,
                               files=list(t.sim_models), attributes=["yosysCellModels"] + attrs,
                               defines=list(t.sim_defines),
                               # the models include their own .vh files
                               incdirs=list(dict.fromkeys(os.path.dirname(m) or "."
                                                          for m in t.sim_models))))
        out.append(FileSet(src=input.name, filetype="verilogSource", basedir=rundir,
                           files=[FORMATS["verilog"][1]], attributes=["format=verilog"] + attrs))
    return TaskDataResult(status=0, changed=True, output=out, markers=markers)
