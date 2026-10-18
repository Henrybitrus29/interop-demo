"""Run one HL7 message or one invoice through the real converters and describe what happened, stage by stage.

The two functions here mirror the pipelines in the sibling projects (hl7bridge.pipeline and einvoice.pipeline), but
return a description of every stage instead of delivering anything. No file or network I/O happens here, nothing is
stored, and message contents are never logged.

Validators are passed in rather than imported, so the demo can say honestly when a validation layer is not installed
on the server instead of pretending the document is valid.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from decimal import Decimal

from einvoice import mapper as inv_mapper
from hl7bridge import hl7v2
from hl7bridge import mapper as hl7_mapper
from hl7bridge.fhirjson import dumps as fhir_dumps

log = logging.getLogger("demo")

HL7_STAGES = [("parse", "Parse"), ("map", "Map"), ("validate", "Validate"), ("deliver", "Deliver")]
INVOICE_STAGES = [("map", "Map"), ("validate", "Validate"), ("deliver", "Deliver")]
NOT_SENT = "Not sent in this demo"

INTERPRETATION = {"H": "High", "L": "Low", "N": "Normal", "HH": "Critical high", "LL": "Critical low", "A": "Abnormal",
                  "AA": "Critical abnormal", "<": "Off scale low", ">": "Off scale high", "POS": "Positive", "NEG": "Negative",
                  "DET": "Detected", "ND": "Not detected", "IND": "Indeterminate"}


# ---------------------------------------------------------------- stage rail

class _Rail:
    """Tracks which stages passed, failed or were skipped, and renders the list the page draws."""

    def __init__(self, order: list[tuple[str, str]]):
        self.order = order
        self.states: dict[str, tuple[str, str]] = {}

    def passed(self, stage: str, note: str = "") -> None:
        self.states[stage] = ("pass", note)

    def failed(self, stage: str, note: str = "") -> None:
        self.states[stage] = ("fail", note)

    def skipped(self, stage: str, note: str) -> None:
        self.states[stage] = ("skipped", note)

    def render(self) -> list[dict]:
        out = []
        for stage, label in self.order:
            state, note = self.states.get(stage, ("notreached", ""))
            out.append({"id": stage, "label": label, "state": state, "note": note})
        return out


def _issue(source: str, severity: str, message: str, where: str = "") -> dict:
    return {"source": source, "severity": severity, "where": where, "message": message}


def _plain_number(x) -> str:
    return format(x, "f") if isinstance(x, Decimal) else str(x)


# ---------------------------------------------------------------- HL7 v2 to FHIR

def run_hl7(text: str, fhir_validate=None, fhir_errors=None) -> dict:
    """parse -> map -> validate, then say what would be delivered. `fhir_validate(bundle)` returns Issue objects with
    .severity/.where/.message; `fhir_errors(issues)` filters them to errors. Both None means the layer is not installed."""
    rail = _Rail(HL7_STAGES)
    result: dict = {"kind": "hl7", "ok": False, "validated": False, "rail": [], "reason": "", "issues": [],
                    "summary": None, "outputs": {"bundle": "", "ack": ""}}
    msg = None

    def finish(code: str, ack_text: str) -> dict:
        result["ack_code"] = code
        result["outputs"]["ack"] = hl7v2.build_ack(msg, code, ack_text).decode("utf-8").replace("\r", "\n").strip()
        result["rail"] = rail.render()
        return result

    try:
        msg = hl7v2.parse(text)
    except hl7v2.HL7Error as exc:
        rail.failed("parse", "Not readable as HL7")
        result["reason"] = f"This is not a readable HL7 v2 message: {exc}."
        return finish("AR", f"unparseable message: {exc}")
    rail.passed("parse", f"{len(msg.segments)} segments")

    try:
        conv = hl7_mapper.convert(msg, hl7_mapper.MapConfig())
    except hl7_mapper.MappingError as exc:
        rail.failed("map", "Could not map faithfully")
        result["reason"] = str(exc)
        return finish("AR", str(exc))
    except Exception as exc:  # a bug in the converter must not take the page down
        log.warning("unexpected mapping error: %s", type(exc).__name__)
        rail.failed("map", "Unexpected error")
        result["reason"] = f"The converter hit an unexpected error ({type(exc).__name__}) while mapping this message."
        return finish("AR", "unexpected mapping error")
    n_resources = sum(conv.counts.values()) if conv.counts else len(conv.bundle.get("entry", []))
    rail.passed("map", f"{n_resources} FHIR resources")
    result["outputs"]["bundle"] = fhir_dumps(conv.bundle)
    result["summary"] = _hl7_summary(conv)
    for w in conv.warnings:
        result["issues"].append(_issue("mapper", "warning", w))

    if fhir_validate is None:
        rail.skipped("validate", "Validator not installed on this server")
    else:
        try:
            issues = fhir_validate(conv.bundle)
        except Exception as exc:
            log.warning("FHIR validator failed: %s", type(exc).__name__)
            issues = None
            rail.skipped("validate", "Validator failed to run")
        if issues is not None:
            errs = fhir_errors(issues) if fhir_errors else [i for i in issues if i.severity == "error"]
            for i in issues:
                result["issues"].append(_issue("validator", i.severity, i.message, i.where))
            if errs:
                rail.failed("validate", f"{len(errs)} error{'s' if len(errs) != 1 else ''}")
                reason = f"The generated FHIR failed validation ({len(errs)} error{'s' if len(errs) != 1 else ''})."
                result["reason"] = reason
                return finish("AR", reason)
            result["validated"] = True
            rail.passed("validate", "No errors")

    rail.skipped("deliver", NOT_SENT)
    result["ok"] = True
    n_warn = len(conv.warnings)
    return finish("AA", "OK" if not n_warn else f"OK with {n_warn} warning(s)")


def _hl7_summary(conv) -> dict:
    rows = []
    for entry in conv.bundle.get("entry", []):
        res = entry.get("resource") or {}
        if res.get("resourceType") != "Observation":
            continue
        rows.append({"test": _concept_text(res.get("code")) or "(unnamed)", "result": _observation_value(res),
                     "flag": _interpretation(res), "status": res.get("status", "")})
    return {"counts": dict(conv.counts or {}), "control_id": conv.control_id, "observations": rows}


def _concept_text(cc) -> str:
    if not isinstance(cc, dict):
        return ""
    if cc.get("text"):
        return str(cc["text"])
    for c in cc.get("coding") or []:
        if c.get("display"):
            return str(c["display"])
    for c in cc.get("coding") or []:
        if c.get("code"):
            return str(c["code"])
    return ""


def _observation_value(res: dict) -> str:
    for key, val in res.items():
        if not key.startswith("value"):
            continue
        if key == "valueQuantity" and isinstance(val, dict):
            return " ".join(p for p in (val.get("comparator", ""), _plain_number(val.get("value", "")), val.get("unit") or val.get("code") or "") if p)
        if key == "valueString":
            return str(val)
        if key == "valueCodeableConcept":
            return _concept_text(val)
        if key == "valueRange" and isinstance(val, dict):
            lo, hi = (val.get("low") or {}), (val.get("high") or {})
            return f"{_plain_number(lo.get('value', ''))} to {_plain_number(hi.get('value', ''))} {hi.get('unit') or lo.get('unit') or ''}".strip()
        if key == "valueRatio" and isinstance(val, dict):
            n, d = (val.get("numerator") or {}), (val.get("denominator") or {})
            return f"{_plain_number(n.get('value', ''))}:{_plain_number(d.get('value', ''))}"
        if isinstance(val, (str, int, float, bool, Decimal)):
            return _plain_number(val)
        return json.dumps(val, default=str)[:80]
    if res.get("dataAbsentReason"):
        return f"(absent: {_concept_text(res['dataAbsentReason'])})"
    return ""


def _interpretation(res: dict) -> str:
    for cc in res.get("interpretation") or []:
        for c in cc.get("coding") or []:
            code = c.get("code", "")
            if code:
                name = INTERPRETATION.get(code)
                return f"{name} ({code})" if name else code
        if cc.get("text"):
            return str(cc["text"])
    return ""


# ---------------------------------------------------------------- invoice to UBL

_NS = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
       "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"}


def run_invoice(text: str, validate_xml=None) -> dict:
    """map -> validate, then say what would be sent. `validate_xml(xml)` returns Issue objects with
    .rule_id/.severity/.message; None means the official rule set is not installed on this server."""
    rail = _Rail(INVOICE_STAGES)
    result: dict = {"kind": "invoice", "ok": False, "validated": False, "rail": [], "reason": "", "issues": [],
                    "summary": None, "outputs": {"xml": ""}}

    def finish() -> dict:
        result["rail"] = rail.render()
        return result

    try:
        invoice = json.loads(text)
    except ValueError as exc:
        rail.failed("map", "Not valid JSON")
        result["reason"] = f"This isn't valid JSON: {exc}."
        return finish()
    if not isinstance(invoice, dict):
        rail.failed("map", "Not an invoice object")
        result["reason"] = "The invoice must be a JSON object with fields such as seller, buyer and lines."
        return finish()

    try:
        xml = inv_mapper.convert(invoice, inv_mapper.MapConfig())
    except inv_mapper.MappingError as exc:
        rail.failed("map", "Rejected before any XML")
        result["reason"] = str(exc)
        return finish()
    except Exception as exc:
        log.warning("unexpected mapping error: %s", type(exc).__name__)
        rail.failed("map", "Unexpected error")
        result["reason"] = f"The converter hit an unexpected error ({type(exc).__name__}) while mapping this invoice."
        return finish()
    rail.passed("map", "UBL 2.1 XML built")
    result["outputs"]["xml"] = pretty_xml(xml)
    result["summary"] = _invoice_summary(xml)

    if validate_xml is None:
        rail.skipped("validate", "EN 16931 rules not installed on this server")
    else:
        try:
            issues = validate_xml(xml)
        except Exception as exc:
            log.warning("Schematron validator failed: %s", type(exc).__name__)
            issues = None
            rail.skipped("validate", "Validator failed to run")
        if issues is not None:
            errs = [i for i in issues if i.severity == "error"]
            for i in issues:
                result["issues"].append(_issue("EN 16931", i.severity, i.message, i.rule_id))
            if errs:
                rail.failed("validate", f"{len(errs)} rule{'s' if len(errs) != 1 else ''} failed")
                result["reason"] = f"The invoice failed {len(errs)} official EN 16931 rule{'s' if len(errs) != 1 else ''}."
                return finish()
            result["validated"] = True
            rail.passed("validate", "All rules passed")

    rail.skipped("deliver", NOT_SENT)
    result["ok"] = True
    return finish()


def _invoice_summary(xml: str) -> dict | None:
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError:
        return None
    kind = root.tag.rsplit("}", 1)[-1]
    line_tag = "cac:CreditNoteLine" if kind == "CreditNote" else "cac:InvoiceLine"
    qty_tag = "cbc:CreditedQuantity" if kind == "CreditNote" else "cbc:InvoicedQuantity"
    lines = []
    for ln in root.findall(line_tag, _NS):
        qty = ln.find(qty_tag, _NS)
        cat = ln.find("cac:Item/cac:ClassifiedTaxCategory", _NS)
        vat = ""
        if cat is not None:
            vat = f"{cat.findtext('cbc:ID', '', _NS)} {cat.findtext('cbc:Percent', '', _NS)}%"
        lines.append({"name": ln.findtext("cac:Item/cbc:Name", "", _NS),
                      "quantity": (qty.text if qty is not None else "") or "",
                      "unit": qty.get("unitCode", "") if qty is not None else "",
                      "unit_price": ln.findtext("cac:Price/cbc:PriceAmount", "", _NS),
                      "vat": vat,
                      "net": ln.findtext("cbc:LineExtensionAmount", "", _NS)})
    money = root.find("cac:LegalMonetaryTotal", _NS)
    totals = {}
    if money is not None:
        totals = {"net": money.findtext("cbc:TaxExclusiveAmount", "", _NS),
                  "gross": money.findtext("cbc:TaxInclusiveAmount", "", _NS),
                  "payable": money.findtext("cbc:PayableAmount", "", _NS)}
    totals["vat"] = root.findtext("cac:TaxTotal/cbc:TaxAmount", "", _NS)
    return {"document": "Credit note" if kind == "CreditNote" else "Invoice",
            "number": root.findtext("cbc:ID", "", _NS),
            "currency": root.findtext("cbc:DocumentCurrencyCode", "", _NS),
            "lines": lines, "totals": totals}


_TOKEN = re.compile(r"<[^>]+>|[^<]+")


def pretty_xml(xml: str) -> str:
    """Indent generated XML for reading. Display only: the document that would be sent is `xml` itself."""
    tokens = [t for t in _TOKEN.findall(xml) if t.strip()]
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i].strip()
        if tok.startswith("<?") or tok.startswith("<!") or tok.endswith("/>"):
            out.append("  " * depth + tok)
        elif tok.startswith("</"):
            depth = max(0, depth - 1)
            out.append("  " * depth + tok)
        elif (i + 2 < len(tokens) and not tokens[i + 1].lstrip().startswith("<")
              and tokens[i + 2].startswith("</")):
            out.append("  " * depth + tok + tokens[i + 1].strip() + tokens[i + 2].strip())   # <a>text</a> on one line
            i += 2
        else:
            out.append("  " * depth + tok)
            depth += 1
        i += 1
    return "\n".join(out) + "\n"
