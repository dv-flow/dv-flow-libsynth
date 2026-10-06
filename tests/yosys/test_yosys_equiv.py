"""synth.yosys.Equiv: a netlist proven equivalent to its RTL, or not."""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from dv_flow.mgr import SeverityE
from pytest_dfm import *  # noqa: F401,F403  (the dvflow fixture)

from dv_flow.libsynth.yosys import equiv

DATA = Path(__file__).parent / "data"
needs_yosys = pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not on PATH")


def _fs(filetype, basedir, files, attributes=(), defines=()):
    return SimpleNamespace(type="std.FileSet", filetype=filetype, basedir=basedir,
                           files=list(files), attributes=list(attributes),
                           defines=list(defines), incdirs=[])


def test_the_netlist_and_its_models_are_the_gate_the_rest_the_gold():
    attrs = ["family=ice40", "target=ice40", "top=m"]
    inp = SimpleNamespace(inputs=[
        _fs("systemVerilogSource", "/rtl", ["m.sv"], ["module=m"]),
        _fs("verilogSource", "/share", ["ice40/cells_sim.v"], ["yosysCellModels"] + attrs,
            defines=["NO_ICE40_DEFAULT_ASSIGNMENTS"]),
        _fs("verilogSource", "/syn", ["netlist.v"], ["format=verilog"] + attrs),
        _fs("verilogSource", "/share", ["simcells.v"], ["yosysCellModels"]),
    ])
    gold, gate = equiv.split(inp)
    assert [f[0] for f in gold.files] == ["/rtl/m.sv"] and gold.tops == ["m"]
    assert [f[0] for f in gate.files] == ["/syn/netlist.v"] and gate.tops == ["m"]
    # Yosys's own cells (simcells.v) are not read; the iCE40 models are, with their defines
    assert gate.models == [("/share/ice40/cells_sim.v", ["NO_ICE40_DEFAULT_ASSIGNMENTS"], [])]
    text = equiv.script(gold, gate, "m", 4, None)
    assert "read_verilog -overwrite -DNO_ICE40_DEFAULT_ASSIGNMENTS /share/ice40/cells_sim.v" in text
    assert "equiv_simple -seq 4" in text and text.rstrip().endswith("equiv_status")


def test_status_is_read_from_the_last_equiv_status():
    log = ("Found 10 $equiv cells in equiv:\n  Of those cells 7 are proven and 3 are unproven.\n"
           "Found 10 $equiv cells in equiv:\n  Of those cells 10 are proven and 0 are unproven.\n")
    assert equiv.status(log) == (10, 10, 0)
    assert equiv.status("nothing") is None


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


def _report(outs, task):
    (fs,) = [o for o in outs[task].output if getattr(o, "filetype", None) == "yosysEquivReport"]
    return json.loads((Path(fs.basedir) / fs.files[0]).read_text())


@needs_yosys
@pytest.mark.parametrize("target", ["generic", "ice40", "ecp5"])
def test_each_targets_netlist_is_equivalent(dvflow, tmp_path, target):
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: verilogSource, base: "{DATA}", include: [count.v]}}
  - name: syn
    uses: synth.yosys.Synth
    needs: [src]
    with: {{target: {target}, top: count}}
  - name: eq
    uses: synth.yosys.Equiv
    needs: [src, syn]
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.eq")
    assert status == 0, [m.msg for m in markers]
    r = _report(outs, "t.eq")
    assert (r["verdict"], r["top"], r["target"], r["unproven"]) == \
        ("equivalent", "count", target, 0)
    assert r["cells"] > 0


@needs_yosys
@pytest.mark.parametrize("gate, want", [(True, 1), (False, 0)])
def test_a_different_netlist_is_not_equivalent(dvflow, tmp_path, gate, want):
    """The RTL counts up; the netlist is synthesized from the counter that
    counts down. The search finds the difference."""
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: verilogSource, base: "{DATA}", include: [count.v]}}
  - name: src-down
    uses: std.FileSet
    with: {{type: verilogSource, base: "{DATA}", include: [count.v], defines: [COUNT_DOWN]}}
  - name: syn
    uses: synth.yosys.Synth
    needs: [src-down]
    with: {{top: count}}
  - name: eq
    uses: synth.yosys.Equiv
    needs: [src, syn]
    with: {{gate: {str(gate).lower()}}}
"""
    status, markers, outs = _run(dvflow, tmp_path, body, "t.eq")
    assert (status != 0) == bool(want)
    (m,) = [m for m in markers if "NOT equivalent" in m.msg]
    assert m.severity == (SeverityE.Error if gate else SeverityE.Warning)
    if not gate:
        r = _report(outs, "t.eq")
        assert r["verdict"] == "not-equivalent" and r["unproven"] > 0
        assert "sat -seq" in r["method"]


@needs_yosys
def test_no_netlist_is_an_error(dvflow, tmp_path):
    body = f"""\
  - name: src
    uses: std.FileSet
    with: {{type: verilogSource, base: "{DATA}", include: [count.v]}}
  - name: eq
    uses: synth.yosys.Equiv
    needs: [src]
"""
    status, markers, _ = _run(dvflow, tmp_path, body, "t.eq")
    assert status != 0 and any("no netlist input" in m.msg for m in markers)
