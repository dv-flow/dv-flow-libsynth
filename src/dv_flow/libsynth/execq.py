"""Running a tool whose nonzero exit is an expected outcome."""
from typing import List


async def exec_expected(ctxt, cmd: List[str], **kw) -> int:
    """`ctxt.exec`, without the generic "Command failed" error marker that
    dv-flow adds on a nonzero exit. For tools whose exit status is a result
    the caller reports itself (a solver timing out, a test failing, a reader
    the caller falls back from)."""
    markers = getattr(ctxt, "_markers", None)
    n = len(markers) if markers is not None else 0
    st = await ctxt.exec(cmd, **kw)
    if st != 0 and markers is not None:
        markers[n:] = [m for m in markers[n:]
                       if not str(getattr(m, "msg", "")).startswith("Command failed:")]
    return st
