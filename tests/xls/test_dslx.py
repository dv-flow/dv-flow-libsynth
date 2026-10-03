"""synth.xls.DslxTest, DslxToIR's import path, and Codegen's valid signals."""
import json
import os
import re
from pathlib import Path

import pytest
from dv_flow.mgr import SeverityE
from pytest_dfm import *  # noqa: F401,F403  (the dvflow fixture)

from dv_flow.libsynth.xls.codegen import find_tool
from dv_flow.libsynth.xls.dslx import parse_test_log

DATA = Path(__file__).parent / "data"
DSLX = DATA / "dslx"
needs_interp = pytest.mark.skipif(
    find_tool("dslx_interpreter_main") is None or not os.environ.get("XLS_DSLX_STDLIB_PATH"),
    reason="dslx_interpreter_main or XLS_DSLX_STDLIB_PATH missing")
needs_xls = pytest.mark.skipif(
    not all(find_tool(t) for t in ("opt_main", "codegen_main", "ir_converter_main"))
    or not os.environ.get("XLS_DSLX_STDLIB_PATH"),
    reason="XLS tools or XLS_DSLX_STDLIB_PATH missing")


def _run(dvflow, tmp_path, body, task):
    p = tmp_path / "flow.dv"
    p.write_text("package:\n  name: t\n  imports: [synth.xls]\n  tasks:\n" + body)
    markers, outputs = [], {}

    def listen(t, reason):
        if reason == "leave":
            markers.extend(t.result.markers)
            outputs[t.name] = t.output
    status, _ = dvflow.runFlow(str(p), task, listener=listen)
    return status, markers, outputs


def _files(out, filetype):
    return [os.path.join(fs.basedir, f) for fs in out.output
            if fs.filetype == filetype for f in fs.files]


LOG = """\
[ RUN UNITTEST  ] good
[            OK ]
[ RUN UNITTEST  ] bad
t.x:9:14-9:23
0008:     let y = f(u8:3);
0009:     assert_eq(u8:5, y)
~~~~~~~~~~~~~~~~~~~^-------^ FailureError: The program being interpreted failed!
  lhs: u8:5
  rhs: u8:4
  were not equal
0010: }
[        FAILED ] bad
[===============] 2 test(s) ran; 1 failed; 0 skipped.
"""


def test_log_failure_is_located_with_its_detail():
    log = parse_test_log(LOG, "/src")
    assert (log.passed, log.failed, log.ran) == (["good"], ["bad"], 2)
    (f,) = log.failures
    assert (f.test, f.kind, f.path, f.line, f.col) == ("bad", "FailureError", "/src/t.x", 9, 14)
    assert f.msg.endswith("lhs: u8:5; rhs: u8:4; were not equal")


def test_log_parse_error_has_no_summary():
    log = parse_test_log("p.x:1:25-1:26\n0001: fn f(x: u8) -> u8 { x + }\n"
                         "~~~~~~^ ParseError: Expected start of an expression; got: }\n")
    assert log.ran is None and log.failures[0].test is None
    assert log.failures[0].kind == "ParseError" and log.failures[0].line == 1


@needs_interp
def test_upstream_imports_resolve_from_the_fileset_base(dvflow, tmp_path):
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: dslxSource, base: "{DSLX}", include: ["up/top.x", "up/util/inc.x"]}}
  - name: test
    uses: synth.xls.DslxTest
    needs: [src]
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.test")
    assert status == 0, markers
    rep = json.loads(Path(_files(outs["t.test"], "dslxTestReport")[0]).read_text())
    assert rep["totals"]["tests_passed"] == 2 and rep["totals"]["tests_failed"] == 0


@needs_interp
def test_failing_test_is_an_error_at_the_assert(dvflow, tmp_path):
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: dslxSource, base: "{DSLX}", include: ["bad_tests.x"]}}
  - name: test
    uses: synth.xls.DslxTest
    needs: [src]
"""
    status, markers, _ = _run(dvflow, tmp_path, body, "t.test")
    assert status != 0
    err = [m for m in markers if m.severity == SeverityE.Error and m.loc]
    assert len(err) == 1, markers
    assert err[0].loc.path == str(DSLX / "bad_tests.x") and err[0].loc.line == 9
    assert err[0].msg.startswith("bad: FailureError") and "were not equal" in err[0].msg


@needs_xls
def test_function_with_valid_signals(dvflow, tmp_path):
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: dslxSource, base: "{DSLX}", include: ["up/top.x"]}}
  - name: ir
    uses: synth.xls.DslxToIR
    needs: [src]
    with: {{top: twice}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [ir]
    with:
      module_name: twice
      pipeline_stages: 2
      input_valid_signal: input_valid
      output_valid_signal: output_valid
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.rtl")
    assert status == 0, markers
    sv = Path(_files(outs["t.rtl"], "systemVerilogSource")[0]).read_text()
    assert re.search(r"input wire input_valid", sv) and re.search(r"output wire output_valid", sv)
    sig = Path(_files(outs["t.rtl"], "xlsModuleSignature")[0]).read_text()
    assert 'input_name: "input_valid"' in sig and 'output_name: "output_valid"' in sig
    assert re.search(r"latency: \d+", sig)


@needs_xls
def test_several_tops_each_from_its_file(dvflow, tmp_path):
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: dslxSource, base: "{DSLX}", include: ["up/top.x", "up/util/inc.x"]}}
  - name: ir
    uses: synth.xls.DslxToIR
    needs: [src]
    with: {{tops: [twice, inc]}}
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.ir")
    assert status == 0, markers
    irs = sorted(os.path.basename(f) for f in _files(outs["t.ir"], "xlsIR"))
    assert irs == ["inc.ir", "twice.ir"]


@needs_xls
def test_codegen_verilog_2001(dvflow, tmp_path):
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: dslxSource, base: "{DSLX}", include: ["up/top.x"]}}
  - name: ir
    uses: synth.xls.DslxToIR
    needs: [src]
    with: {{top: twice}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [ir]
    with: {{module_name: twice, pipeline_stages: 2, system_verilog: false}}
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.rtl")
    assert status == 0, markers
    v = Path(_files(outs["t.rtl"], "systemVerilogSource")[0]).read_text()
    assert "always_ff" not in v and "always @ (posedge clk)" in v
