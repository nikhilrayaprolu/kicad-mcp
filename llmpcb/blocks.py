"""Reference-circuit blocks: parameterised, engineering-checked sub-circuits.

Each block adds components + connections + placement intent (near constraints) to
the IR and returns a short summary incl. the design calculations it made, so the
LLM reasons at the level of "a 1.8 V 2 A buck from VSYS" instead of 9 parts.
"""
from __future__ import annotations

import math

from . import parts
from .ir import Constraint, Design

E96 = [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33, 1.37, 1.40, 1.43, 1.47, 1.50,
       1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32,
       2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09, 3.16, 3.24, 3.32, 3.40, 3.48, 3.57,
       3.65, 3.74, 3.83, 3.92, 4.02, 4.12, 4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49,
       5.62, 5.76, 5.90, 6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45,
       8.66, 8.87, 9.09, 9.31, 9.53, 9.76]


def _fmt_r(r: float) -> str:
    if r >= 1e6:
        return f"{r/1e6:g}M"
    if r >= 1e3:
        return f"{r/1e3:g}k"
    return f"{r:g}"


def divider(vout: float, vref: float, r_bot: float = 100e3) -> tuple[float, float, float]:
    """Pick E96 top resistor for a fixed bottom resistor; returns (r_top, r_bot, vout_actual)."""
    ideal = r_bot * (vout / vref - 1)
    best = None
    for dec in (1e3, 1e4, 1e5, 1e6):
        for m in E96:
            r = m * dec
            v = vref * (1 + r / r_bot)
            if best is None or abs(v - vout) < abs(best[2] - vout):
                best = (r, r_bot, v)
    return best if ideal > 0 else (0, r_bot, vref)


def _near(d: Design, ref: str, target: str, mm: float, note: str = ""):
    d.constraints.append(Constraint(kind="near", ref=ref, target=target, max_mm=mm, note=note))


# ------------------------------------------------------------------ blocks
def buck_tlv62569(d: Design, block: str, vin: str, vout_net: str, vout: float, en: str | None = None,
                  gnd: str = "GND", iout: float = 1.0, soft_start_rc: bool = False) -> dict:
    """TLV62569 2A buck: Cin, L, Cout, FB divider, optional EN RC delay (sequencing)."""
    u = parts.add(d, "TLV62569DBV", block=block)
    L_uH = 1.0 if vout <= 1.2 else 2.2
    rt, rb, va = divider(vout, 0.6, 100e3)
    sw = f"{vout_net}_SW"
    fb = f"{vout_net}_FB"
    en_net = en or f"{vout_net}_EN"
    parts.connect(d, u.ref, {"VIN": vin, "GND": gnd, "SW": sw, "FB": fb, "EN": en_net})
    cin = parts.add(d, "C0603", block=block, value="10u/10V", conn={"1": vin, "2": gnd})
    l = parts.add(d, "L2520", block=block, value=f"{L_uH:g}uH", conn={"1": sw, "2": vout_net})
    co1 = parts.add(d, "C0805", block=block, value="22u/10V", conn={"1": vout_net, "2": gnd})
    co2 = parts.add(d, "C0402", block=block, value="100n", conn={"1": vout_net, "2": gnd})
    r1 = parts.add(d, "R0402", block=block, value=f"{_fmt_r(rt)} 1%", conn={"1": vout_net, "2": fb})
    r2 = parts.add(d, "R0402", block=block, value=f"{_fmt_r(rb)} 1%", conn={"1": fb, "2": gnd})
    cff = parts.add(d, "C0402", block=block, value="22p", conn={"1": vout_net, "2": fb})
    refs = [u.ref, cin.ref, l.ref, co1.ref, co2.ref, r1.ref, r2.ref, cff.ref]
    if en is None or soft_start_rc:
        # EN from VIN through R (+C for delay): simple sequencing by RC
        ren = parts.add(d, "R0402", block=block, value="100k" if soft_start_rc else "10k", conn={"1": vin, "2": en_net})
        refs.append(ren.ref)
        if soft_start_rc:
            cen = parts.add(d, "C0402", block=block, value="100n", conn={"1": en_net, "2": gnd})
            refs.append(cen.ref)
    _near(d, cin.ref, f"{u.ref}.VIN", 1.5, "Cin tight to VIN/GND (hot loop)")
    _near(d, l.ref, f"{u.ref}.SW", 2.0, "short SW node")
    _near(d, co1.ref, l.ref, 3.0)
    _near(d, co2.ref, l.ref, 3.0)
    _near(d, r1.ref, f"{u.ref}.FB", 2.0, "FB divider close to FB pin")
    _near(d, r2.ref, f"{u.ref}.FB", 2.0)
    _near(d, cff.ref, f"{u.ref}.FB", 2.5)
    return {"refs": refs, "calc": {"vout_target": vout, "vout_actual": round(va, 4), "r_top": _fmt_r(rt),
                                   "r_bot": _fmt_r(rb), "L": f"{L_uH}uH", "iout_max": 2.0, "iout_req": iout},
            "nets": [sw, fb, en_net]}


