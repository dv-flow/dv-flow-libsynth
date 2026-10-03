"""dv_flow.libsynth.xls.diag: XLS messages -> source positions (no XLS needed)."""
from dv_flow.libsynth.xls import diag

IR = """\
package p

file_number 0 "/src/a.sv"
file_number 1 "<Generated non-synthesizable function for q>"

top fn f(a: bits[8] id=1, b: bits[8] id=2) -> bits[8] {
  m1: bits[8] = umul(a, b, id=3, pos=[(0,9,4)])
  bit_slice.12: bits[4] = bit_slice(m1, start=0, width=4, id=12)
  zero_ext.13: bits[8] = zero_ext(bit_slice.12, new_bit_count=8, id=13)
  x: bits[8] = add(zero_ext.13, a, id=5, pos=[(0,11,4), (1,0,0)])
  ret a.1: bits[8] = sel(x, cases=[m1, x], default=a, id=6, pos=[(0,12,2)])
}
"""


def idx():
    return diag.IrIndex(IR)


def test_index_positions_operands_and_pseudo_files():
    i = idx()
    assert i.nodes["m1"].pos == [("/src/a.sv", 10, 5)]
    assert i.nodes["x"].pos == [("/src/a.sv", 12, 5)]          # <Generated ...> dropped
    assert i.nodes["zero_ext.13"].operands == ["bit_slice.12"]
    assert i.nodes["a.1"].operands == ["x", "m1"]
    assert i.node_at(9) == "zero_ext.13"


def test_a_node_without_pos_takes_the_nearest_one():
    i = idx()
    assert i.locate("bit_slice.12") == [("/src/a.sv", 10, 5)]  # its operand m1
    assert i.locate("zero_ext.13") == [("/src/a.sv", 10, 5)]


def test_parse_error_cites_the_ir_line():
    log = ('Error: INVALID_ARGUMENT: Declared type bits[9] does not match expected type '
           'bits[8] @ 7:21; @ p.ir\n=== Source Location Trace: ===\nxls/ir/ir_parser.cc:1503\n')
    [d] = diag.map_log(log, idx())
    assert d.msg == "Declared type bits[9] does not match expected type bits[8] (XLS node m1)"
    assert d.loc == ("/src/a.sv", 10, 5)


def test_pass_chain_is_stripped_and_short_names_are_not_words():
    log = ("Error: INVALID_ARGUMENT: Channel o is proven_mutually_exclusive, and x is a "
           "problem; proved that m1 is also active.; Running pass #209: Legalize "
           "[short: channel_legalization]; Failed as part of compound pass\n")
    i = idx()
    i.nodes["a"] = diag.IrNode("a", "param", 6, [("/src/a.sv", 2, 1)])
    ds = diag.map_log(log, i)
    assert [d.loc[1] for d in ds] == [12, 10]                  # x, m1 -- not "a"
    assert all("Running pass" not in d.msg for d in ds)


def test_unlocated_messages_and_glog_notes():
    log = ("E1003 17:15:23.721328  354017 run_pipeline_schedule.cc:347] Unable to schedule "
           "with the specified clock period (100 ps); finding the shortest...\n"
           "Error: INVALID_ARGUMENT: cannot achieve the specified clock period. Try "
           "`--clock_period_ps=569`.; Running pass #75: Pipeline Scheduling\n")
    ds = diag.map_log(log, idx())
    assert [(d.severity, d.loc) for d in ds] == [("info", None), ("error", None)]
    assert diag.suggested_period(log) == 569


SCHED = """
schedules { key: "f" value { function: "f"
  stages { stage: 0
    timed_nodes { node: "a" node_delay_ps: 0 path_delay_ps: 0 }
    timed_nodes { node: "m1" node_delay_ps: 300 path_delay_ps: 300 }
    timed_nodes { node: "bit_slice.12" node_delay_ps: 0 path_delay_ps: 300 }
    timed_nodes { node: "zero_ext.13" node_delay_ps: 0 path_delay_ps: 300 }
    timed_nodes { node: "x" node_delay_ps: 200 path_delay_ps: 500 }
  }
  stages { stage: 1
    timed_nodes { node: "a.1" node_delay_ps: 50 path_delay_ps: 50 }
  }
} }
"""


def test_critical_path_walks_back_through_operands():
    i = idx()
    sched = diag.parse_schedule(SCHED.replace("{ ", "{\n").replace(" }", "\n}")
                                .replace(" node_delay", "\nnode_delay")
                                .replace(" path_delay", "\npath_delay"))
    path = diag.critical_path(sched, i)
    assert [t.name for t in path] == ["m1", "bit_slice.12", "zero_ext.13", "x"]
    steps = diag.describe_path(path, i)
    assert [l[1] for _, l in steps] == [10, 12]                # one step per source line


def test_source_comments():
    v = ("  // /src/a.sv:9:4\n  assign p = a * b;\n  // <Generated q>:0:0\n"
         "  // other.sv:3:1\n\n  assign q = p;\n")
    fixed = diag.fix_source_comments(v, ["/src/a.sv", "<Generated q>"])
    assert "// /src/a.sv:10:5" in fixed and "<Generated q>:0:0" in fixed
    assert "other.sv:3:1" in fixed
    assert diag.verilog_source(fixed, 2) == ("/src/a.sv", 10, 5)
    assert diag.verilog_source(fixed, 6) is None               # a blank line ends a block
    rel = diag.absolute_source_comments("// a.sv:1:1\n// xa.sv:1:1\n", {"a.sv": "/p/a.sv"})
    assert rel == "// /p/a.sv:1:1\n// xa.sv:1:1\n"
