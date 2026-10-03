#****************************************************************************
#* __ext__.py
#*
#* Registers the libsynth packages with dv-flow.
#****************************************************************************
import os


def dvfm_packages():
    d = os.path.dirname(os.path.abspath(__file__))
    return {
        'synth': os.path.join(d, "flow.dv"),
        'synth.xls': os.path.join(d, "xls", "flow.dv"),
        'synth.yosys': os.path.join(d, "yosys", "flow.dv"),
    }
