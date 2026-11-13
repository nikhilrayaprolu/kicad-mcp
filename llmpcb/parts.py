"""Curated part database + pin-name resolution.

A part = symbol + footprint + defaults + facts the LLM can query.  Parts whose
symbol/footprint are missing from KiCad's stock libraries are generated on demand
(gen.py) from knowledge files, e.g. the RV1106 from knowledge/rv1106.yaml, so the
pin map comes straight from the datasheet extraction (never typed by hand).
"""
from __future__ import annotations

import fnmatch
import functools
import re
from pathlib import Path

import yaml

from . import gen, libs
from .ir import Component, Design

KNOW = Path(__file__).resolve().parents[1] / "knowledge"

# id -> dict(symbol, footprint, value, mpn, lcsc, desc, facts, ref)
PARTS: dict[str, dict] = {
    # passives (value is set at instantiation)
    "R0402": dict(symbol="Device:R_Small", footprint="Resistor_SMD:R_0402_1005Metric", ref="R", desc="0402 resistor"),
    "R0603": dict(symbol="Device:R_Small", footprint="Resistor_SMD:R_0603_1608Metric", ref="R", desc="0603 resistor"),
    "C0402": dict(symbol="Device:C_Small", footprint="Capacitor_SMD:C_0402_1005Metric", ref="C", desc="0402 MLCC (<=1uF/10V typ)"),
    "C0603": dict(symbol="Device:C_Small", footprint="Capacitor_SMD:C_0603_1608Metric", ref="C", desc="0603 MLCC (<=10uF/10V typ)"),
    "C0805": dict(symbol="Device:C_Small", footprint="Capacitor_SMD:C_0805_2012Metric", ref="C", desc="0805 MLCC (22uF/10V typ)"),
    "FB0402": dict(symbol="Device:FerriteBead_Small", footprint="Inductor_SMD:L_0402_1005Metric", ref="FB",
                   value="120R@100MHz", mpn="BLM15PD121SN1D", desc="ferrite bead 0402"),
    "L2520": dict(symbol="Device:L", footprint="Inductor_SMD:L_Cenker_CKCS252012", ref="L",
                  desc="power inductor 2.5x2.0x1.2 (1-2.2uH, ~2-3A)", mpn="CKCS252012"),
    "LED0603": dict(symbol="Device:LED_Small", footprint="LED_SMD:LED_0603_1608Metric", ref="D", desc="0603 LED (pin1=K)"),
    "D_SOD123F": dict(symbol="Device:D_Schottky", footprint="Diode_SMD:D_SOD-123F", ref="D", value="MBR230LSFT1G",
                      mpn="MBR230LSFT1G", desc="2A 30V Schottky, SOD-123F (pin1=K)"),
    # actives
    "TLV62569DBV": dict(symbol="Regulator_Switching:TLV62569DBV", footprint="Package_TO_SOT_SMD:SOT-23-5", ref="U",
                        value="TLV62569DBV", mpn="TLV62569DBVR", lcsc="C141836",
                        desc="TI 2A sync buck, 2.5-5.5Vin, Vfb=0.6V, 1.5MHz, SOT-23-5",
                        facts={"vfb": 0.6, "fsw_hz": 1.5e6, "iout_a": 2.0, "vin": [2.5, 5.5],
                               "L_uH": "1-2.2 (2.2uH for Vout>1.2V, 1uH for <=1.2V)", "cout": ">=10uF (22uF rec)",
                               "cin": "4.7-10uF", "cff": "optional 22pF across R_top for stability with big Cout",
                               "src": "TI TLV62569 datasheet, section 9.2 (typ. application)"}),
    "USBLC6-2SC6": dict(symbol="Power_Protection:USBLC6-2SC6", footprint="Package_TO_SOT_SMD:SOT-23-6", ref="U",
                        value="USBLC6-2SC6", mpn="USBLC6-2SC6", lcsc="C7519", desc="USB2 ESD protection, 2 lines + VBUS"),
    # connectors / electromech
    "USB_C_16P": dict(symbol="Connector:USB_C_Receptacle_USB2.0_16P", footprint="Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
                      ref="J", value="TYPE-C-31-M-12", mpn="TYPE-C-31-M-12", lcsc="C165948", desc="USB-C 2.0 receptacle (HRO)"),
    "MICROSD_DM3AT": dict(symbol="Connector:Micro_SD_Card_Det_Hirose_DM3AT", footprint="Connector_Card:microSD_HC_Hirose_DM3AT-SF-PEJM5",
                          ref="J", value="DM3AT-SF-PEJM5", mpn="DM3AT-SF-PEJM5", desc="microSD push-push w/ detect"),
    "RJ45_HR911105A": dict(symbol="Connector:RJ45_Hanrun_HR911105A_Horizontal", footprint="Connector_RJ:RJ45_Hanrun_HR911105A_Horizontal",
                           ref="J", value="HR911105A", mpn="HR911105A", lcsc="C12074",
                           desc="RJ45 w/ integrated 10/100 magnetics + LEDs (pins 9-12 LEDs, 8 shield/GND)"),
    "FPC15_1MM": dict(symbol="Connector_Generic:Conn_01x15", footprint="Connector_FFC-FPC:Molex_200528-0150_1x15-1MP_P1.00mm_Horizontal",
                      ref="J", value="FPC 15P 1.0mm", desc="15-pin 1.0mm FPC (RPi-style 2-lane CSI camera cable)"),
    "HDR_2x10": dict(symbol="Connector_Generic:Conn_02x10_Odd_Even", footprint="Connector_PinHeader_2.54mm:PinHeader_2x10_P2.54mm_Vertical",
                     ref="J", value="GPIO 2x10", desc="2.54mm 2x10 header"),
    "HDR_1x3": dict(symbol="Connector_Generic:Conn_01x03", footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
                    ref="J", value="UART", desc="2.54mm 1x3 header"),
    "HDR_1x4": dict(symbol="Connector_Generic:Conn_01x04", footprint="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
                    ref="J", value="HDR", desc="2.54mm 1x4 header"),
    "SW_TS1187A": dict(symbol="Switch:SW_Push", footprint="Button_Switch_SMD:SW_Push_1P1T_XKB_TS-1187A", ref="SW",
                       value="TS-1187A", mpn="TS-1187A-B-A-B", lcsc="C318884", desc="5.1x5.1 SMD tact switch"),
    "XTAL_3225": dict(symbol="Device:Crystal_GND24", footprint="Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm", ref="Y",
                      desc="3225 4-pin crystal (pins 2/4 case GND)"),
}


