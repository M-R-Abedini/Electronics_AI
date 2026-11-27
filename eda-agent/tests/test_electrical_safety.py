# SPDX-License-Identifier: Apache-2.0
"""Unit tests for first-principles electrical safety & stress audit checks."""

from __future__ import annotations

from pathlib import Path

from eda_agent.core.kicad_netlist import parse_kicadxml_netlist, snapshot_from_netlist
from eda_agent.core.review_engine import review_snapshot
from eda_agent.core.snapshot import DesignSnapshot


def _snap(parts, pins):
    return DesignSnapshot.build("test", parts, pins)


def _findings_by_code(res: dict, code: str) -> list[dict]:
    return [f for f in res["findings"] if f["code"] == code]


def test_regulator_en_overvoltage_tps709_and_clean_floating_en():
    parts = [
        {"refdes": "U1", "value": "TPS70933DBVR", "lib_ref": "Regulator_Linear:TPS70933"},
        {"refdes": "C1", "value": "1uF"},
        {"refdes": "C2", "value": "2.2uF"},
    ]
    bad_pins = [
        {"refdes": "U1", "pin": "1", "net": "V24_OUT", "pinfunction": "IN"},
        {"refdes": "U1", "pin": "3", "net": "V24_OUT", "pinfunction": "EN"},
        {"refdes": "U1", "pin": "2", "net": "GND", "pinfunction": "GND"},
        {"refdes": "U1", "pin": "5", "net": "V3V3", "pinfunction": "OUT"},
        {"refdes": "C1", "pin": "1", "net": "V24_OUT"},
        {"refdes": "C1", "pin": "2", "net": "GND"},
        {"refdes": "C2", "pin": "1", "net": "V3V3"},
        {"refdes": "C2", "pin": "2", "net": "GND"},
    ]
    res = review_snapshot(_snap(parts, bad_pins))
    hits = _findings_by_code(res, "regulator_en_overvoltage")
    assert len(hits) == 1
    assert hits[0]["severity"] == "error"
    assert "U1" in hits[0]["refs"]

    # Clean case: EN left unconnected (floating internal pull-up)
    clean_pins = [p for p in bad_pins if not (p["refdes"] == "U1" and p["pin"] == "3")]
    res_clean = review_snapshot(_snap(parts, clean_pins))
    assert not _findings_by_code(res_clean, "regulator_en_overvoltage")


def test_hv_undersized_passive_0603_and_snubber_1206_vs_tht_clean():
    parts = [
        {"refdes": "R1", "value": "2M", "footprint": "Resistor_SMD:R_0603_1608Metric"},
        {"refdes": "R2", "value": "47k", "footprint": "Resistor_SMD:R_1206_3216Metric"},
        {"refdes": "R3", "value": "2M", "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"},
        {"refdes": "R4", "value": "47k", "footprint": "Resistor_THT:R_Axial_DIN0411_L9.9mm_D3.6mm_P15.24mm_Horizontal"},
    ]
    pins = [
        {"refdes": "R1", "pin": "1", "net": "/PSU/HV_BUS"},
        {"refdes": "R1", "pin": "2", "net": "/PSU/V_SENSE"},
        {"refdes": "R2", "pin": "1", "net": "/PSU/HV_BUS"},
        {"refdes": "R2", "pin": "2", "net": "/PSU/RCD_CLAMP"},
        {"refdes": "R3", "pin": "1", "net": "/PSU/HV_BUS"},
        {"refdes": "R3", "pin": "2", "net": "/PSU/V_SENSE"},
        {"refdes": "R4", "pin": "1", "net": "/PSU/HV_BUS"},
        {"refdes": "R4", "pin": "2", "net": "/PSU/RCD_CLAMP"},
    ]
    res = review_snapshot(_snap(parts, pins))
    hits = _findings_by_code(res, "hv_undersized_passive")
    by_ref = {h["refs"][0]: h for h in hits}
    assert by_ref["R1"]["severity"] == "error"
    assert by_ref["R2"]["severity"] == "warning"
    assert "R3" not in by_ref
    assert "R4" not in by_ref


