"""Map XLS diagnostics back to the source an IR package was made from.

XLS has no warnings channel. A tool failure is one absl Status line::

    Error: INVALID_ARGUMENT: <message>; Running pass #3 (...) ...

plus glog lines (``E1003 12:00:00.0 1 file.cc:42] <message>``). What the
message points at depends on the kind of failure:

* **IR parse / verify** -- a line:col in the IR text (``@ 7:21``) or a node
  name (``operand 1 of s``);
* **an optimizer check** -- bare node names (``Channel o ... s2 ... s1``);
* **scheduling** -- nothing (``Try --clock_period_ps=569``): see
  :func:`critical_path`.

Each node may carry ``pos=[(file, line, col)]`` (0-based) into the
package's ``file_number`` table. Positions are best-effort through
``opt_main``: nodes it creates have none, and codegen errors name nodes of
the optimized IR. So a node without a ``pos`` takes the nearest one along
its operands, then its users.
"""
from __future__ import annotations

import dataclasses as dc
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

#: (absolute source path, 1-based line, 1-based column)
SrcLoc = Tuple[str, int, int]

_FILE = re.compile(r'^\s*file_number\s+(\d+)\s+"([^"]*)"', re.M)
_NODE = re.compile(r"^\s*(?:ret\s+)?([A-Za-z_][\w.]*)\s*:\s*[^=]+?=\s*([a-z_]+)\((.*)\)\s*$")
_STATE = re.compile(r"([A-Za-z_][\w.]*)\s*:\s*[^,()]+?\s+pos=\[\((\d+),\s*(\d+),\s*(\d+)\)")
_POS = re.compile(r"pos=\[((?:\(\d+,\s*\d+,\s*\d+\),?\s*)*)\]")
_TUPLE = re.compile(r"\((\d+),\s*(\d+),\s*(\d+)\)")
_IDENT = re.compile(r"[A-Za-z_][\w.]*")


@dc.dataclass
class IrNode:
    name: str
    op: str
    line: int                                   # 1-based line in the IR text
    pos: List[SrcLoc] = dc.field(default_factory=list)
    operands: List[str] = dc.field(default_factory=list)
    users: List[str] = dc.field(default_factory=list)


class IrIndex:
    """The nodes of an IR package's text: where each is, and what it reads."""

    def __init__(self, text: str, files_root: Optional[str] = None):
        #: file number -> the path as the IR writes it
        self.raw_files = {int(n): p for n, p in _FILE.findall(text)}
        self.files = {n: resolve_path(p, files_root) for n, p in self.raw_files.items()}
        self.nodes: Dict[str, IrNode] = {}
        self.by_line: Dict[int, str] = {}
        lines = text.splitlines()
        for i, line in enumerate(lines, start=1):
            m = _NODE.match(line)
            if m:
                name, op, args = m.groups()
                args = re.sub(r'"[^"]*"|pos=\[[^\]]*\]|(?:channel|state_element|id)=[\w.]+',
                              "", args)
                operands = [t for t in _IDENT.findall(args)
                            if not re.search(re.escape(t) + r"\s*=", args)]
                self._add(IrNode(name, op, i, self._pos(line), operands))
                continue
            # state elements in a proc header: ``st: bits[8] pos=[...]``
            if re.match(r"^\s*(?:top\s+)?proc\s", line):
                for sname, fno, l, c in _STATE.findall(line):
                    self._add(IrNode(sname, "state_element", i,
                                     self._pos(f"pos=[({fno},{l},{c})]")))
        for n in self.nodes.values():
            n.operands = list(dict.fromkeys(o for o in n.operands if o in self.nodes and o != n.name))
            for o in n.operands:
                self.nodes[o].users.append(n.name)
        # a state_read / next_value reads its state element
        for n in self.nodes.values():
            m = re.search(r"state_element=([\w.]+)", lines[n.line - 1])
            if m and m.group(1) in self.nodes and n.op != "state_element":
                n.operands.append(m.group(1))

    def _add(self, n: IrNode) -> None:
        if n.name not in self.nodes:
            self.nodes[n.name] = n
            self.by_line.setdefault(n.line, n.name)

    def _pos(self, line: str) -> List[SrcLoc]:
        out = []
        m = _POS.search(line)
        for fno, l, c in _TUPLE.findall(m.group(1) if m else ""):
            f = self.files.get(int(fno))
            if f is not None and not is_pseudo(f):
                out.append((f, int(l) + 1, int(c) + 1))
        return out

    def locate(self, name: str, limit: int = 8) -> List[SrcLoc]:
        """Source positions of node *name*: its own, else the nearest node
        along its operands, then along its users, that has one."""
        if name not in self.nodes:
            return []
        if self.nodes[name].pos:
            return self.nodes[name].pos
        for edge in ("operands", "users"):
            seen, frontier = {name}, [name]
            for _ in range(limit):
                nxt = []
                for f in frontier:
                    for o in getattr(self.nodes[f], edge):
                        if o not in seen:
                            seen.add(o)
                            nxt.append(o)
                found = [p for o in nxt for p in self.nodes[o].pos]
                if found:
                    return list(dict.fromkeys(found))
                frontier = nxt
                if not frontier:
                    break
        return []

    def node_at(self, line: int) -> Optional[str]:
        """The node defined on IR line *line*, or the closest one above it."""
        for l in range(line, 0, -1):
            if l in self.by_line:
                return self.by_line[l]
        return None


