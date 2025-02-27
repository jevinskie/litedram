#
# This file is part of LiteDRAM.
#
# Copyright (c) 2021 Antmicro <www.antmicro.com>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.build.sim import SimPlatform
from litex.build.sim.config import SimConfig
from litex.build.generic_platform import Pins, Subsignal
from litex.soc.interconnect.csr import CSRStorage, AutoCSR

from litedram.common import Settings, tXXDController
from litedram.phy.utils import Serializer, Deserializer, edge


# PHY ----------------------------------------------------------------------------------------------

class SimSerDesMixin:
    """Helper class for easier (de-)serialization to simulation pads."""
    def ser(self, *, i, o, clkdiv, clk, name="", **kwargs):
        assert len(o) == 1
        kwargs = dict(i=i, i_dw=len(i), o=o, o_dw=1, clk=clk, clkdiv=clkdiv,
            name=f"ser_{name}".strip("_"), **kwargs)
        self.submodules += Serializer(**kwargs)

    def des(self, *, i, o, clkdiv, clk, name="", **kwargs):
        assert len(i) == 1
        kwargs = dict(i=i, i_dw=1, o=o, o_dw=len(o), clk=clk, clkdiv=clkdiv,
            name=f"des_{name}".strip("_"), **kwargs)
        self.submodules += Deserializer(**kwargs)

# Platform -----------------------------------------------------------------------------------------

class SimPad(Settings):
    def __init__(self, name, width, io=False):
        self.set_attributes(locals())


class SimulationPads(Module):
    """Pads for simulation purpose

    Tristate pads are simulated as separate input/output pins (name_i, name_o) and
    an output-enable pin (name_oe). Output pins are to be driven byt the PHY and
    input pins are to be driven by the DRAM simulator. An additional pin without
    a suffix is created and this module will include logic to set this pin to the
    actual value depending on the output-enable signal.
    """
    def layout(self, **kwargs):
        raise NotImplementedError("Simulation pads layout as a list of SimPad objects")

    def __init__(self, **kwargs):
        for pad  in self.layout(**kwargs):
            if pad.io:
                o, i, oe = (f"{pad.name}_{suffix}" for suffix in ["o", "i", "oe"])
                setattr(self, pad.name, Signal(pad.width))
                setattr(self, o, Signal(pad.width, name=o))
                setattr(self, i, Signal(pad.width, name=i))
                setattr(self, oe, Signal(name=oe))
                self.comb += If(getattr(self, oe),
                    getattr(self, pad.name).eq(getattr(self, o))
                ).Else(
                    getattr(self, pad.name).eq(getattr(self, i))
                )
            else:
                setattr(self, pad.name, Signal(pad.width, name=pad.name))


class Clocks(dict):
    """Helper for definiting simulation clocks

    Dictionary format is `{name: {"freq_hz": _, "phase_deg": _}, ...}`.
    """
    def names(self):
        return list(self.keys())

    def add_io(self, io):
        for name in self.names():
            io.append((name + "_clk", 0, Pins(1)))

    def add_clockers(self, sim_config):
        for name, desc in self.items():
            sim_config.add_clocker(name + "_clk", **desc)


class CRG(Module):
    """Clock & Reset Generator for Verilator-based simulation"""
    def __init__(self, platform, clock_domains=None):
        if clock_domains is None:
            clock_domains = ["sys"]
        elif isinstance(clock_domains, Clocks):
            clock_domains = list(clock_domains.names())

        # request() before creating clock_domains to avoid signal renaming problem
        clock_domains = {name: platform.request(name + "_clk") for name in clock_domains}

        self.clock_domains.cd_por = ClockDomain(reset_less=True)
        for name in clock_domains.keys():
            setattr(self.clock_domains, "cd_" + name, ClockDomain(name=name))

        int_rst = Signal(reset=1)
        self.sync.por += int_rst.eq(0)
        self.comb += self.cd_por.clk.eq(self.cd_sys.clk)

        for name, clk in clock_domains.items():
            cd = getattr(self, "cd_" + name)
            self.comb += cd.clk.eq(clk)
            self.comb += cd.rst.eq(int_rst)