def decoupling(d: Design, block: str, ic: str, net: str, values: list[str], pins: list[str] | None = None,
               gnd: str = "GND", max_mm: float = 2.5) -> dict:
    """Decoupling caps for one rail of an IC; each cap gets a `near` constraint to a power pin."""
    comp = d.components[ic]
    if pins is None:
        pins = [p for p, n in comp.pins.items() if n == net]
    refs = []
    for i, v in enumerate(values):
        pkg = "C0805" if v.startswith("22u") else "C0603" if v.startswith(("10u", "4.7u", "4u7")) else "C0402"
        c = parts.add(d, pkg, block=block, value=v, conn={"1": net, "2": gnd})
        refs.append(c.ref)
        if pins:
            _near(d, c.ref, f"{ic}.{pins[i % len(pins)]}", max_mm if pkg == "C0402" else max_mm + 2.0,
                  f"decouple {net}")
    return {"refs": refs}


def crystal(d: Design, block: str, ic: str, xin: str, xout: str, freq: str = "24MHz", cl_pf: float = 12.0,
            cstray_pf: float = 3.0, series_r: str | None = None, feedback_r: str | None = None, gnd: str = "GND") -> dict:
    """Crystal + load caps: C = 2*(CL - Cstray)."""
    c_load = max(2.0, 2 * (cl_pf - cstray_pf))
    std = min([6, 8, 10, 12, 15, 18, 20, 22, 27, 33], key=lambda v: abs(v - c_load))
    xi, xo = f"{ic}_XIN", f"{ic}_XOUT"
    parts.connect(d, ic, {xin: xi})
    y = parts.add(d, "XTAL_3225", block=block, value=freq, conn={"1": xi, "2": gnd, "4": gnd})
    refs = [y.ref]
    xo_x = xo
    if series_r:
        xo_x = f"{ic}_XOUT_R"
        r = parts.add(d, "R0402", block=block, value=series_r, conn={"1": xo, "2": xo_x})
        refs.append(r.ref)
        _near(d, r.ref, f"{ic}.{xout}", 2.0)
    parts.connect(d, ic, {xout: xo})
    parts.connect(d, y.ref, {"3": xo_x})
    c1 = parts.add(d, "C0402", block=block, value=f"{std}p", conn={"1": xi, "2": gnd})
    c2 = parts.add(d, "C0402", block=block, value=f"{std}p", conn={"1": xo_x, "2": gnd})
    refs += [c1.ref, c2.ref]
    if feedback_r:
        rf = parts.add(d, "R0402", block=block, value=feedback_r, conn={"1": xi, "2": xo_x})
        refs.append(rf.ref)
        _near(d, rf.ref, y.ref, 3.0)
    _near(d, y.ref, f"{ic}.{xin}", 4.0, "short crystal traces")
    _near(d, c1.ref, y.ref, 2.0)
    _near(d, c2.ref, y.ref, 2.0)
    return {"refs": refs, "calc": {"C_load_each_pF": std, "formula": "2*(CL-Cstray)"}}