def is_pseudo(path: str) -> bool:
    """XLS's made-up files, e.g. ``<Generated non-synthesizable function for q>``."""
    return path.startswith("<")


def resolve_path(path: str, root: Optional[str] = None) -> str:
    """An IR file path made absolute. Front ends (and ``ir_converter_main``)
    record paths as given, relative to where they ran; *root* (the IR's
    directory) is tried, then the current directory."""
    if is_pseudo(path) or os.path.isabs(path):
        return path
    cands = ([os.path.join(root, path)] if root else []) + [os.path.abspath(path)]
    for c in cands:
        if os.path.exists(c):
            return os.path.abspath(c)
    return os.path.abspath(cands[0])


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

@dc.dataclass
class XlsMessage:
    severity: str                  # "error" | "warning" | "info"
    text: str                      # the message, without the pass-nesting chain
    ir_line: Optional[int] = None  # 1-based IR text line it cites, if any
    ir_col: Optional[int] = None
    glog: bool = False             # a log line, not the tool's Status


_ERROR = re.compile(r"^(?:.*?\b)?Error:\s*(?:[A-Z_]+:\s*)?(.*)$")
_GLOG = re.compile(r"^([EWF])\d{4}\s+[\d:.]+\s+\d+\s+[\w./-]+:\d+\]\s*(.*)$")
_IRPOS = re.compile(r"@\s*(\d+):(\d+)(?:-\d+:\d+)?")
_CHAIN = re.compile(r";\s*(?:Running pass|While running|\[).*$")
#: glog lines that only repeat or trace the Status
_GLOG_NOISE = re.compile(r"^(?:Source Location Trace|\w+/[\w/.-]+:\d+$)")


def strip_chain(msg: str) -> str:
    """The message without XLS's ``; Running pass #N ...`` nesting chain."""
    return _CHAIN.sub("", msg).strip()


