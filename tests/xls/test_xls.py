"""synth.xls.DslxToIR and synth.xls.Codegen, run through dv-flow (pytest-dfm)."""
import os
import re
from pathlib import Path

import pytest
from dv_flow.mgr import SeverityE
from pytest_dfm import *  # noqa: F401,F403  (the dvflow fixture)

from dv_flow.libsynth.xls.codegen import find_tool, ir_positions

DATA = Path(__file__).parent / "data"
needs_xls = pytest.mark.skipif(
    not all(find_tool(t) for t in ("opt_main", "codegen_main", "ir_converter_main")),
    reason="XLS tools not on PATH or in $XLS_BIN")


def _flow(tmp_path, body: str) -> str:
    p = tmp_path / "flow.dv"
    p.write_text("package:\n  name: t\n  imports: [synth.xls]\n  tasks:\n" + body)
    return str(p)


def _run(dvflow, tmp_path, body, task):
    markers, outputs = [], {}

    def listen(t, reason):
        if reason == "leave":
            markers.extend(t.result.markers)
            outputs[t.name] = t.output
    status, _ = dvflow.runFlow(_flow(tmp_path, body), task, listener=listen)
    return status, markers, outputs


def _files(out, filetype):
    return [os.path.join(fs.basedir, f) for fs in out.output
            if fs.filetype == filetype for f in fs.files]


@needs_xls
@pytest.mark.skipif(not os.environ.get("XLS_DSLX_STDLIB_PATH"),
                    reason="XLS_DSLX_STDLIB_PATH not set")
def test_dslx_to_verilog(dvflow, tmp_path):
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: dslxSource, base: "{DATA}", include: ["acc.x"]}}
  - name: ir
    uses: synth.xls.DslxToIR
    needs: [src]
    with: {{top: acc}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [ir]
    with: {{module_name: acc_blk, pipeline_stages: 2}}
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.rtl")
    assert status == 0, markers
    out = outs["t.rtl"]
    sv = _files(out, "systemVerilogSource")
    assert len(sv) == 1 and "module acc_blk(" in Path(sv[0]).read_text()
    # source annotations are on by default: comments name acc.x
    assert re.search(r"// \S*acc\.x:\d+:\d+", Path(sv[0]).read_text())
    sig = Path(_files(out, "xlsModuleSignature")[0]).read_text()
    assert 'module_name: "acc_blk"' in sig and 'valid_port_name: "_in_ch_vld"' in sig
    assert _files(out, "xlsOptIR") and _files(out, "xlsSchedule")


@needs_xls
def test_codegen_error_points_at_the_source_line(dvflow, tmp_path):
    body = f"""\
  - name: ir
    uses: std.FileSet
    with: {{type: xlsIR, base: "{DATA}", include: ["bad.ir"]}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [ir]
"""
    status, markers, _ = _run(dvflow, tmp_path, body, "t.rtl")
    assert status != 0
    located = [m for m in markers if m.loc is not None]
    assert located, markers
    m = located[0]
    # pos=[(0,6,10)] is 0-based: line 7, column 11 of bad.src
    assert m.severity == SeverityE.Error and "operand 1 of s" in m.msg
    assert (os.path.basename(m.loc.path), m.loc.line, m.loc.pos) == ("bad.src", 7, 11)


def test_ir_positions_are_one_based():
    pos = ir_positions((DATA / "bad.ir").read_text(), str(DATA))
    assert pos["s"] == (str(DATA / "bad.src"), 7, 11)


def _located(markers):
    return [(m.severity, os.path.basename(m.loc.path), m.loc.line) for m in markers
            if m.loc is not None]


@needs_xls
def test_optimizer_error_names_both_sends(dvflow, tmp_path):
    # Two sends on one channel: XLS names the nodes s1 and s2, unquoted.
    body = f"""\
  - name: ir
    uses: std.FileSet
    with: {{type: xlsIR, base: "{DATA}", include: ["twosend.ir"]}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [ir]
"""
    status, markers, _ = _run(dvflow, tmp_path, body, "t.rtl")
    assert status != 0
    assert sorted(_located(markers)) == [(SeverityE.Error, "twosend.src", 8),
                                         (SeverityE.Error, "twosend.src", 10)]
    assert all("Running pass" not in m.msg for m in markers)


@needs_xls
def test_unmet_clock_period_reports_the_critical_path(dvflow, tmp_path):
    body = f"""\
  - name: ir
    uses: std.FileSet
    with: {{type: xlsIR, base: "{DATA}", include: ["slow.ir"]}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [ir]
    with: {{delay_model: asap7, clock_period_ps: 100}}
"""
    status, markers, _ = _run(dvflow, tmp_path, body, "t.rtl")
    assert status != 0
    err = [m for m in markers if m.severity == SeverityE.Error and m.loc is not None]
    assert len(err) == 1 and "critical path" in err[0].msg, markers
    assert "clock_period_ps=100" in err[0].msg
    # the umul on line 10 or 11 sets the period; each step is a note
    assert err[0].loc.line in (10, 11)
    notes = [m for m in markers if m.severity == SeverityE.Info and m.loc is not None]
    assert notes and all(os.path.basename(m.loc.path) == "slow.src" for m in notes)


@needs_xls
def test_source_comments_are_one_based_and_absolute(dvflow, tmp_path):
    body = f"""\
  - name: ir
    uses: std.FileSet
    with: {{type: xlsIR, base: "{DATA}", include: ["slow.ir"]}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [ir]
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.rtl")
    assert status == 0, markers
    sv = Path(_files(outs["t.rtl"], "systemVerilogSource")[0]).read_text()
    src = str(DATA / "slow.src")
    lines = {int(l) for l in re.findall(re.escape(src) + r":(\d+):5", sv)}
    assert lines and lines <= {10, 11, 12}, sv     # pos lines 9..11, 0-based