def usb_c_sink(d: Design, block: str, vbus: str = "VBUS", dp: str = "USB_DP", dm: str = "USB_DM",
               esd: bool = True, gnd: str = "GND") -> dict:
    """USB-C 2.0 receptacle as UFP/sink: 5.1k Rd on CC1/CC2, D+/D- tied A/B, optional USBLC6 ESD."""
    j = parts.add(d, "USB_C_16P", block=block)
    cc1, cc2 = "USB_CC1", "USB_CC2"
    dpc, dmc = (dp + "_C", dm + "_C") if False else (dp, dm)
    parts.connect(d, j.ref, {"VBUS": vbus, "GND": gnd, "CC1": cc1, "CC2": cc2, "A6": dpc, "B6": dpc,
                             "A7": dmc, "B7": dmc, "SBU1": "NC", "SBU2": "NC", "SH": gnd})
    r1 = parts.add(d, "R0402", block=block, value="5.1k", conn={"1": cc1, "2": gnd})
    r2 = parts.add(d, "R0402", block=block, value="5.1k", conn={"1": cc2, "2": gnd})
    refs = [j.ref, r1.ref, r2.ref]
    _near(d, r1.ref, j.ref, 5.0)
    _near(d, r2.ref, j.ref, 5.0)
    if esd:
        u = parts.add(d, "USBLC6-2SC6", block=block, conn={"1": dpc, "6": dpc, "3": dmc, "4": dmc, "2": gnd, "5": vbus})
        refs.append(u.ref)
        _near(d, u.ref, j.ref, 5.0, "ESD at connector")
    d.constraints.append(Constraint(kind="diffpair", p=dp, n=dm, impedance=90, tol_mm=1.0))
    return {"refs": refs, "connector": j.ref}


def led(d: Design, block: str, net: str, color: str = "green", r: str = "1k", gnd: str = "GND",
        active_low: bool = False, supply: str | None = None) -> dict:
    """Indicator LED. active_low: LED from `supply` to `net` (GPIO sinks)."""
    if active_low:
        mid = f"LED_{net}_K"
        l = parts.add(d, "LED0603", block=block, value=color, conn={"A": supply, "K": mid})
        rr = parts.add(d, "R0402", block=block, value=r, conn={"1": mid, "2": net})
    else:
        mid = f"LED_{net}_A"
        rr = parts.add(d, "R0402", block=block, value=r, conn={"1": net, "2": mid})
        l = parts.add(d, "LED0603", block=block, value=color, conn={"A": mid, "K": gnd})
    _near(d, rr.ref, l.ref, 3.0)
    return {"refs": [l.ref, rr.ref]}


def button(d: Design, block: str, net: str, pull: str | None = None, pull_r: str = "10k", gnd: str = "GND",
           series_r: str | None = None, cap: str | None = None) -> dict:
    """Push button to GND with optional pull-up, series R and debounce C."""
    node = net
    refs = []
    if series_r:
        node = f"{net}_BTN"
        rs = parts.add(d, "R0402", block=block, value=series_r, conn={"1": net, "2": node})
        refs.append(rs.ref)
    sw = parts.add(d, "SW_TS1187A", block=block, conn={"1": node, "2": gnd})
    refs.append(sw.ref)
    if pull:
        rp = parts.add(d, "R0402", block=block, value=pull_r, conn={"1": pull, "2": net})
        refs.append(rp.ref)
    if cap:
        c = parts.add(d, "C0402", block=block, value=cap, conn={"1": net, "2": gnd})
        refs.append(c.ref)
    return {"refs": refs}


def resistor(d: Design, block: str, a: str, b: str, value: str, near: str | None = None, pkg: str = "R0402",
             max_mm: float = 3.0) -> dict:
    r = parts.add(d, pkg, block=block, value=value, conn={"1": a, "2": b})
    if near:
        _near(d, r.ref, near, max_mm)
    return {"refs": [r.ref]}


def capacitor(d: Design, block: str, a: str, b: str, value: str, near: str | None = None, pkg: str = "C0402",
              max_mm: float = 3.0) -> dict:
    c = parts.add(d, pkg, block=block, value=value, conn={"1": a, "2": b})
    if near:
        _near(d, c.ref, near, max_mm)
    return {"refs": [c.ref]}


def ferrite(d: Design, block: str, a: str, b: str, near: str | None = None) -> dict:
    f = parts.add(d, "FB0402", block=block, conn={"1": a, "2": b})
    if near:
        _near(d, f.ref, near, 4.0)
    return {"refs": [f.ref]}


BLOCKS = {
    "buck_tlv62569": buck_tlv62569, "decoupling": decoupling, "crystal": crystal, "usb_c_sink": usb_c_sink,
    "led": led, "button": button, "resistor": resistor, "capacitor": capacitor, "ferrite": ferrite,
}


def describe() -> dict:
    import inspect
    out = {}
    for k, f in BLOCKS.items():
        sig = str(inspect.signature(f)).replace("d: llmpcb.ir.Design, ", "").replace("d: Design, ", "")
        doc = (f.__doc__ or "").strip()
        out[k] = {"params": sig, "doc": doc.splitlines()[0] if doc else ""}
    return out