def parse_log(text: str) -> List[XlsMessage]:
    """The errors and warnings in an XLS tool's output, de-duplicated."""
    out: List[XlsMessage] = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        sev = msg = None
        m = _GLOG.match(line)
        if m:
            sev = {"E": "error", "F": "error", "W": "warning"}[m.group(1)]
            msg = m.group(2)
        else:
            m = _ERROR.match(line)
            if m:
                sev, msg = "error", m.group(1)
        if not msg or _GLOG_NOISE.match(msg):
            continue
        # ``...; @ file.ir`` at the end names the IR file: keep the line:col
        m = _IRPOS.search(msg)
        ir_line, ir_col = (int(m.group(1)), int(m.group(2))) if m else (None, None)
        msg = re.sub(r"\s*@\s*\d+:\d+(?:-\d+:\d+)?", "", msg)
        msg = strip_chain(re.sub(r";\s*@\s*\S+", "", msg))
        if not msg or msg in seen:
            continue
        seen.add(msg)
        out.append(XlsMessage(sev, msg, ir_line, ir_col, glog=bool(_GLOG.match(line))))
    # With a Status error, glog errors are its progress notes ("Unable to
    # schedule ...; finding the shortest feasible clock period").
    if any(m.severity == "error" and not m.glog for m in out):
        for m in out:
            if m.glog and m.severity == "error":
                m.severity = "info"
    return out


#: English words XLS messages use that are also plausible signal names. A node
#: named like one is matched only when the message sets it off (quotes,
#: ``name:``, ``name(`` or ``of name``).
_WORDS = {"a", "an", "and", "as", "at", "be", "by", "for", "from", "if", "in", "is", "it",
          "no", "not", "of", "on", "or", "the", "to", "type", "value", "with", "node",
          "operand", "channel", "state", "token", "bits", "also", "sometimes", "proved",
          "active", "unconditionally", "expected", "declared", "does", "match", "but",
          "got", "has", "have", "must", "cannot", "only", "one", "which", "that", "this"}


def named_nodes(msg: str, index: IrIndex) -> List[str]:
    """The IR nodes a message names, in order of first mention."""
    out = []
    for m in _IDENT.finditer(msg):
        tok = m.group(0).rstrip(".")
        if tok not in index.nodes or tok in out:
            continue
        if tok.lower() in _WORDS:
            before, after = msg[:m.start()], msg[m.end():]
            if not (re.search(r"[\"'`]$", before) or re.match(r"[\"'`:(]", after)
                    or re.search(r"\bof\s+$", before)):
                continue
        out.append(tok)
    return out


@dc.dataclass
class Diagnostic:
    severity: str
    msg: str
    loc: Optional[SrcLoc] = None


def map_log(log_text: str, index: Optional[IrIndex]) -> List[Diagnostic]:
    """One diagnostic per source position an XLS message points at (or one
    without a position, when it points at none)."""
    out: List[Diagnostic] = []
    for m in parse_log(log_text):
        locs: List[Tuple[SrcLoc, str]] = []
        if index is not None:
            if m.ir_line is not None:
                n = index.node_at(m.ir_line)
                if n is not None:
                    locs += [(p, n) for p in index.locate(n)]
            for n in named_nodes(m.text, index):
                locs += [(p, n) for p in index.locate(n)]
        done = set()
        for p, n in locs:
            if p in done:
                continue
            done.add(p)
            out.append(Diagnostic(m.severity, f"{m.text} (XLS node {n})", p))
        if not done:
            out.append(Diagnostic(m.severity, m.text))
    return out


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

_SUGGEST = re.compile(r"Try `?--clock_period_ps=(\d+)")


def suggested_period(log_text: str) -> Optional[int]:
    m = _SUGGEST.search(log_text)
    return int(m.group(1)) if m else None


@dc.dataclass
class TimedNode:
    name: str
    stage: int
    node_delay: int
    path_delay: int


def parse_schedule(text: str) -> Dict[str, TimedNode]:
    """``PackageScheduleProto`` text format -> node -> its timing."""
    out: Dict[str, TimedNode] = {}
    stage = 0
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"stage:\s*(\d+)", s)
        if m:
            stage = int(m.group(1))
            continue
        m = re.match(r'node:\s*"([^"]+)"', s)
        if m:
            cur = out[m.group(1)] = TimedNode(m.group(1), stage, 0, 0)
            continue
        m = re.match(r"(node_delay_ps|path_delay_ps):\s*(\d+)", s)
        if m and out:
            setattr(cur, "node_delay" if m.group(1) == "node_delay_ps" else "path_delay",
                    int(m.group(2)))
    return out


