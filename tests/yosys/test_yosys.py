"""synth.yosys.Synth: the script it writes, its log markers, and Yosys runs."""
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from dv_flow.mgr import SeverityE
from pytest_dfm import *  # noqa: F401,F403  (the dvflow fixture)

from dv_flow.libsynth.yosys import synth

DATA = Path(__file__).parent / "data"
needs_yosys = pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not on PATH")


def _p(**kw):
    d = dict(target="generic", top="", device="", flatten=True, dsp=True, bram=True,
             frontend="auto", formats=["json"], sim_netlist=True, synth_args=[], args=[],
             warnings=True)
    d.update(kw)
    return SimpleNamespace(**d)


# -- the synthesis command, per target ---------------------------------------

def test_generic_flattens_and_picks_its_top():
    t = synth.TARGETS["generic"]
    assert synth.synth_command(t, _p(), None) == "synth -auto-top -flatten"
    assert synth.synth_command(t, _p(flatten=False), "m") == "synth -top m"


def test_one_parameter_maps_to_each_familys_flag():
    p = _p(dsp=False, bram=False, flatten=False)
    assert synth.synth_command(synth.TARGETS["ice40"], _p(device="up"), "m") == \
        "synth_ice40 -top m -device up -dsp"
    assert synth.synth_command(synth.TARGETS["ecp5"], p, "m") == \
        "synth_lattice -top m -family ecp5 -noflatten -nodsp -nobram"
    assert synth.synth_command(synth.TARGETS["nexus"], _p(), "m") == \
        "synth_lattice -top m -family lifcl"
    assert synth.synth_command(synth.TARGETS["xilinx"], _p(device="xc7"), "m") == \
        "synth_xilinx -top m -family xc7 -flatten"


def test_a_device_on_a_target_without_one_is_an_error():
    with pytest.raises(ValueError, match="takes no device"):
        synth.synth_command(synth.TARGETS["gatemate"], _p(device="x"), "m")


def test_user_commands_come_before_the_writes():
    src = synth.Sources([("/a.v", False)], [], ["X=1"], [], [])
    text = synth.script(src, synth.TARGETS["generic"], _p(args=["opt -full"]), "m", "/r", None)
    lines = text.splitlines()
    assert lines[0] == "read_verilog -DX=1 /a.v"
    assert lines.index("opt -full") < next(i for i, l in enumerate(lines) if l.startswith("write_"))
    assert any(l.startswith("tee -q -o /r/stat.json stat -json") for l in lines)
    assert "write_verilog -noattr /r/netlist.v" in lines     # sim_netlist


# -- the log -------------------------------------------------------------------

def test_errors_and_warnings_become_markers(tmp_path):
    log = ("bad.v:2: ERROR: syntax error, unexpected ';'\n"
           "Warning: Replacing memory \\m with list of registers. See x.v:7\n"
           "Warning: wire '\\q' is assigned in a block at y.v:3.5-3.20.\n"
           "Warning: wire '\\q' is assigned in a block at y.v:3.5-3.20.\n")
    ms = synth.log_markers(log)
    assert [(m.severity, m.loc.path if m.loc else None, m.loc.line if m.loc else None)
            for m in ms] == [(SeverityE.Error, "bad.v", 2), (SeverityE.Warning, None, None),
                             (SeverityE.Warning, "y.v", 3)]


def test_a_line_of_xls_verilog_maps_to_its_source(tmp_path):
    v = tmp_path / "m.sv"
    v.write_text("module m;\n  // /src/f.sv:12:5\n  assign x = y;\nendmodule\n")
    (m,) = synth.log_markers(f"{v}:3: ERROR: oops\n")
    assert (m.loc.path, m.loc.line, m.loc.pos) == ("/src/f.sv", 12, 5)


def test_summary_of_the_top():
    stat = {"modules": {"\\m": {"num_cells": 3, "num_wire_bits": 9,
                                "num_cells_by_type": {"$_NOT_": 1, "SB_LUT4": 2}}}}
    assert synth.summarize(stat, "m") == {"cells": 3, "wire_bits": 9, "cells._NOT_": 1,
                                          "cells.SB_LUT4": 2}


# -- Yosys -----------------------------------------------------------------------

def _run(dvflow, tmp_path, body, task):
    p = tmp_path / "flow.dv"
    p.write_text("package:\n  name: t\n  imports: [synth.yosys]\n  tasks:\n" + body)
    markers, outputs = [], {}

    def listen(t, reason):
        if reason == "leave":
            markers.extend(t.result.markers)
            outputs[t.name] = t.output
    status, _ = dvflow.runFlow(str(p), task, listener=listen)
    return status, markers, outputs


def _src(name, ftype, defines=None):
    d = f", defines: {defines}" if defines else ""
    return f"""\
  - name: src
    uses: std.FileSet
    with: {{type: {ftype}, base: "{DATA}", include: ["{name}"]{d}}}
"""


@needs_yosys
@pytest.mark.parametrize("target, cell", [("generic", "$_SDFFE_PP0P_"),
                                          ("ice40", "SB_LUT4"), ("ecp5", "LUT4")])
