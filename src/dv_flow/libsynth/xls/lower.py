"""Lowering of an XLS schedule to pipeline registers + stage logic.

Input: a function's optimized IR (``<fn>.opt.ir``) and its
``PackageScheduleProto`` (``<fn>.schedule.textproto``), as written by
``synth.xls.Codegen``.

The *structurer* (``structure()``) follows the core rules of XLS codegen
(1.5, the function-to-pipeline path):

* every node runs in the stage the schedule gives it; parameters are in
  stage 0, behind the input flops (``flop_inputs``);
* a node defined in stage ``s`` and used in stage ``t > s`` (or the return
  value, defined before the last stage) gets one pipeline register at each
  boundary ``s|s+1 .. t-1|t`` (pass-through registers included);
* literals are not registered: they are rematerialized in every stage that
  uses them (XLS's register-constant removal gives the same result);
* the return value is registered by the output flops (``flop_outputs``);
* a valid bit runs beside the data. Every register (data and valid) is reset
  by ``rst`` to 0; a data register loads only when the valid entering it is
  set, else holds (XLS's load-enable on valid).

The latency is then ``stages + 1`` (input flops + stage boundaries + output
flops) and the module's ports are those of XLS's ``codegen_main`` with the
same options: ``clk, rst, input_valid, <params>, output_valid, out`` with
arrays flattened element 0 at the LSB.

Emitters:

* ``emit_v5()`` -- synthesizable SV: one combinational module per stage
  (``<module>__stage<s>``) and the top ``<module>`` holding the registers;
* ``emit_v4()`` -- the stage-annotated cycle model: a package
  ``<module>_v4_pkg`` with one function per stage and a class ``<module>_v4``
  in the fw-hdl class-RTL style (two-phase: named stage registers, their
  next values ``*_n``, one method per stage, ``tick()`` the per-edge
  kernel: open / stage methods / commit) and ``task op()`` that times an op
  against a clock as the RTL transactor does.

Run as ``python -m dv_flow.libsynth.xls.lower <opt.ir> <schedule> <outdir>``.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------- types


@dataclass
class Type:
    width: int                     # flattened width
    dims: List[int] = field(default_factory=list)   # outermost first
    elem: int = 0                  # width of the innermost bits element

    @property
    def is_array(self) -> bool:
        return bool(self.dims)

    def sub(self) -> "Type":
        """Element type of the outermost dimension."""
        d = self.dims[1:]
        w = self.elem
        for n in d:
            w *= n
        return Type(w, d, self.elem)


_BITS = re.compile(r"^bits\[(\d+)\]((?:\[\d+\])*)$")


def parse_type(s: str) -> Type:
    s = s.strip()
    m = _BITS.match(s)
    if not m:
        raise ValueError(f"unsupported type {s!r} (bits and arrays of bits only)")
    elem = int(m.group(1))
    # bits[8][4][32]: the last dimension is the outermost
    dims = [int(x) for x in re.findall(r"\[(\d+)\]", m.group(2))][::-1]
    w = elem
    for n in dims:
        w *= n
    return Type(w, dims, elem)


# ------------------------------------------------------------------------ IR


@dataclass
class Node:
    name: str
    type: Type
    op: str
    operands: List[str]
    kw: Dict[str, str]
    id: int = 0
    stage: int = 0


@dataclass
class Function:
    name: str
    params: List[Tuple[str, Type]]
    ret: str
    ret_type: Type
    nodes: Dict[str, Node]          # in IR (topological) order


def _split_top(s: str) -> List[str]:
    """Split at commas outside brackets/parentheses."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


_FN = re.compile(r"^(?:top\s+)?fn\s+([\w.]+)\((.*)\)\s*->\s*(.+?)\s*\{\s*$")
_NODE = re.compile(r"^\s*(ret\s+)?([\w.]+):\s*(.+?)\s*=\s*([a-z_]+)\((.*)\)\s*$")


