"""synth.xls.Lower re-entry: Structure.to_json/from_json and lower_structure
emit byte-identical files to the IR + schedule path."""
import json
import os
from pathlib import Path

import pytest
from pytest_dfm import *  # noqa: F401,F403  (the dvflow fixture)

from dv_flow.libsynth.xls import lower as L

IR = """\
package t

top fn f(a: bits[8] id=1, b: bits[8] id=2) -> bits[8] {
  literal.3: bits[8] = literal(value=5, id=3)
  add.4: bits[8] = add(a, literal.3, id=4)
  literal.5: bits[8][4] = literal(value=[1, 2, 3, 4], id=5)
  bit_slice.6: bits[2] = bit_slice(b, start=0, width=2, id=6)
  array_index.7: bits[8] = array_index(literal.5, indices=[bit_slice.6], id=7)
  ugt.8: bits[1] = ugt(add.4, b, id=8)
  ret sel.9: bits[8] = sel(ugt.8, cases=[array_index.7, add.4], id=9)
}
"""


def _sched(stages):
    out = ['schedules {', '  key: "f"', '  value {', '    function: "f"']
    for s, nodes in enumerate(stages):
        out += ["    stages {", f"      stage: {s}"]
        for n in nodes:
            out += ["      timed_nodes {", f'        node: "{n}"', "      }"]
        out += ["    }"]
    return "\n".join(out + ["  }", "}", ""])


SCHED = _sched([["a", "b", "add.4", "bit_slice.6"], ["array_index.7", "ugt.8"], ["sel.9"]])


@pytest.fixture
def inputs(tmp_path):
    ir, sched = tmp_path / "f.sched.ir", tmp_path / "f.schedule.textproto"
    ir.write_text(IR)
    sched.write_text(SCHED)
    return str(ir), str(sched)


def _tree(d):
    return {f: (Path(d) / f).read_bytes() for f in sorted(os.listdir(d))}


def test_round_trip_emits_identical_files(inputs, tmp_path):
    st = L.build_structure(*inputs, module="f_mod")
    a = L.emit_all(st, str(tmp_path / "A"))
    assert set(a) == {"v5", "v4_stages", "v4", "structure", "structure_full"}
    j = st.to_json()
    assert j["format"] == L.STRUCTURE_FORMAT and j["version"] == L.STRUCTURE_VERSION
    assert j["n_stages"] == 3 and json.loads(json.dumps(j)) == j
    st2 = L.Structure.from_json(json.loads(json.dumps(j)))
    assert st2.to_json() == j
    assert L.Structure.from_json(st.to_json_text()).to_json_text() == st.to_json_text()
    L.emit_all(st2, str(tmp_path / "B"))
    ta, tb = _tree(tmp_path / "A"), _tree(tmp_path / "B")
    assert sorted(ta) == sorted(tb) == ["f_mod.structure.full.json", "f_mod.structure.json",
                                        "f_mod.v4.sv", "f_mod.v4_stages.sv", "f_mod.v5.sv"]
    assert ta == tb
    # the file path, and lower_files (unchanged API) agree too
    L.lower_structure(a["structure_full"], str(tmp_path / "C"))
    L.lower_files(*inputs, str(tmp_path / "D"), module="f_mod")
    assert _tree(tmp_path / "C") == ta == _tree(tmp_path / "D")


def test_edited_structure_changes_the_output(inputs, tmp_path):
    a = L.lower_files(*inputs, str(tmp_path / "A"), module="f_mod")
    j = json.loads(Path(a["structure_full"]).read_text())
    # rename one SV identifier: V5 and V4 follow the structure, not the IR
    j["sv"]["add.4"] = "renamed_add"
    p = tmp_path / "edit.structure.full.json"
    p.write_text(json.dumps(j))
    b = L.lower_structure(str(p), str(tmp_path / "B"))
    v5 = Path(b["v5"]).read_text()
    assert "s_renamed_add" in v5 and v5 != Path(a["v5"]).read_text()


def test_bad_version_is_rejected(inputs):
    j = L.build_structure(*inputs).to_json()
    j["version"] = 999
    with pytest.raises(ValueError, match="version"):
        L.Structure.from_json(j)


def test_cli_from_structure(inputs, tmp_path):
    a = L.lower_files(*inputs, str(tmp_path / "A"), module="f_mod")
    assert L.main(["--from-structure", a["structure_full"], str(tmp_path / "B")]) == 0
    assert _tree(tmp_path / "A") == _tree(tmp_path / "B")
    assert L.main([*inputs, str(tmp_path / "C"), "--module", "f_mod"]) == 0
    assert _tree(tmp_path / "A") == _tree(tmp_path / "C")


def test_lower_task_reenters_from_structure(dvflow, inputs, tmp_path):
    a = L.lower_files(*inputs, str(tmp_path / "A"), module="f_mod")
    flow = tmp_path / "flow.dv"
    flow.write_text(f"""\
package:
  name: t
  imports: [synth.xls]
  tasks:
  - name: st
    uses: std.FileSet
    with: {{type: xlsLowerStructure, base: "{tmp_path / 'A'}", include: ["*.structure.full.json"]}}
  - name: ll
    uses: synth.xls.Lower
    needs: [st]
""")
    outs = {}

    def listen(t, reason):
        if reason == "leave":
            outs[t.name] = t.output
    status, _ = dvflow.runFlow(str(flow), "t.ll", listener=listen)
    assert status == 0
    files = {fs.filetype: [os.path.join(fs.basedir, f) for f in fs.files] for fs in outs["t.ll"].output}
    assert sorted(os.path.basename(f) for f in files["xlsLowerStructure"]) == \
        ["f_mod.structure.full.json", "f_mod.structure.json"]
    for f in files["systemVerilogSource"] + files["xlsV4Model"] + files["xlsLowerStructure"]:
        assert Path(f).read_bytes() == (tmp_path / "A" / os.path.basename(f)).read_bytes(), f