def test_counter_for_each_target(dvflow, tmp_path, target, cell):
    body = _src("count.v", "verilogSource") + f"""\
  - name: syn
    uses: synth.yosys.Synth
    needs: [src]
    with: {{target: {target}, top: count}}
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.syn")
    assert status == 0, markers
    out = outs["t.syn"].output
    net = [fs for fs in out if fs.filetype == "yosysNetlist"]
    assert len(net) == 1 and "format=json" in net[0].attributes
    assert f"family={target}" in net[0].attributes and "top=count" in net[0].attributes
    assert "frontend=yosys" in net[0].attributes
    stat = next(fs for fs in out if fs.filetype == "yosysStat")
    nums = synth.summarize(json.loads(Path(stat.basedir, "stat.json").read_text()), "count")
    assert nums[f"cells.{cell.lstrip('$')}"] > 0
    # simulation: the cell models, then the netlist
    sim = [fs for fs in out if fs.filetype == "verilogSource"]
    assert "yosysCellModels" in sim[0].attributes and sim[1].files == ["netlist.v"]
    assert all(os.path.isfile(os.path.join(sim[0].basedir, f)) for f in sim[0].files)
    if target == "ice40":
        assert sim[0].defines == ["NO_ICE40_DEFAULT_ASSIGNMENTS"]


@needs_yosys
def test_defines_reach_the_reader(dvflow, tmp_path):
    body = _src("count.v", "verilogSource", "[COUNT_DOWN]") + """\
  - name: syn
    uses: synth.yosys.Synth
    needs: [src]
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.syn")
    assert status == 0, markers
    ys = Path(outs["t.syn"].output[0].basedir, "synth.ys").read_text()
    assert "read_verilog -DCOUNT_DOWN" in ys


@needs_yosys
def test_auto_frontend_falls_back_to_sv2v(dvflow, tmp_path):
    body = _src("arr2.sv", "systemVerilogSource") + """\
  - name: syn
    uses: synth.yosys.Synth
    needs: [src]
    with: {top: arr2}
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.syn")
    assert status == 0, markers
    net = next(fs for fs in outs["t.syn"].output if fs.filetype == "yosysNetlist")
    assert "frontend=sv2v" in net.attributes
    assert any(m.severity == SeverityE.Info and "sv2v" in m.msg for m in markers)


@needs_yosys
def test_yosys_frontend_reports_the_read_error_at_its_line(dvflow, tmp_path):
    body = _src("arr2.sv", "systemVerilogSource") + """\
  - name: syn
    uses: synth.yosys.Synth
    needs: [src]
    with: {top: arr2, frontend: yosys}
"""
    status, markers, _ = _run(dvflow, tmp_path, body, "t.syn")
    assert status != 0
    err = [m for m in markers if m.severity == SeverityE.Error and m.loc]
    assert err and err[0].loc.path.endswith("arr2.sv") and err[0].loc.line == 15
    assert "array indices" in err[0].msg


@needs_yosys
def test_syntax_error(dvflow, tmp_path):
    body = _src("bad.v", "verilogSource") + """\
  - name: syn
    uses: synth.yosys.Synth
    needs: [src]
"""
    status, markers, _ = _run(dvflow, tmp_path, body, "t.syn")
    assert status != 0
    # (the one located error; no generic "Command failed" marker)
    (err,) = [m for m in markers if m.severity == SeverityE.Error]
    assert err.loc.path.endswith("bad.v") and err.loc.line == 2


def test_stats_of_several_tops_are_merged_and_summed():
    a = {"creator": "y", "modules": {"\\a": {"num_cells": 2, "num_wire_bits": 3,
                                            "num_cells_by_type": {"$_NOT_": 2}}}}
    b = {"creator": "y", "modules": {"\\b": {"num_cells": 5, "num_wire_bits": 1,
                                            "num_cells_by_type": {"$_NOT_": 1, "$_XOR_": 4}}}}
    m = synth.merge_stats([a, b])
    assert sorted(m["modules"]) == ["\\a", "\\b"]
    assert synth.summarize(m, None) == {"cells": 7, "wire_bits": 4, "cells._NOT_": 3,
                                        "cells._XOR_": 4}


def test_unmodeled_cells_are_found(tmp_path):
    (tmp_path / "models.v").write_text('`include "more.vh"\nmodule LUT4(); endmodule\n')
    (tmp_path / "more.vh").write_text("module FF(); endmodule\n")
    (tmp_path / "net.v").write_text(
        "module top(a);\n  LUT4 _1_ (\n    .A(a)\n  );\n  FF \\q_reg[0] (.D(a));\n"
        "  always @* if (a) x = 1;\n  else if (a) x = 0;\n"
        "  MULT18X18D m0 (.A0(a));\n  MULT18X18D m1 (.A0(a));\nendmodule\n")
    assert synth.unmodeled_cells([str(tmp_path / "net.v")], [str(tmp_path / "models.v")]) \
        == {"MULT18X18D": 2}