def parse_ir(text: str, fn_name: Optional[str] = None) -> Function:
    """The top function (or ``fn_name``) of an XLS IR package."""
    lines = text.splitlines()
    fns: Dict[str, Function] = {}
    top = None
    cur: Optional[Function] = None
    for ln in lines:
        m = _FN.match(ln.strip())
        if m:
            params = []
            for p in _split_top(m.group(2)):
                pm = re.match(r"([\w.]+):\s*(.+?)\s+id=\d+$", p.strip())
                if not pm:
                    raise ValueError(f"cannot parse parameter {p!r}")
                params.append((pm.group(1), parse_type(pm.group(2))))
            cur = Function(m.group(1), params, "", parse_type(m.group(3)), {})
            fns[cur.name] = cur
            if ln.strip().startswith("top"):
                top = cur.name
            continue
        if cur is None:
            continue
        if ln.strip() == "}":
            cur = None
            continue
        m = _NODE.match(ln)
        if not m:
            continue
        isret, name, ty, op, args = m.groups()
        ops, kw = [], {}
        for a in _split_top(args):
            km = re.match(r"^([a-z_]+)=(.*)$", a, re.S)
            if km:
                kw[km.group(1)] = km.group(2)
            else:
                ops.append(a)
        n = Node(name, parse_type(ty), op, ops, kw, int(kw.get("id", "0")))
        cur.nodes[name] = n
        if isret:
            cur.ret = name
    f = fns[fn_name or top or next(iter(fns))]
    if not f.ret:
        raise ValueError(f"{f.name}: no ret node")
    return f


def _list_operand(s: str) -> List[str]:
    s = s.strip()
    assert s.startswith("[") and s.endswith("]"), s
    return [x for x in _split_top(s[1:-1])]


def literal_value(n: Node) -> int:
    """A literal's value as one flattened integer (array element 0 at the LSB)."""
    def flat(v: str, t: Type) -> int:
        v = v.strip()
        if t.is_array:
            el = _split_top(v[1:-1])
            st = t.sub()
            r = 0
            for i, e in enumerate(el):
                r |= flat(e, st) << (i * st.width)
            return r
        v = re.sub(r"^bits\[\d+\]:", "", v).replace("_", "")
        return int(v, 0) & ((1 << t.width) - 1)
    return flat(n.kw["value"], n.type)


# ------------------------------------------------------------------ schedule

_SCHED = re.compile(r'schedules\s*\{\s*key:\s*"([^"]+)"')


def parse_schedule(text: str, fn: str) -> Dict[str, int]:
    """node -> stage for function ``fn`` of a PackageScheduleProto."""
    starts = [(m.start(), m.group(1)) for m in _SCHED.finditer(text)]
    for i, (pos, name) in enumerate(starts):
        if name != fn:
            continue
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        body = text[pos:end]
        out: Dict[str, int] = {}
        for sm in re.finditer(r"stages\s*\{\s*stage:\s*(\d+)(.*?)(?=stages\s*\{|\Z)", body, re.S):
            st = int(sm.group(1))
            for nm in re.findall(r'node:\s*"([^"]+)"', sm.group(2)):
                out[nm] = st
        return out
    raise ValueError(f"no schedule for {fn}")


# ---------------------------------------------------------------- structurer


@dataclass
class Reg:
    node: str      # IR node it carries
    width: int
    boundary: int  # registered at the entry of stage `boundary` (0: input flops)


@dataclass
class Structure:
    fn: Function
    module: str
    n_stages: int
    sv: Dict[str, str]                       # node -> SV identifier stem
    stage_nodes: List[List[str]]             # non-literal nodes per stage, topo order
    regs: List[List[Reg]]                    # regs[b]: registers entering stage b; b == n_stages: output flops
    literals: List[str]

    def reg_name(self, b: int, node: str) -> str:
        if b == 0:
            return f"in_{self.sv[node]}"
        if b == self.n_stages:
            return "out_flop"
        return f"p{b}_{self.sv[node]}"

    def stage_inputs(self, s: int) -> List[Reg]:
        return self.regs[s]

    def stage_outputs(self, s: int) -> List[Reg]:
        return self.regs[s + 1]

    def summary(self) -> dict:
        return {
            "module": self.module, "function": self.fn.name,
            "stages": self.n_stages, "latency": self.n_stages + 1,
            "nodes_per_stage": [len(x) for x in self.stage_nodes],
            "literals": len(self.literals),
            "inferred_stage_nodes": len(getattr(self, "inferred", [])),
            "registers": [{"boundary": b, "count": len(rs), "bits": sum(r.width for r in rs),
                           "names": [self.reg_name(b, r.node) for r in rs]}
                          for b, rs in enumerate(self.regs)],
        }