# ------------------------------------------------------------------ generated parts
@functools.lru_cache(maxsize=None)
def rv1106_data() -> dict:
    return yaml.safe_load((KNOW / "rv1106.yaml").read_text())


def _short(name: str) -> str:
    segs = name.split("/")
    gpio = [s for s in segs if re.match(r"GPIO\d_[A-D]\d|GPI\d_", s)]
    first = segs[0]
    g = re.sub(r"_[udz]$", "", gpio[0]) if gpio else ""
    if g and first != gpio[0]:
        return f"{first}/{g}"
    return re.sub(r"_[udz]$", "", first)


def ensure_rv1106() -> str:
    """Generate RV1106G3 symbol (pins on the physical side) + QFN128 footprint."""
    sym_id, fp_id = "llmpcb:RV1106G3", "llmpcb:QFN-128-1EP_12.3x12.3mm_P0.35mm_EP7x7mm"
    try:
        libs._get_symbol(sym_id); libs._get_footprint(fp_id)
        return sym_id
    except KeyError:
        pass
    data = rv1106_data()
    pins = []
    for num, p in sorted(((k, v) for k, v in data["pins"].items() if str(k).isdigit()), key=lambda kv: int(kv[0])):
        num = int(num)
        t = p["type"]
        nm = p["name"]
        etype = {"power_in": "power_in", "ground": "power_in", "io": "bidirectional", "analog": "passive",
                 "clock": "passive", "nc": "no_connect"}.get(t, "bidirectional")
        if num == 68:
            etype = "input"
        if num == 69:
            etype = "output"
        side = "left" if num <= 32 else "bottom" if num <= 64 else "right" if num <= 96 else "top"
        pins.append({"number": str(num), "name": _short(nm), "type": etype, "side": side})
    pins.append({"number": "129", "name": "VSS_EPAD", "type": "power_in", "side": "left"})
    sym = gen.make_box_symbol("RV1106G3", pins, "U", fp_id, "Rockchip RV1106G3 SoC, QFN128 0.35mm, 2Gb DDR3L in package",
                              "knowledge/sources/Rockchip_RV1106_Datasheet_V2.2-20260602.pdf")
    sym[1] = "RV1106G3"
    gen.save_symbols([sym])
    pk = data["package"]
    fp = gen.qfn("QFN-128-1EP_12.3x12.3mm_P0.35mm_EP7x7mm", 128, 0.35, 12.3, 12.3, pad_l=0.70, pad_w=0.20,
                 ep=(7.0, 7.0), ep_vias=5, toe=0.20, ep_number="129",
                 descr=f"RV1106 QFN128 {pk['body_size_mm']} P{pk['pitch_mm']} EP7x7 (IPC-7351-style, derived from DS p19)")
    gen.save_footprint(fp)
    return sym_id


