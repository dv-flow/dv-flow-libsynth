"""synth.xls.DslxTest: run a DSLX module's own tests in the DSLX interpreter.

`dslx_interpreter_main` prints, per test:

    [ RUN UNITTEST  ] name
    t.x:9:14-9:23                         <- 1-based span of the failing expression
    0009:     assert_eq(u8:5, y)          <- source context
    ~~~~~~~~~~~~~~~~~~~^-------^ FailureError: The program being interpreted failed!
      lhs: u8:5                           <- detail, indented
    [        FAILED ] name                <- or [            OK ]

and a summary, `[===============] 3 test(s) ran; 2 failed; 0 skipped.` A file
that does not parse or typecheck prints the span and the error, and no summary.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from dv_flow.mgr import (FileSet, SeverityE, TaskDataInput, TaskDataResult, TaskMarker,
                         TaskMarkerLoc, TaskRunCtxt)

from .codegen import find_tool

_RUN = re.compile(r"^\[ RUN (?:UNITTEST|QUICKCHECK)\s*\] (\S+)")
_OK = re.compile(r"^\[\s+OK \]")
_FAILED = re.compile(r"^\[\s+FAILED \] (\S+)")
_SPAN = re.compile(r"^(\S+?):(\d+):(\d+)-\d+:\d+$")
_CARET = re.compile(r"^~*\^[-^]*\s+(\w+): (.*)$")
_CONTEXT = re.compile(r"^\d{4}: ")
_SUMMARY = re.compile(r"^\[=+\] (\d+) test\(s\) ran; (\d+) failed; (\d+) skipped")


@dataclass
class DslxFailure:
    test: Optional[str]          # None: the module did not parse or typecheck
    kind: str
    msg: str
    path: Optional[str] = None
    line: int = -1
    col: int = -1


@dataclass
class DslxTestLog:
    passed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    failures: List[DslxFailure] = field(default_factory=list)
    ran: Optional[int] = None    # from the summary; None when there was none
    skipped: int = 0


def parse_test_log(text: str, srcdir: Optional[str] = None) -> DslxTestLog:
    out = DslxTestLog()
    test, span, cur = None, None, None
    for line in text.splitlines():
        m = _RUN.match(line)
        if m:
            test, span, cur = m.group(1), None, None
            continue
        if _OK.match(line):
            if test:
                out.passed.append(test)
            test, cur = None, None
            continue
        m = _FAILED.match(line)
        if m:
            out.failed.append(m.group(1))
            test, cur = None, None
            continue
        m = _SUMMARY.match(line)
        if m:
            out.ran, out.skipped = int(m.group(1)), int(m.group(3))
            continue
        m = _SPAN.match(line)
        if m:
            path = m.group(1)
            if srcdir and not os.path.isabs(path):
                path = os.path.join(srcdir, path)
            span, cur = (path, int(m.group(2)), int(m.group(3))), None
            continue
        m = _CARET.match(line)
        if m:
            path, ln, col = span or (None, -1, -1)
            cur = DslxFailure(test, m.group(1), m.group(2).strip(), path, ln, col)
            out.failures.append(cur)
            continue
        if cur is not None and line.startswith("  ") and not _CONTEXT.match(line):
            cur.msg += "; " + line.strip()
        elif cur is not None and _CONTEXT.match(line):
            cur = None
    return out


def dslx_path(input: TaskDataInput, extra) -> List[str]:
    """The module search path: each dslxSource fileset's base directory (so an
    upstream tree kept at its own paths imports as it does upstream), then
    `extra`."""
    dirs = [fs.basedir for fs in getattr(input, "inputs", []) or []
            if getattr(fs, "filetype", None) == "dslxSource"]
    return list(dict.fromkeys(dirs + list(extra or [])))


def _sources(input: TaskDataInput) -> List[str]:
    out = []
    for fs in getattr(input, "inputs", []) or []:
        if getattr(fs, "filetype", None) == "dslxSource":
            out += [os.path.join(fs.basedir, f) for f in fs.files]
    return list(dict.fromkeys(out))


async def DslxTest(ctxt: TaskRunCtxt, input: TaskDataInput) -> TaskDataResult:
    interp = find_tool("dslx_interpreter_main")
    if interp is None:
        return TaskDataResult(status=1, changed=False, output=[], markers=[TaskMarker(
            msg="dslx_interpreter_main not found on PATH or in $XLS_BIN",
            severity=SeverityE.Error)])
    p = input.params
    srcs = _sources(input)
    if not srcs:
        return TaskDataResult(status=1, changed=False, output=[], markers=[TaskMarker(
            msg="DslxTest: no dslxSource input", severity=SeverityE.Error)])
    stdlib = p.stdlib_path or os.environ.get("XLS_DSLX_STDLIB_PATH", "")
    search = ":".join(dslx_path(input, p.dslx_path))
    rundir = input.rundir
    os.makedirs(rundir, exist_ok=True)

    markers: List[TaskMarker] = []
    report, status = [], 0
    totals = {"tests_run": 0, "tests_passed": 0, "tests_failed": 0, "tests_skipped": 0}
    for src in srcs:
        log = os.path.join(rundir, os.path.basename(src) + ".log")
        cmd = [interp, src, f"--dslx_path={search}"]
        if stdlib:
            cmd.append(f"--dslx_stdlib_path={stdlib}")
        if p.test_filter:
            cmd.append(f"--test_filter={p.test_filter}")
        st = await ctxt.exec(cmd, logfile=log, cwd=rundir)
        with open(log, errors="replace") as f:
            res = parse_test_log(f.read(), rundir)
        for fl in res.failures:
            what = f"{fl.test}: " if fl.test else ""
            msg = f"{what}{fl.kind}: {fl.msg}"
            markers.append(TaskMarker(msg=msg, severity=SeverityE.Error,
                                      loc=TaskMarkerLoc(path=fl.path, line=fl.line, pos=fl.col))
                           if fl.path else TaskMarker(msg=msg, severity=SeverityE.Error))
        ok = st == 0 and res.ran is not None and not res.failed
        if not ok:
            status = 1
            if not res.failures:
                markers.append(TaskMarker(
                    msg=f"{os.path.basename(src)}: the interpreter exited {st} "
                        f"without reporting a failure (see {log})", severity=SeverityE.Error))
        totals["tests_run"] += res.ran or 0
        totals["tests_passed"] += len(res.passed)
        totals["tests_failed"] += len(res.failed)
        totals["tests_skipped"] += res.skipped
        report.append({"file": src, "status": "pass" if ok else "fail", "log": log,
                       "passed": res.passed, "failed": res.failed,
                       "skipped": res.skipped,
                       "failures": [asdict(f) for f in res.failures]})
    with open(os.path.join(rundir, "dslx_tests.json"), "w") as f:
        json.dump({"totals": totals, "files": report}, f, indent=2)
    ctxt.info(f"DSLX tests: {totals['tests_passed']} of {totals['tests_run']} passing "
              f"in {len(srcs)} file(s)")
    return TaskDataResult(status=status, changed=True, markers=markers, output=[
        FileSet(src=input.name, filetype="dslxTestReport", basedir=rundir,
                files=["dslx_tests.json"])])