def _ident(name: str, used: set) -> str:
    s = re.sub(r"\W", "_", name)
    base, k = s, 1
    while s in used:
        s = f"{base}_{k}"
        k += 1
    used.add(s)
    return s


def node_operands(n: Node) -> List[str]:
    """Names of the nodes ``n`` reads."""
    out = list(n.operands)
    for k in ("indices", "cases"):
        if k in n.kw:
            out += _list_operand(n.kw[k])
    if "default" in n.kw:
        out.append(n.kw["default"])
    return out


def structure(fn: Function, sched: Dict[str, int], module: Optional[str] = None,
              infer: bool = False) -> Structure:
    inferred: List[str] = []
    module = module or re.sub(r"\W", "_", fn.name)
    used: set = set()
    sv = {p: _ident(p, used) for p, _ in fn.params}
    for nm in fn.nodes:
        sv[nm] = _ident(nm, used)
    pnames = {p for p, _ in fn.params}
    n_stages = max(sched.values()) + 1 if sched else 1
    stage_of: Dict[str, int] = {p: 0 for p in pnames}
    literals = []
    for nm, n in fn.nodes.items():
        if n.op == "literal":
            literals.append(nm)
            stage_of[nm] = -1
            continue
        if nm in sched:
            stage_of[nm] = sched[nm]
        elif infer:
            # not in the schedule (an .opt.ir rather than the scheduled IR):
            # as soon as its operands are available
            stage_of[nm] = max([stage_of[o] for o in node_operands(n) if stage_of.get(o, -1) >= 0] or [0])
            inferred.append(nm)
        else:
            raise ValueError(f"node {nm} ({n.op}) is not in the schedule; "
                             "use the scheduled IR (codegen --output_schedule_ir_path)")
    # last use stage of each node
    last: Dict[str, int] = {}
    for nm, n in fn.nodes.items():
        if n.op == "literal":
            continue
        for o in node_operands(n):
            if stage_of.get(o, -1) < 0:
                continue
            if stage_of[o] > stage_of[nm]:
                raise ValueError(f"{nm} (stage {stage_of[nm]}) reads {o} from a later stage {stage_of[o]}")
            last[o] = max(last.get(o, 0), stage_of[nm])
    regs: List[List[Reg]] = [[] for _ in range(n_stages + 1)]
    for p, t in fn.params:
        regs[0].append(Reg(p, t.width, 0))
    order = list(fn.params and [p for p, _ in fn.params]) + [n for n in fn.nodes if stage_of[n] >= 0]
    for nm in order:
        s = stage_of[nm]
        w = fn.nodes[nm].type.width if nm in fn.nodes else dict(fn.params)[nm].width
        for b in range(s + 1, last.get(nm, s) + 1):
            regs[b].append(Reg(nm, w, b))
    rnode = fn.nodes[fn.ret]
    if rnode.op == "literal":
        raise ValueError("a literal return value is not supported")
    # pass the return value down to the last stage, then the output flops
    for b in range(stage_of[fn.ret] + 1, n_stages):
        if not any(r.node == fn.ret for r in regs[b]):
            regs[b].append(Reg(fn.ret, rnode.type.width, b))
    regs[n_stages] = [Reg(fn.ret, rnode.type.width, n_stages)]
    stage_nodes = [[] for _ in range(n_stages)]
    for nm, n in fn.nodes.items():
        if n.op != "literal":
            stage_nodes[stage_of[nm]].append(nm)
    st = Structure(fn, module, n_stages, sv, stage_nodes, regs, literals)
    st.inferred = inferred
    return st


# ---------------------------------------------------------------- op lowering


def _hex(width: int, v: int) -> str:
    return f"{width}'h{v:x}"


