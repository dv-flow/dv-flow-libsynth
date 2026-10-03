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