def critical_path(sched: Dict[str, TimedNode], index: IrIndex) -> List[TimedNode]:
    """The longest combinational path, from its start to its end: the node
    with the largest path delay, then back through the operand whose path
    delay it extends."""
    if not sched:
        return []
    end = max(sched.values(), key=lambda t: t.path_delay)
    path = [end]
    cur = end
    while True:
        want = cur.path_delay - cur.node_delay
        if want <= 0 or cur.name not in index.nodes:
            break
        prev = [sched[o] for o in index.nodes[cur.name].operands
                if o in sched and sched[o].stage == cur.stage]
        prev = [p for p in prev if p.path_delay == want] or \
            sorted(prev, key=lambda p: -p.path_delay)[:1]
        if not prev or prev[0] in path:
            break
        cur = prev[0]
        path.append(cur)
    return list(reversed(path))


def describe_path(path: Iterable[TimedNode], index: IrIndex) -> List[Tuple[str, Optional[SrcLoc]]]:
    """``(text, loc)`` per step of a path, one per source position."""
    out: List[Tuple[str, Optional[SrcLoc]]] = []
    last = None
    for t in path:
        locs = index.locate(t.name)
        loc = locs[0] if locs else None
        op = index.nodes[t.name].op if t.name in index.nodes else "?"
        text = f"{t.name} ({op}, +{t.node_delay} ps = {t.path_delay} ps)"
        if loc is not None and loc == last and out:
            out[-1] = (out[-1][0] + f", {text}", loc)
        else:
            out.append((text, loc))
        last = loc
    return out


# ---------------------------------------------------------------------------
# Verilog source comments
# ---------------------------------------------------------------------------

def fix_source_comments(verilog: str, files: Iterable[str]) -> str:
    """Make codegen's ``// <file>:<line>:<col>`` comments 1-based.

    XLS positions are 0-based and codegen prints them unchanged, so each
    comment is one line (and column) early. Only comments naming one of the
    package's *files* are touched."""
    names = sorted({f for f in files if f and not is_pseudo(f)}, key=len, reverse=True)
    if not names:
        return verilog
    alt = "|".join(re.escape(f) for f in names)
    pat = re.compile(r"(//\s*)(" + alt + r"):(\d+):(\d+)")
    return pat.sub(lambda m: f"{m.group(1)}{m.group(2)}:{int(m.group(3)) + 1}:{int(m.group(4)) + 1}",
                   verilog)


def absolute_source_comments(verilog: str, files: Dict[str, str]) -> str:
    """Rewrite the file names in source comments: *files* maps the name as
    written in the IR to its absolute path."""
    for raw, absolute in sorted(files.items(), key=lambda kv: -len(kv[0])):
        if raw != absolute and not is_pseudo(raw):
            verilog = re.sub(r"(//\s*)" + re.escape(raw) + r"(?=:\d+:\d+)",
                             lambda m: m.group(1) + absolute, verilog)
    return verilog


_SRC_COMMENT = re.compile(r"//\s*(\S+):(\d+):(\d+)\s*$")


def verilog_source(verilog: str, line: int) -> Optional[SrcLoc]:
    """The source position of Verilog line *line* (1-based): the nearest
    source comment at or above it, within the same statement block. For
    mapping a lint or simulation message on generated Verilog back to the
    source (after :func:`fix_source_comments`)."""
    lines = verilog.splitlines()
    for i in range(min(line, len(lines)) - 1, -1, -1):
        s = lines[i].strip()
        m = _SRC_COMMENT.match(s)
        if m:
            return (m.group(1), int(m.group(2)), int(m.group(3)))
        if i < line - 1 and (not s or s.startswith("// =====")):
            break
    return None