class Lowerer:
    """SV expressions for the nodes of one stage. ``name(node)`` gives the
    identifier a node is read through in that stage."""

    def __init__(self, st: Structure, s: int, local: str = "s"):
        self.st, self.s, self.local = st, s, local
        self.fn = st.fn
        self.mine = set(st.stage_nodes[s])
        self.lits = set(st.literals)

    def name(self, node: str) -> str:
        if node in self.lits:
            return f"L_{self.st.sv[node]}"
        if node in self.mine:
            return f"{self.local}_{self.st.sv[node]}"
        return f"r_{self.st.sv[node]}"     # stage input register

    def type(self, node: str) -> Type:
        if node in self.fn.nodes:
            return self.fn.nodes[node].type
        return dict(self.fn.params)[node]

    def _index(self, arr: str, t: Type, idx: List[str]) -> str:
        """arr[idx...] with XLS's out-of-bounds clamp (last element)."""
        base = self.name(arr)
        const_off, dyn = 0, []
        for i in idx:
            n = t.dims[0]
            st = t.sub()
            if i in self.lits:
                v = min(literal_value(self.fn.nodes[i]), n - 1)
                const_off += v * st.width
            else:
                iw = self.type(i).width
                e = self.name(i)
                if (1 << iw) > n:
                    e = f"(({e} >= {iw}'d{n}) ? {iw}'d{n - 1} : {e})" if iw < 32 else \
                        f"(({e} >= {n}) ? {n - 1} : {e})"
                dyn.append(f"{e} * {st.width}" if st.width != 1 else e)
            t = st
        if not dyn:
            return f"{base}[{const_off + t.width - 1}:{const_off}]"
        off = " + ".join(([str(const_off)] if const_off else []) + [f"({d})" for d in dyn])
        return f"{base}[{off} +: {t.width}]"

    def expr(self, nm: str) -> str:
        n = self.fn.nodes[nm]
        o = [self.name(x) for x in n.operands]
        w = n.type.width
        op = n.op
        if op in ("and", "or", "xor"):
            return f" {'&' if op == 'and' else '|' if op == 'or' else '^'} ".join(o)
        if op in ("nand", "nor"):
            return f"~({(' & ' if op == 'nand' else ' | ').join(o)})"
        if op == "not":
            return f"~{o[0]}"
        if op == "neg":
            return f"-{o[0]}"
        if op == "identity":
            return o[0]
        if op in ("add", "sub", "umul"):
            sym = {"add": "+", "sub": "-", "umul": "*"}[op]
            return f"{w}'({o[0]} {sym} {o[1]})"
        if op in ("eq", "ne", "ult", "ule", "ugt", "uge"):
            sym = {"eq": "==", "ne": "!=", "ult": "<", "ule": "<=", "ugt": ">", "uge": ">="}[op]
            return f"({o[0]} {sym} {o[1]})"
        if op in ("shll", "shrl"):
            return f"{w}'({o[0]} {'<<' if op == 'shll' else '>>'} {o[1]})"
        if op == "bit_slice":
            start, width = int(n.kw["start"]), int(n.kw["width"])
            if start == 0 and width == self.type(n.operands[0]).width:
                return o[0]
            return f"{o[0]}[{start + width - 1}:{start}]"
        if op == "concat":
            return "{" + ", ".join(o) + "}"
        if op in ("sign_ext", "zero_ext"):
            iw = self.type(n.operands[0]).width
            if iw == w:
                return o[0]
            fill = f"{o[0]}[{iw - 1}]" if op == "sign_ext" else "1'b0"
            return f"{{{{{w - iw}{{{fill}}}}}, {o[0]}}}"
        if op == "array":
            return "{" + ", ".join(reversed(o)) + "}"
        if op == "array_index":
            return self._index(n.operands[0], self.type(n.operands[0]), _list_operand(n.kw["indices"]))
        if op == "sel":
            s = self.name(n.operands[0])
            sw = self.type(n.operands[0]).width
            cases = [self.name(c) for c in _list_operand(n.kw["cases"])]
            dflt = self.name(n.kw["default"]) if "default" in n.kw else None
            if sw == 1 and len(cases) == 2:
                return f"({s} ? {cases[1]} : {cases[0]})"
            e = dflt if dflt is not None else cases[-1]
            last = len(cases) - (0 if dflt is not None else 1)
            for i in reversed(range(last)):
                e = f"(({s} == {sw}'d{i}) ? {cases[i]} : {e})"
            return e
        raise ValueError(f"{nm}: op {op} is not supported by the lowering")