def ensure_w25n01() -> str:
    sym_id = "llmpcb:W25N01GVZEIG"
    try:
        libs._get_symbol(sym_id)
        return sym_id
    except KeyError:
        pass
    pins = [{"number": "1", "name": "~{CS}", "type": "input", "side": "left"},
            {"number": "6", "name": "CLK", "type": "input", "side": "left"},
            {"number": "5", "name": "DI/IO0", "type": "bidirectional", "side": "right"},
            {"number": "2", "name": "DO/IO1", "type": "bidirectional", "side": "right"},
            {"number": "3", "name": "~{WP}/IO2", "type": "bidirectional", "side": "right"},
            {"number": "7", "name": "~{HOLD}/IO3", "type": "bidirectional", "side": "right"},
            {"number": "8", "name": "VCC", "type": "power_in", "side": "top"},
            {"number": "4", "name": "GND", "type": "power_in", "side": "bottom"},
            {"number": "9", "name": "EP", "type": "passive", "side": "bottom"}]
    sym = gen.make_box_symbol("W25N01GVZEIG", pins, "U", "Package_SON:WSON-8-1EP_8x6mm_P1.27mm_EP3.4x4.3mm",
                              "Winbond 1Gbit SPI NAND, WSON-8 8x6 (EP: GND or float)", "https://www.winbond.com/")
    gen.save_symbols([sym])
    return sym_id


GENERATED = {
    "RV1106G3": dict(ensure=ensure_rv1106, footprint="llmpcb:QFN-128-1EP_12.3x12.3mm_P0.35mm_EP7x7mm", ref="U",
                     value="RV1106G3", mpn="RV1106G3", desc="Rockchip RV1106G3 SoC (Cortex-A7, 1 TOPS NPU, 2Gb DDR3L SiP, FE PHY)"),
    "W25N01GV": dict(ensure=ensure_w25n01, footprint="Package_SON:WSON-8-1EP_8x6mm_P1.27mm_EP3.4x4.3mm", ref="U",
                     value="W25N01GVZEIG", mpn="W25N01GVZEIG", lcsc="C111706", desc="1Gbit SPI NAND, 3.3V, WSON-8 8x6"),
}


libs.GENERATORS.update({
    "llmpcb:RV1106G3": lambda: ensure_rv1106(),
    "llmpcb:QFN-128-1EP_12.3x12.3mm_P0.35mm_EP7x7mm": lambda: ensure_rv1106(),
    "llmpcb:W25N01GVZEIG": lambda: ensure_w25n01(),
})


def part_info(pid: str) -> dict:
    if pid in PARTS:
        p = dict(PARTS[pid])
        sym = libs.get_symbol(p["symbol"])
    elif pid in GENERATED:
        g = GENERATED[pid]
        p = {k: v for k, v in g.items() if k != "ensure"}
        p["symbol"] = g["ensure"]()
        sym = libs.get_symbol(p["symbol"])
    else:
        raise KeyError(f"unknown part {pid}; use part_search")
    p["pins"] = [f"{q.number}:{q.name}:{q.etype}" for q in sym.pins] if len(sym.pins) <= 40 else \
        f"{len(sym.pins)} pins (use pin_query)"
    return p


