"""synth.xls.Equiv."""
import json
from pathlib import Path

import pytest
from dv_flow.mgr import SeverityE
from pytest_dfm import *  # noqa: F401,F403  (the dvflow fixture)

from dv_flow.libsynth.xls.codegen import find_tool
from dv_flow.libsynth.xls.equiv import verdict

DATA = Path(__file__).parent / "data" / "equiv"
needs_equiv = pytest.mark.skipif(find_tool("check_ir_equivalence_main") is None,
                                 reason="check_ir_equivalence_main not on PATH or in $XLS_BIN")


def test_verdicts():
    assert verdict("Verified equivalent\n") == ("equivalent", "")
    assert verdict("Verified NOT equivalent; results differ for input: bits[7]:0x0\n") == \
        ("not-equivalent", "results differ for input: bits[7]:0x0")
    v, d = verdict("Error: INVALID_ARGUMENT: Cannot prove equivalence of functions with "
                   "differing return types: bits[7] vs bits[8]\n=== Source Location Trace")
    assert v == "error" and d.startswith("INVALID_ARGUMENT")
    assert verdict("")[0] == "inconclusive"


def _run(dvflow, tmp_path, a, b, extra=""):
    p = tmp_path / "flow.dv"
    p.write_text(f"""\
package:
  name: t
  imports: [synth.xls]
  tasks:
  - name: a
    uses: std.FileSet
    with: {{type: xlsIR, base: "{DATA}", include: ["{a}"]}}
  - name: b
    uses: std.FileSet
    with: {{type: xlsIR, base: "{DATA}", include: ["{b}"]}}
  - name: eq
    uses: synth.xls.Equiv
    needs: [a, b]
{extra}""")
    markers, outputs = [], {}

    def listen(t, reason):
        if reason == "leave":
            markers.extend(t.result.markers)
            outputs[t.name] = t.output
    status, _ = dvflow.runFlow(str(p), "t.eq", listener=listen)
    rep = None
    if "t.eq" in outputs and outputs["t.eq"].output:
        fs = outputs["t.eq"].output[0]
        rep = json.loads(Path(fs.basedir, "equiv.json").read_text())
    return status, markers, rep


@needs_equiv
def test_equivalent(dvflow, tmp_path):
    status, markers, rep = _run(dvflow, tmp_path, "twice_add.ir", "twice_shift.ir")
    assert status == 0, markers
    assert rep["verdict"] == "equivalent"


@needs_equiv
def test_not_equivalent_fails_with_the_counterexample(dvflow, tmp_path):
    status, markers, rep = _run(dvflow, tmp_path, "twice_add.ir", "plus_one.ir")
    assert status != 0 and rep["verdict"] == "not-equivalent"
    err = [m for m in markers if m.severity == SeverityE.Error and "NOT equivalent" in m.msg]
    assert err and "results differ for input" in err[0].msg


@needs_equiv
def test_not_equivalent_is_data_without_gate(dvflow, tmp_path):
    status, markers, rep = _run(dvflow, tmp_path, "twice_add.ir", "plus_one.ir",
                                "    with: {gate: false}\n")
    assert status == 0 and rep["verdict"] == "not-equivalent"
    assert any(m.severity == SeverityE.Warning for m in markers)


@needs_equiv
def test_pairs_by_top_name(dvflow, tmp_path):
    p = tmp_path / "flow.dv"
    p.write_text(f"""\
package:
  name: t
  imports: [synth.xls]
  tasks:
  - name: irs
    uses: std.FileSet
    with: {{type: xlsIR, base: "{DATA}", include: ["twice_add.ir", "twice_shift.ir", "plus_one.ir"]}}
  - name: eq
    uses: synth.xls.Equiv
    needs: [irs]
    with: {{pairs: ["f=g", "f=h"], gate: false}}
""")
    markers, outputs = [], {}

    def listen(t, reason):
        if reason == "leave":
            markers.extend(t.result.markers)
            outputs[t.name] = t.output
    status, _ = dvflow.runFlow(str(p), "t.eq", listener=listen)
    assert status == 0, markers
    fs = outputs["t.eq"].output[0]
    rep = json.loads(Path(fs.basedir, "equiv.json").read_text())
    assert rep["verdict"] == "not-equivalent" and "verdict=not-equivalent" in fs.attributes
    assert [r["verdict"] for r in rep["pairs"]] == ["equivalent", "not-equivalent"]


