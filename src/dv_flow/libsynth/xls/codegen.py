"""synth.xls tasks: XLS IR -> Verilog (Codegen) and DSLX -> XLS IR (DslxToIR)."""
from __future__ import annotations

import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from dv_flow.mgr import (FileSet, SeverityE, TaskDataInput, TaskDataResult, TaskMarker,
                         TaskMarkerLoc, TaskRunCtxt)

from . import diag


def find_tool(name: str) -> Optional[str]:
    """An XLS tool on PATH, else in $XLS_BIN."""
    p = shutil.which(name)
    if p:
        return p
    d = os.environ.get("XLS_BIN")
    if d and os.access(os.path.join(d, name), os.X_OK):
        return os.path.join(d, name)
    return None


def input_files(input: TaskDataInput, filetype: str) -> List[str]:
    out = []
    for fs in getattr(input, "inputs", []) or []:
        if getattr(fs, "filetype", None) == filetype:
            out += [os.path.join(fs.basedir, f) for f in fs.files]
    return list(dict.fromkeys(out))


_TOP = re.compile(r"^top\s+(?:fn|proc|block)\s+([\w.]+)", re.M)
_PKG = re.compile(r"^package\s+([\w.]+)", re.M)


def ir_top(text: str) -> Optional[str]:
    m = _TOP.search(text) or _PKG.search(text)
    return m.group(1) if m else None


def ir_positions(text: str, srcdir: Optional[str] = None) -> Dict[str, Tuple[str, int, int]]:
    """node name -> (source file, 1-based line, 1-based column), from ``pos=``.

    XLS positions are 0-based, so both are incremented."""
    idx = diag.IrIndex(text, srcdir)
    return {n: v.pos[0] for n, v in idx.nodes.items() if v.pos}


_SEVERITY = {"error": SeverityE.Error, "warning": SeverityE.Warning, "info": SeverityE.Info}


def _marker(sev: str, msg: str, loc: Optional[diag.SrcLoc]) -> TaskMarker:
    if loc is None:
        return TaskMarker(msg=msg, severity=_SEVERITY[sev])
    return TaskMarker(msg=msg, severity=_SEVERITY[sev],
                      loc=TaskMarkerLoc(path=loc[0], line=loc[1], pos=loc[2]))


def source_markers(log_text: str, ir_text: str, srcdir: Optional[str],
                   severities=("error", "warning", "info")) -> List[TaskMarker]:
    """A marker on the source line of each IR node (or IR line) an XLS
    message points at; an unlocated marker for a message that points at none."""
    idx = diag.IrIndex(ir_text, srcdir) if ir_text else None
    return [_marker(d.severity, d.msg, d.loc) for d in diag.map_log(log_text, idx)
            if d.severity in severities]


def schedule_markers(sched_text: str, opt_ir_text: str, srcdir: Optional[str],
                     period: int, needed: int) -> List[TaskMarker]:
    """Explain a clock period XLS cannot meet: the critical path at the
    period it suggested, as an error on its last source line and a note on
    each source line along it."""
    idx = diag.IrIndex(opt_ir_text, srcdir)
    steps = diag.describe_path(diag.critical_path(diag.parse_schedule(sched_text), idx), idx)
    if not steps:
        return []
    where = " -> ".join(f"{os.path.basename(l[0])}:{l[1]}" if l else "?" for _, l in steps)
    end = next((l for _, l in reversed(steps) if l), None)
    out = [_marker("error", f"cannot meet clock_period_ps={period}: the critical path "
                            f"needs {needed} ps ({where})", end)]
    out += [_marker("info", f"on the critical path: {t}", l) for t, l in steps if l]
    return out


def _error(msg: str) -> TaskDataResult:
    return TaskDataResult(status=1, changed=False, output=[],
                          markers=[TaskMarker(msg=msg, severity=SeverityE.Error)])


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