def test_mains_fuse_and_bridge_voltage_ratings():
    parts = [
        {"refdes": "F1", "value": "500mA T 250VAC", "lib_ref": "Device:Fuse"},
        {"refdes": "F2", "value": "500mA T 300VAC HBC", "lib_ref": "Device:Fuse"},
        {"refdes": "BR1", "value": "MB6S", "lib_ref": "Diode_Bridge:MB6S"},
        {"refdes": "BR2", "value": "MB10S", "lib_ref": "Diode_Bridge:MB10S"},
    ]
    pins = [
        {"refdes": "F1", "pin": "1", "net": "AC_L"},
        {"refdes": "F1", "pin": "2", "net": "AC_L_FUSED"},
        {"refdes": "F2", "pin": "1", "net": "AC_L"},
        {"refdes": "F2", "pin": "2", "net": "AC_L_FUSED"},
        {"refdes": "BR1", "pin": "1", "net": "AC_L_FUSED"},
        {"refdes": "BR1", "pin": "2", "net": "AC_N"},
        {"refdes": "BR2", "pin": "1", "net": "AC_L_FUSED"},
        {"refdes": "BR2", "pin": "2", "net": "AC_N"},
    ]
    res = review_snapshot(_snap(parts, pins))
    fuse_hits = _findings_by_code(res, "mains_fuse_voltage_rating")
    assert len(fuse_hits) == 1 and fuse_hits[0]["refs"] == ["F1"]
    br_hits = _findings_by_code(res, "hv_rectifier_bridge_voltage_margin")
    assert len(br_hits) == 1 and br_hits[0]["refs"] == ["BR1"]


def test_bidirectional_tvs_on_unipolar_ic_vs_unidirectional_and_bipolar():
    # Unipolar IC (AVDD=24V, AVSS=GND) + bidirectional SMBJ24CA -> flagged
    ic_pins = [
        {"refdes": "U1", "pin": "1", "net": "V24", "pinfunction": "AVDD"},
        {"refdes": "U1", "pin": "2", "net": "GND", "pinfunction": "AVSS"},
        {"refdes": "U1", "pin": "3", "net": "IOUT_RAW", "pinfunction": "IOUT"},
        {"refdes": "U1", "pin": "4", "net": "SPI_CLK"},
        {"refdes": "U1", "pin": "5", "net": "SPI_MOSI"},
        {"refdes": "U1", "pin": "6", "net": "SPI_CS"},
    ]
    parts = [
        {"refdes": "U1", "value": "DAC8760"},
        {"refdes": "R1", "value": "22R"},
        {"refdes": "D1", "value": "SMBJ24CA"},
        {"refdes": "D2", "value": "SMBJ18A"},
    ]
    pins = ic_pins + [
        {"refdes": "R1", "pin": "1", "net": "IOUT_RAW"},
        {"refdes": "R1", "pin": "2", "net": "IOUT_FIELD"},
        {"refdes": "D1", "pin": "1", "net": "IOUT_FIELD"},
        {"refdes": "D1", "pin": "2", "net": "GND"},
        {"refdes": "D2", "pin": "1", "net": "IOUT_FIELD"},
        {"refdes": "D2", "pin": "2", "net": "GND"},
    ]
    res = review_snapshot(_snap(parts, pins))
    hits = _findings_by_code(res, "bidirectional_tvs_on_unipolar_ic")
    assert len(hits) == 1 and hits[0]["refs"] == ["D1", "U1"]

    # Adding a negative supply rail (-15V) makes U1 bipolar -> bidirectional TVS is legal!
    bipolar_pins = pins + [{"refdes": "U1", "pin": "7", "net": "-15V", "pinfunction": "AVSS"}]
    res_bi = review_snapshot(_snap(parts, bipolar_pins))
    assert not _findings_by_code(res_bi, "bidirectional_tvs_on_unipolar_ic")