def test_exhaustive_bench_matches_inputs_by_position():
    from dv_flow.libsynth.xls.equiv import _ports, exhaustive_tb
    sig = ('data_ports {\n  direction: PORT_DIRECTION_INPUT\n  name: "a"\n  width: 3\n}\n'
           'data_ports {\n  direction: PORT_DIRECTION_OUTPUT\n  name: "out"\n  width: 2\n}\n')
    ins, outs = _ports(sig)
    assert (ins, outs) == ([("a", 3)], [("out", 2)])
    tb = exhaustive_tb([("a", 3), ("b", 1)], [("x", 3), ("y", 1)], "out", "o", 2)
    assert "lhs ul (.a(i0), .b(i1), .out(yl));" in tb
    assert "rhs ur (.x(i0), .y(i1), .o(yr));" in tb
    assert "v < (64'd1 << 4)" in tb


@needs_equiv
def test_inconclusive_falls_back_to_every_input(dvflow, tmp_path):
    # gcd_binary at 8 bits: the solver does not finish in 5 s, and two 8-bit
    # inputs are within exhaustive_max_bits.
    import shutil
    if shutil.which("verilator") is None:
        pytest.skip("verilator not on PATH")
    status, markers, rep = _run(dvflow, tmp_path, "gcd_bin_sv.ir", "gcd_bin_dslx.ir",
                                "    with: {timeout_s: 5}\n")
    assert status == 0, markers
    assert rep["method"] == "exhaustive", rep
    assert rep["verdict"] == "equivalent" and rep["detail"] == "exhaustive: all 65536 inputs agree"


@needs_equiv
def test_exhaustive_finds_the_first_mismatch(dvflow, tmp_path):
    import shutil
    if shutil.which("verilator") is None:
        pytest.skip("verilator not on PATH")
    import re
    bad = (DATA / "gcd_bin_sv.ir").read_text()
    # Break the function: change the first literal 1 to 0. (An op cannot be
    # swapped in place: XLS checks a node's name against its op.)
    bad, n = re.subn(r"literal\(value=1,", "literal(value=0,", bad, count=1)
    assert n == 1
    (tmp_path / "bad.ir").write_text(bad)
    p = tmp_path / "flow.dv"
    p.write_text(f"""\
package:
  name: t
  imports: [synth.xls]
  tasks:
  - name: a
    uses: std.FileSet
    with: {{type: xlsIR, base: "{tmp_path}", include: ["bad.ir"]}}
  - name: b
    uses: std.FileSet
    with: {{type: xlsIR, base: "{DATA}", include: ["gcd_bin_dslx.ir"]}}
  - name: eq
    uses: synth.xls.Equiv
    needs: [a, b]
    with: {{timeout_s: 5, gate: false}}
""")
    outputs = {}

    def listen(t, reason):
        if reason == "leave":
            outputs[t.name] = t.output
    dvflow.runFlow(str(p), "t.eq", listener=listen)
    fs = outputs["t.eq"].output[0]
    rep = json.loads(Path(fs.basedir, "equiv.json").read_text())
    assert rep["verdict"] == "not-equivalent", rep
    if rep["method"] == "exhaustive":
        assert "first:" in rep["detail"]


@needs_equiv
def test_a_solver_timeout_leaves_no_error_marker(dvflow, tmp_path):
    import shutil
    if shutil.which("verilator") is None:
        pytest.skip("verilator not on PATH")
    status, markers, rep = _run(dvflow, tmp_path, "gcd_bin_sv.ir", "gcd_bin_dslx.ir",
                                "    with: {timeout_s: 2}\n")
    assert status == 0 and rep["verdict"] == "equivalent"
    assert not [m for m in markers if m.severity == SeverityE.Error], markers