def _decl(w: int, name: str, kw: str = "logic") -> str:
    return f"{kw} [{w - 1}:0] {name}"


def _used_literals(st: Structure, s: int) -> List[str]:
    lits = set(st.literals)
    used = []
    for nm in st.stage_nodes[s]:
        for x in node_operands(st.fn.nodes[nm]):
            if x in lits and x not in used:
                used.append(x)
    # literals used only as constant indices still need no declaration, but
    # declaring them is harmless; keep them for readability.
    return used


def _literal_decl(st: Structure, nm: str, kw: str) -> str:
    n = st.fn.nodes[nm]
    return f"    {kw} logic [{n.type.width - 1}:0] L_{st.sv[nm]} = {_hex(n.type.width, literal_value(n))};"


# ------------------------------------------------------------------------ V5


def _stage_module(st: Structure, s: int) -> str:
    lw = Lowerer(st, s, "s")
    ins, outs = st.stage_inputs(s), st.stage_outputs(s)
    ports = [f"    input  wire {'[%d:0] ' % (r.width - 1)}r_{st.sv[r.node]}" for r in ins]
    ports += [f"    output wire {'[%d:0] ' % (r.width - 1)}n_{st.sv[r.node]}" for r in outs]
    L = [f"// Stage {s} of {st.module}: {len(st.stage_nodes[s])} nodes, "
         f"{len(ins)} input registers, {len(outs)} output registers",
         f"module {st.module}__stage{s}(", ",\n".join(ports), ");"]
    for nm in _used_literals(st, s):
        L.append(_literal_decl(st, nm, "localparam"))
    for nm in st.stage_nodes[s]:
        n = st.fn.nodes[nm]
        L.append(f"    {_decl(n.type.width, 's_' + st.sv[nm], 'wire')};")
    for nm in st.stage_nodes[s]:
        L.append(f"    assign s_{st.sv[nm]} = {lw.expr(nm)};")
    for r in outs:
        L.append(f"    assign n_{st.sv[r.node]} = {lw.name(r.node)};")
    L.append("endmodule\n")
    return "\n".join(L)


def emit_v5(st: Structure) -> str:
    fn = st.fn
    N = st.n_stages
    L = [f"// V5: {st.module}, lowered from the XLS schedule of {fn.name} by",
         "// dv_flow.libsynth.xls.lower (synth.xls.Lower). Ports and latency as",
         f"// XLS codegen_main (pipeline, flop_inputs/outputs, valid): latency {N + 1}.",
         ""]
    for s in range(N):
        L.append(_stage_module(st, s))
    ports = ["    input  wire clk", "    input  wire rst", "    input  wire input_valid"]
    ports += [f"    input  wire [{t.width - 1}:0] {p}" for p, t in fn.params]
    ports += ["    output wire output_valid", f"    output wire [{fn.ret_type.width - 1}:0] out"]
    L += [f"module {st.module}(", ",\n".join(ports), ");"]
    # registers
    L.append("    // stage registers: in_* (input flops, entry of stage 0),"
             " p<b>_* (entry of stage b), out_flop (output flops)")
    for b in range(N + 1):
        L.append(f"    logic v{b};  // valid entering {'stage %d' % b if b < N else 'the output'}")
        for r in st.regs[b]:
            L.append(f"    {_decl(r.width, st.reg_name(b, r.node))};")
        if b < N:
            for r in st.regs[b + 1]:
                L.append(f"    {_decl(r.width, f'n{b + 1}_{st.sv[r.node]}', 'wire')};")
    for s in range(N):
        conns = [f".r_{st.sv[r.node]}({st.reg_name(s, r.node)})" for r in st.regs[s]]
        conns += [f".n_{st.sv[r.node]}(n{s + 1}_{st.sv[r.node]})" for r in st.regs[s + 1]]
        L.append(f"    {st.module}__stage{s} u_stage{s}(\n        " + ",\n        ".join(conns) + ");")
    L.append("    always_ff @(posedge clk) begin")
    L.append("        if (rst) begin")
    for b in range(N + 1):
        L.append(f"            v{b} <= 1'b0;")
        for r in st.regs[b]:
            L.append(f"            {st.reg_name(b, r.node)} <= '0;")
    L.append("        end else begin")
    L.append("            v0 <= input_valid;")
    for r in st.regs[0]:
        nm = st.reg_name(0, r.node)
        L.append(f"            if (input_valid) {nm} <= {r.node};")
    for b in range(1, N + 1):
        L.append(f"            v{b} <= v{b - 1};")
        for r in st.regs[b]:
            L.append(f"            if (v{b - 1}) {st.reg_name(b, r.node)} <= n{b}_{st.sv[r.node]};")
    L.append("        end")
    L.append("    end")
    L.append(f"    assign output_valid = v{N};")
    L.append("    assign out = out_flop;")
    L.append("endmodule\n")
    return "\n".join(L)