class Platform(SimPlatform):
    def __init__(self, io, clocks: Clocks):
        common_io = [
            ("sys_rst", 0, Pins(1)),

            ("serial", 0,
                Subsignal("source_valid", Pins(1)),
                Subsignal("source_ready", Pins(1)),
                Subsignal("source_data",  Pins(8)),
                Subsignal("sink_valid",   Pins(1)),
                Subsignal("sink_ready",   Pins(1)),
                Subsignal("sink_data",    Pins(8)),
            ),
        ]
        clocks.add_io(common_io)
        SimPlatform.__init__(self, "SIM", common_io + io)

# Logging ------------------------------------------------------------------------------------------

class SimLogger(Module, AutoCSR):
    """Logger for use in simulation

    This module allows for easier message logging when running simulation designs.
    The logger can be used from `comb` context so it the methods can be directly
    used inside `FSM` code. It also provides logging levels that can be used to
    filter messages, either by specifying the default `log_level` or in runtime
    by driving to the `level` signal or using a corresponding CSR.
    """
    # Allows to use Display inside FSM and to filter log messages by level (statically or dynamically)
    DEBUG = 0
    INFO  = 1
    WARN  = 2
    ERROR = 3
    NONE  = 4

    def __init__(self, log_level=INFO, clk_freq=None):
        self.ops = []
        self.level = Signal(reset=log_level, max=self.NONE)
        self.time_ps = None
        if clk_freq is not None:
            self.time_ps = Signal(64)
            cnt = Signal(64)
            self.sync += cnt.eq(cnt + 1)
            self.comb += self.time_ps.eq(cnt * int(1e12/clk_freq))

    def debug(self, fmt, *args, **kwargs):
        return self.log("[DEBUG] " + fmt, *args, level=self.DEBUG, **kwargs)

    def info(self, fmt, *args, **kwargs):
        return self.log("[INFO] " + fmt, *args, level=self.INFO, **kwargs)

    def warn(self, fmt, *args, **kwargs):
        return self.log("[WARN] " + fmt, *args, level=self.WARN, **kwargs)

    def error(self, fmt, *args, **kwargs):
        return self.log("[ERROR] " + fmt, *args, level=self.ERROR, **kwargs)

    def log(self, fmt, *args, level=DEBUG, once=True):
        cond = Signal()
        if once:  # make the condition be triggered only on rising edge
            condition = edge(self, cond)
        else:
            condition = cond

        self.ops.append((level, condition, fmt, args))
        return cond.eq(1)

    def add_csrs(self):
        self._level = CSRStorage(len(self.level), reset=self.level.reset.value)
        self.comb += self.level.eq(self._level.storage)

    def do_finalize(self):
        for level, cond, fmt, args in self.ops:
            if self.time_ps is not None:
                fmt = f"[%16d ps] {fmt}"
                args = (self.time_ps, *args)
            self.sync += If((level >= self.level) & cond, Display(fmt, *args))

def log_level_getter(log_level):
    """Parse logging level description

    Log level can be presented in a simple form (e.g. `--log-level=DEBUG`) to specify
    the same level for all modules, or can set different levels for different modules
    e.g. `--log-level=all=INFO,data=DEBUG`.
    """
    def get_level(name):
        return getattr(SimLogger, name.upper())

    if "=" not in log_level:  # simple log_level, e.g. "INFO"
        return lambda _: get_level(log_level)

    # parse log_level in the per-module form, e.g. "--log-level=all=INFO,data=DEBUG"
    per_module = dict(part.split("=") for part in log_level.strip().split(","))
    return lambda module: get_level(per_module.get(module, per_module.get("all", None)))

# Simulator ----------------------------------------------------------------------------------------

class PulseTiming(Module):
    """Timing monitor with pulse input/output

    This module works like `tXXDController` with the following differences:

    * countdown triggered by a low to high pulse on `trigger`
    * `ready` is initially low, only after a trigger it can become high
    * provides `ready_p` which is high only for 1 cycle when `ready` becomes high
    """
    def __init__(self, t):
        self.trigger = Signal()
        self.ready   = Signal()
        self.ready_p = Signal()

        ready_d = Signal()
        triggered = Signal()
        tctrl = tXXDController(t)
        self.submodules += tctrl

        self.sync += If(self.trigger, triggered.eq(1)),
        self.comb += [
            self.ready.eq(triggered & tctrl.ready),
            self.ready_p.eq(edge(self, self.ready)),
            tctrl.valid.eq(edge(self, self.trigger)),
        ]
