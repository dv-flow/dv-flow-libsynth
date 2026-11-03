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


def _pass_proc(name: str) -> str:
    """A proc that adds 1 three times: enough depth to take 1..3 stages."""
    ci = ("strictness=proven_mutually_exclusive, flow_control=ready_valid, flop_kind=none")
    return f"""package {name}

top proc {name}<a: bits[8] in, o: bits[8] out>(s: (), init={{()}}) {{
  chan_interface a(direction=receive, kind=streaming, {ci})
  chan_interface o(direction=send, kind=streaming, {ci})
  t: token = after_all(id=2)
  r: (token, bits[8]) = receive(t, channel=a, id=3)
  rt: token = tuple_index(r, index=0, id=4)
  d: bits[8] = tuple_index(r, index=1, id=5)
  one: bits[8] = literal(value=1, id=6)
  x1: bits[8] = add(d, one, id=7)
  x2: bits[8] = add(x1, one, id=8)
  x3: bits[8] = add(x2, one, id=9)
  s1: token = send(rt, x3, channel=o, id=10)
  s: () = state_read(state_element=s, id=1)
  nv: () = next_value(state_element=s, value=s, id=11)
}}
"""


@needs_xls
def test_per_module_codegen_options(dvflow, tmp_path):
    import json
    (tmp_path / "blk_a.ir").write_text(_pass_proc("blk_a"))
    (tmp_path / "blk_b.ir").write_text(_pass_proc("blk_b"))
    (tmp_path / "opts.json").write_text(json.dumps({
        "format": "xls.codegen-options",
        "modules": {"blk_b": {"pipeline_stages": 3, "args": ["--flop_inputs=true"]}}}))
    body = f"""\
  - name: irs
    uses: std.FileSet
    with: {{type: xlsIR, base: "{tmp_path}", include: ["blk_a.ir", "blk_b.ir"]}}
  - name: opts
    uses: std.FileSet
    with: {{type: xlsCodegenOptions, base: "{tmp_path}", include: ["opts.json"]}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [irs, opts]
    with: {{pipeline_stages: 1}}
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.rtl")
    assert status == 0, markers
    out = outs["t.rtl"]
    stages = {Path(p).name: Path(p).read_text().count("stages {")
              for p in _files(out, "xlsSchedule")}
    assert stages == {"blk_a.schedule.textproto": 1, "blk_b.schedule.textproto": 3}
    (tc,) = _files(out, "xlsToolchain")
    rec = json.loads(Path(tc).read_text())
    assert [t["tool"] for t in rec["tools"]] == ["opt_main", "codegen_main"]
    assert all(len(t["sha256"]) == 64 for t in rec["tools"])


@needs_xls
def test_per_module_codegen_options_reject_unknown_keys(dvflow, tmp_path):
    import json
    (tmp_path / "blk_a.ir").write_text(_pass_proc("blk_a"))
    (tmp_path / "opts.json").write_text(json.dumps({"modules": {"blk_a": {"stages": 3}}}))
    body = f"""\
  - name: irs
    uses: std.FileSet
    with: {{type: xlsIR, base: "{tmp_path}", include: ["blk_a.ir"]}}
  - name: opts
    uses: std.FileSet
    with: {{type: xlsCodegenOptions, base: "{tmp_path}", include: ["opts.json"]}}
  - name: rtl
    uses: synth.xls.Codegen
    needs: [irs, opts]
"""
    status, markers, _ = _run(dvflow, tmp_path, body, "t.rtl")
    assert status != 0 and any("unknown codegen options" in m.msg for m in markers)