def test_flyback_fwd_highside_polarity_and_switcher_vout_headroom():
    parts = [
        {"refdes": "U1", "value": "INN3692C"},
        {"refdes": "T1", "value": "EE16_Flyback"},
        {"refdes": "D2", "value": "ES2D"},
        {"refdes": "R5", "value": "47R"},
    ]
    # High-side rectification: D2 Anode on winding, Cathode on V24_OUT -> FWD sees negative swing!
    highside_pins = [
        {"refdes": "T1", "pin": "3", "net": "GND_OUT"},
        {"refdes": "T1", "pin": "4", "net": "SEC_SW"},
        {"refdes": "D2", "pin": "2", "net": "SEC_SW", "pinfunction": "A"},
        {"refdes": "D2", "pin": "1", "net": "V24_OUT", "pinfunction": "K"},
        {"refdes": "R5", "pin": "1", "net": "SEC_SW"},
        {"refdes": "R5", "pin": "2", "net": "FLY_FWD"},
        {"refdes": "U1", "pin": "7", "net": "FLY_FWD", "pinfunction": "FWD"},
        {"refdes": "U1", "pin": "6", "net": "V24_OUT", "pinfunction": "VOUT"},
        {"refdes": "U1", "pin": "4", "net": "FLY_BPS", "pinfunction": "BPS"},
    ]
    res = review_snapshot(_snap(parts, highside_pins))
    fwd_hits = _findings_by_code(res, "flyback_fwd_highside_polarity")
    assert len(fwd_hits) == 1 and fwd_hits[0]["severity"] == "error"
    vout_hits = _findings_by_code(res, "switcher_vout_headroom")
    assert len(vout_hits) == 1

    # Low-side rectification on 15 V rail -> both clear!
    lowside_pins = [
        {"refdes": "T1", "pin": "4", "net": "V15_OUT"},
        {"refdes": "T1", "pin": "3", "net": "SEC_SW"},
        {"refdes": "D2", "pin": "2", "net": "GND_OUT", "pinfunction": "A"},
        {"refdes": "D2", "pin": "1", "net": "SEC_SW", "pinfunction": "K"},
        {"refdes": "R5", "pin": "1", "net": "SEC_SW"},
        {"refdes": "R5", "pin": "2", "net": "FLY_FWD"},
        {"refdes": "U1", "pin": "7", "net": "FLY_FWD", "pinfunction": "FWD"},
        {"refdes": "U1", "pin": "6", "net": "V15_OUT", "pinfunction": "VOUT"},
        {"refdes": "U1", "pin": "4", "net": "FLY_BPS", "pinfunction": "BPS"},
    ]
    res_ok = review_snapshot(_snap(parts, lowside_pins))
    assert not _findings_by_code(res_ok, "flyback_fwd_highside_polarity")
    assert not _findings_by_code(res_ok, "switcher_vout_headroom")


def test_sense_outside_series_resistor_and_kicad_xml_pinfunction(tmp_path: Path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <export version="E">
      <components>
        <comp ref="U8"><value>DAC8760</value><footprint>Package_SO:HTSSOP-24</footprint></comp>
        <comp ref="R20"><value>10R</value><footprint>Resistor_SMD:R_0805</footprint></comp>
        <comp ref="J3"><value>Conn_01x02</value><footprint>TerminalBlock:TB2</footprint></comp>
      </components>
      <nets>
        <net code="1" name="VOUT_RAW">
          <node ref="U8" pin="21" pinfunction="VOUT" pintype="output"/>
          <node ref="U8" pin="22" pinfunction="+VSENSE" pintype="input"/>
          <node ref="R20" pin="1" pinfunction="1" pintype="passive"/>
        </net>
        <net code="2" name="VOUT_EXT">
          <node ref="R20" pin="2" pinfunction="2" pintype="passive"/>
          <node ref="J3" pin="1" pinfunction="Pin_1" pintype="passive"/>
        </net>
      </nets>
    </export>
    """
    xml_file = tmp_path / "netlist.xml"
    xml_file.write_text(xml, encoding="utf-8")
    nl = parse_kicadxml_netlist(str(xml_file))
    snap = snapshot_from_netlist(nl, board_name="test.kicad_sch")
    res = review_snapshot(snap)
    hits = _findings_by_code(res, "sense_outside_series_resistor")
    assert len(hits) == 1
    assert set(hits[0]["refs"]) == {"U8", "R20", "J3"}