# ------------------------------------------------------------------------ V4


def _packed(regs: List[Reg], f) -> str:
    """{last, ..., first}: the first register at the LSB."""
    return "{" + ", ".join(f(r) for r in reversed(regs)) + "}"


def _width(regs: List[Reg]) -> int:
    return sum(r.width for r in regs)


def _stage_function(st: Structure, s: int) -> str:
    lw = Lowerer(st, s, "t")
    ins, outs = st.stage_inputs(s), st.stage_outputs(s)
    iw, ow = _width(ins), _width(outs)
    L = [f"    // stage {s}: {_width(ins)} register bits in, {_width(outs)} out",
         f"    function automatic logic [{ow - 1}:0] stage{s}_f(logic [{iw - 1}:0] i);"]
    for r in ins:
        L.append(f"        {_decl(r.width, 'r_' + st.sv[r.node])};")
    for nm in st.stage_nodes[s]:
        L.append(f"        {_decl(st.fn.nodes[nm].type.width, 't_' + st.sv[nm])};")
    off = 0
    for r in ins:
        L.append(f"        r_{st.sv[r.node]} = i[{off + r.width - 1}:{off}];")
        off += r.width
    for nm in st.stage_nodes[s]:
        L.append(f"        t_{st.sv[nm]} = {lw.expr(nm)};")
    L.append(f"        stage{s}_f = {_packed(outs, lambda r: lw.name(r.node))};")
    L.append("    endfunction")
    return "\n".join(L)


def emit_v4_stages(st: Structure) -> str:
    """Package ``<module>_v4_stages_pkg``: one function per stage (plain SV,
    also read by Yosys for the V4 <-> V5 link check)."""
    N = st.n_stages
    L = [f"// V4 stage logic of {st.fn.name}: stage<s>_f(entry registers of stage s,",
         "// packed first-at-LSB) -> entry registers of stage s+1. From",
         "// dv_flow.libsynth.xls.lower (synth.xls.Lower).",
         f"package {st.module}_v4_stages_pkg;"]
    for nm in st.literals:
        if any(nm in node_operands(st.fn.nodes[x]) for s in range(N) for x in st.stage_nodes[s]):
            L.append(_literal_decl(st, nm, "localparam"))
    for s in range(N):
        L.append(_stage_function(st, s))
    L += ["endpackage", ""]
    return "\n".join(L)