async def Codegen(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    opt, cg = find_tool("opt_main"), find_tool("codegen_main")
    if opt is None or cg is None:
        return _error("opt_main / codegen_main not found on PATH or in $XLS_BIN")
    p = input.params
    irs = input_files(input, "xlsIR")
    if not irs:
        return _error("Codegen: no xlsIR input")
    if len(irs) > 1 and p.module_name:
        return _error("Codegen: module_name names one module, but there are "
                      f"{len(irs)} xlsIR inputs")
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    out: List[FileSet] = []
    markers: List[TaskMarker] = []
    status = 0
    for ir_path in irs:
        ir_text = _read(ir_path)
        top = ir_top(ir_text)
        module = p.module_name or re.sub(r"\W", "_", top or "top")
        srcdir = os.path.dirname(ir_path)
        opt_ir = os.path.join(rundir, f"{module}.opt.ir")
        sv = os.path.join(rundir, f"{module}.sv")
        sig = os.path.join(rundir, f"{module}.sig.textproto")
        sched = os.path.join(rundir, f"{module}.schedule.textproto")

        log = os.path.join(rundir, f"{module}.opt.log")
        st = await ctxt.exec([opt, ir_path, f"--output_path={opt_ir}"], logfile=log, cwd=rundir)
        if st != 0:
            markers += source_markers(_read(log), ir_text, srcdir)
            status = st
            continue
        markers += source_markers(_read(log), ir_text, srcdir, ("warning",))

        def codegen_cmd(period: int, sched_path: str, sv_path: str) -> List[str]:
            cmd = [cg, opt_ir, "--generator=pipeline",
                   f"--use_system_verilog={'true' if p.system_verilog else 'false'}",
                   f"--delay_model={p.delay_model}", f"--module_name={module}",
                   f"--output_verilog_path={sv_path}", f"--output_schedule_path={sched_path}",
                   f"--streaming_channel_data_suffix={p.data_suffix}",
                   f"--streaming_channel_valid_suffix={p.valid_suffix}",
                   f"--streaming_channel_ready_suffix={p.ready_suffix}"]
            if period:
                cmd.append(f"--clock_period_ps={period}")
            else:
                cmd.append(f"--pipeline_stages={p.pipeline_stages}")
            if p.input_valid_signal:
                cmd.append(f"--input_valid_signal={p.input_valid_signal}")
            if p.output_valid_signal:
                cmd.append(f"--output_valid_signal={p.output_valid_signal}")
            if p.reset:
                cmd += [f"--reset={p.reset}",
                        f"--reset_active_low={'true' if p.reset_active_low else 'false'}"]
            if p.source_annotations:
                cmd.append("--source_annotation_strategy=comment")
            return cmd + list(p.args or [])

        cmd = codegen_cmd(p.clock_period_ps, sched, sv) + [f"--output_signature_path={sig}"]
        log = os.path.join(rundir, f"{module}.codegen.log")
        st = await ctxt.exec(cmd, logfile=log, cwd=rundir)
        # positions come from the optimized IR, which is what codegen read
        opt_text = _read(opt_ir) or ir_text
        log_text = _read(log)
        if st != 0:
            found = source_markers(log_text, opt_text, srcdir)
            status = st
            needed = diag.suggested_period(log_text)
            if needed and p.clock_period_ps:
                # XLS names no nodes: schedule again at the period it can
                # meet, and report the path that sets it.
                dsched = os.path.join(rundir, f"{module}.critical.schedule.textproto")
                dst = await ctxt.exec(codegen_cmd(needed, dsched, os.devnull),
                                      logfile=os.path.join(rundir, f"{module}.critical.log"),
                                      cwd=rundir)
                path = schedule_markers(_read(dsched), opt_text, srcdir,
                                        p.clock_period_ps, needed) if dst == 0 else []
                if path:
                    found = path        # says what XLS's message says, and where
            markers += found
            continue
        markers += source_markers(log_text, opt_text, srcdir, ("warning",))
        if "--use_system_verilog=false" in cmd and re.search(r"=\s*assert\(", opt_text):
            markers.append(TaskMarker(
                msg=f"{module}: Verilog-2001 output drops the design's assertions "
                    f"(--use_system_verilog=false)", severity=SeverityE.Warning))
        if p.source_annotations:
            # XLS prints its 0-based positions in the comments: make them
            # 1-based, and the file names absolute, so they can be followed.
            idx = diag.IrIndex(opt_text, srcdir)
            text = diag.absolute_source_comments(
                _read(sv), {idx.raw_files[n]: idx.files[n] for n in idx.files})
            text = diag.fix_source_comments(text, idx.files.values())
            with open(sv, "w") as f:
                f.write(text)
        out += [FileSet(src=input.name, filetype="systemVerilogSource", basedir=rundir,
                        files=[os.path.basename(sv)], params={"module": module},
                        attributes=[f"module={module}"]),
                FileSet(src=input.name, filetype="xlsModuleSignature", basedir=rundir,
                        files=[os.path.basename(sig)], params={"module": module},
                        attributes=[f"module={module}"]),
                FileSet(src=input.name, filetype="xlsOptIR", basedir=rundir,
                        files=[os.path.basename(opt_ir)], params={"module": module},
                        attributes=[f"module={module}"]),
                FileSet(src=input.name, filetype="xlsSchedule", basedir=rundir,
                        files=[os.path.basename(sched)], params={"module": module},
                        attributes=[f"module={module}"])]
    return TaskDataResult(status=status, changed=True, output=out, markers=markers)


def _defining_file(srcs: List[str], top: str) -> Optional[str]:
    """The DSLX file that defines function or proc *top*: the only one, or
    the one whose text declares it."""
    if len(srcs) == 1:
        return srcs[0]
    pat = re.compile(r"^\s*(?:pub\s+)?(?:fn|proc)\s+" + re.escape(top) + r"\b", re.M)
    found = [f for f in srcs if pat.search(_read(f))]
    return found[0] if len(found) == 1 else None


async def DslxToIR(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    conv = find_tool("ir_converter_main")
    if conv is None:
        return _error("ir_converter_main not found on PATH or in $XLS_BIN")
    p = input.params
    srcs = input_files(input, "dslxSource")
    if not srcs:
        return _error("DslxToIR: no dslxSource input")
    tops = list(dict.fromkeys(([p.top] if p.top else []) + list(p.tops or [])))
    if not tops:
        return _error("DslxToIR: give `top` or `tops`")
    stdlib = p.stdlib_path or os.environ.get("XLS_DSLX_STDLIB_PATH", "")
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)
    from .dslx import dslx_path
    search = dslx_path(input, p.dslx_path)
    irs, status = [], 0
    markers: List[TaskMarker] = []
    for top in tops:
        src = _defining_file(srcs, top)
        if src is None:
            markers.append(TaskMarker(
                msg=f"DslxToIR: no single dslxSource file defines `{top}`",
                severity=SeverityE.Error))
            status = 1
            continue
        ir = os.path.join(rundir, f"{top}.ir")
        cmd = [conv, src, f"--top={top}", f"--output_file={ir}"]
        if search:
            cmd.append(f"--dslx_path={':'.join(search)}")
        if stdlib:
            cmd.append(f"--dslx_stdlib_path={stdlib}")
        st = await ctxt.exec(cmd, logfile=os.path.join(rundir, f"{top}.ir_converter.log"),
                             cwd=rundir)
        if st != 0:
            status = st
            continue
        irs.append(os.path.basename(ir))
    if status:
        return TaskDataResult(status=status, changed=True, output=[], markers=markers)
    return TaskDataResult(status=0, changed=True, output=[
        FileSet(src=input.name, filetype="xlsIR", basedir=rundir, files=irs)])