def search(query: str, limit: int = 15) -> dict:
    q = query.lower()
    words = q.split()
    curated = [{"id": k, "desc": v.get("desc", ""), "symbol": v.get("symbol", "generated")}
               for k, v in {**PARTS, **GENERATED}.items()
               if all(w in (k + " " + v.get("desc", "")).lower() for w in words)]
    stock = []
    for lid, desc, kw in libs.symbol_index():
        hay = f"{lid} {desc} {kw}".lower()
        if all(w in hay for w in words):
            stock.append({"symbol": lid, "desc": desc[:100]})
            if len(stock) >= limit:
                break
    fps = [f for f in libs.footprint_index() if all(w in f.lower() for w in words)][:limit]
    return {"curated": curated[:limit], "kicad_symbols": stock, "kicad_footprints": fps}


# ------------------------------------------------------------------ instantiation
def resolve_pins(symbol: str, key: str) -> list[str]:
    """Map a pin key (number, exact name, '/'-segment of the name, or glob) to pin numbers."""
    sym = libs.get_symbol(symbol)
    nums = [p.number for p in sym.pins]
    if key in nums:
        return [key]
    exact = [p.number for p in sym.pins if p.name == key]
    if exact:
        return exact
    ku = key.upper()
    seg = [p.number for p in sym.pins if ku in [s.upper() for s in p.name.split("/")]]
    if seg:
        return seg
    if symbol.endswith("RV1106G3"):
        # search the full datasheet names (all mux functions)
        full = [str(n) for n, p in rv1106_data()["pins"].items() if str(n).isdigit() and ku in [re.sub(r"_[udz]$", "", s).upper() for s in p["name"].split("/")]]
        if full:
            return full
    glob = [p.number for p in sym.pins if fnmatch.fnmatchcase(p.name.upper(), ku)]
    if glob:
        return glob
    raise KeyError(f"{symbol}: no pin matches {key!r}")


def add(design: Design, pid: str | None = None, *, ref: str | None = None, block: str = "main",
        value: str | None = None, conn: dict[str, str] | None = None, symbol: str | None = None,
        footprint: str | None = None, mpn: str = "", lcsc: str = "", nc: list[str] | None = None,
        note: str = "", dnp: bool = False) -> Component:
    if pid:
        if pid in GENERATED:
            g = GENERATED[pid]
            base = {k: v for k, v in g.items() if k != "ensure"}
            base["symbol"] = g["ensure"]()
        elif pid in PARTS:
            base = dict(PARTS[pid])
        else:
            raise KeyError(f"unknown part {pid}")
    else:
        if not (symbol and footprint):
            raise ValueError("need part id or symbol+footprint")
        base = {"symbol": symbol, "footprint": footprint, "ref": "U"}
    symbol = symbol or base["symbol"]
    footprint = footprint or base["footprint"]
    libs.get_symbol(symbol)
    libs.get_footprint(footprint)
    ref = ref or design.next_ref(base.get("ref", "U"))
    if ref in design.components:
        raise ValueError(f"{ref} exists")
    comp = Component(ref=ref, symbol=symbol, footprint=footprint, value=value or base.get("value", ""),
                     part=pid or "", mpn=mpn or base.get("mpn", ""), lcsc=lcsc or base.get("lcsc", ""),
                     block=block, note=note, dnp=dnp)
    design.components[ref] = comp
    if conn:
        connect(design, ref, conn)
    for k in nc or []:
        comp.nc += resolve_pins(symbol, k)
    return comp


def connect(design: Design, ref: str, conn: dict[str, str]) -> list[str]:
    comp = design.components[ref]
    fp_pads = {p.number for p in libs.get_footprint(comp.footprint).pads}
    done = []
    for key, net in conn.items():
        for n in resolve_pins(comp.symbol, key):
            if n not in fp_pads:
                raise ValueError(f"{ref}: symbol pin {n} has no pad in {comp.footprint}")
            if net in (None, "", "NC"):
                comp.pins.pop(n, None)
                if n not in comp.nc:
                    comp.nc.append(n)
            else:
                comp.pins[n] = net
                if n in comp.nc:
                    comp.nc.remove(n)
            done.append(n)
    return done