def emit_v4(st: Structure, clk_if: str = "") -> str:
    fn = st.fn
    N = st.n_stages
    m = st.module
    clk_if = clk_if or f"{m}_v4_clk_if"
    L = [f"// V4: stage-annotated cycle model of {fn.name} (schedule: {N} stages,",
         f"// latency {N + 1}), from dv_flow.libsynth.xls.lower (synth.xls.Lower).",
         f"// Same registers as V5; stage<s>() runs stage s ({m}_v4_stages_pkg::stage<s>_f).",
         f"interface {clk_if}(input logic clk);", "endinterface", "",
         f"package {m}_v4_pkg;",
         f"    import {m}_v4_stages_pkg::*;"]
    L += ["", f"    class {m}_v4;",
          f"        static const int unsigned STAGES = {N};",
          f"        static const int unsigned LATENCY = {N + 1};",
          f"        virtual {clk_if} vif;",
          "        int unsigned m_clks;",
          "        // named stage registers (current value; *_n: next)"]
    for b in range(N + 1):
        L.append(f"        bit v{b}, v{b}_n;")
        for r in st.regs[b]:
            nm = st.reg_name(b, r.node)
            L.append(f"        {_decl(r.width, nm, 'bit')}, {nm}_n;")
    L += ["", f"        function new(virtual {clk_if} vif = null);",
          "            this.vif = vif;", "        endfunction", "",
          "        function void reset();"]
    for b in range(N + 1):
        L.append(f"            v{b} = 0;")
        for r in st.regs[b]:
            L.append(f"            {st.reg_name(b, r.node)} = '0;")
    L.append("        endfunction")
    for s in range(N):
        ins, outs = st.regs[s], st.regs[s + 1]
        L += ["", f"        // stage {s}: {len(st.stage_nodes[s])} nodes",
              f"        function void stage{s}();",
              f"            v{s + 1}_n = v{s};",
              f"            if (v{s}) {_packed(outs, lambda r: st.reg_name(s + 1, r.node) + '_n')} = "
              f"stage{s}_f({_packed(ins, lambda r: st.reg_name(s, r.node))});",
              "        endfunction"]
    args = ", ".join(f"bit [{t.width - 1}:0] {p}" for p, t in fn.params)
    L += ["", "        // one clock edge: open (n = v), the stage methods, commit (v = n)",
          f"        function void tick(bit input_valid, {args});"]
    for b in range(N + 1):
        L.append(f"            v{b}_n = v{b};")
        for r in st.regs[b]:
            nm = st.reg_name(b, r.node)
            L.append(f"            {nm}_n = {nm};")
    L.append("            v0_n = input_valid;")
    for p, _ in fn.params:
        L.append(f"            if (input_valid) in_{st.sv[p]}_n = {p};")
    for s in range(N):
        L.append(f"            stage{s}();")
    for b in range(N + 1):
        L.append(f"            v{b} = v{b}_n;")
        for r in st.regs[b]:
            nm = st.reg_name(b, r.node)
            L.append(f"            {nm} = {nm}_n;")
    L.append("        endfunction")
    L += ["", "        // an op timed as the RTL transactor sees it: inputs valid for one",
          "        // edge, then edges until output_valid is seen (sampled before the edge)",
          f"        task op({args}, output bit [{fn.ret_type.width - 1}:0] result);",
          "            bit seen;",
          "            @(posedge vif.clk);", "            m_clks++;",
          "            tick(1, " + ", ".join(p for p, _ in fn.params) + ");",
          "            do begin",
          "                @(posedge vif.clk);", "                m_clks++;",
          f"                seen = v{N};",
          "                tick(0, " + ", ".join(p for p, _ in fn.params) + ");",
          "            end while (!seen);",
          "            result = out_flop;",
          "        endtask",
          "    endclass", "endpackage", ""]
    return "\n".join(L)


# ------------------------------------------------------------------- driver


def lower_files(opt_ir: str, schedule: str, outdir: str, module: Optional[str] = None,
                fn_name: Optional[str] = None, infer: bool = False) -> Dict[str, str]:
    with open(opt_ir) as f:
        fn = parse_ir(f.read(), fn_name)
    with open(schedule) as f:
        sched = parse_schedule(f.read(), fn.name)
    st = structure(fn, sched, module, infer)
    os.makedirs(outdir, exist_ok=True)
    out = {"v5": os.path.join(outdir, f"{st.module}.v5.sv"),
           "v4_stages": os.path.join(outdir, f"{st.module}.v4_stages.sv"),
           "v4": os.path.join(outdir, f"{st.module}.v4.sv"),
           "structure": os.path.join(outdir, f"{st.module}.structure.json")}
    with open(out["v5"], "w") as f:
        f.write(emit_v5(st))
    with open(out["v4_stages"], "w") as f:
        f.write(emit_v4_stages(st))
    with open(out["v4"], "w") as f:
        f.write(emit_v4(st))
    with open(out["structure"], "w") as f:
        json.dump(st.summary(), f, indent=1)
    return out


if __name__ == "__main__":
    print(json.dumps(lower_files(*sys.argv[1:4]), indent=1))
