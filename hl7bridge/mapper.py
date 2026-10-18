"""Map an HL7 v2 ORU^R01 (lab results) message to a FHIR R4 transaction Bundle.

Design choices (see docs/DESIGN.md):
- Deterministic ids (UUID v5 over natural keys) so re-sending a message produces the same resources: no duplicates, and a
  corrected result overwrites the earlier one.
- Patient is created only if absent (conditional create). A results feed must never overwrite demographics owned by ADT.
- Anything that cannot be mapped faithfully raises MappingError. A silently dropped lab result is worse than a rejected message.
- Loosely follows the HL7 v2-to-FHIR approach; it is NOT claimed to be conformant to that implementation guide.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from .hl7v2 import Message, Segment, has_explicit_tz, hl7_ts

NS = uuid.UUID("5f0c1c0e-6a53-4c5e-9b0a-2f7c1d0a7a11")   # fixed namespace for deterministic ids

LOINC, SNOMED, UCUM = "http://loinc.org", "http://snomed.info/sct", "http://unitsofmeasure.org"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
INTERP = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
DAR = "http://terminology.hl7.org/CodeSystem/data-absent-reason"

CODE_SYSTEMS = {"LN": LOINC, "LOINC": LOINC, "SCT": SNOMED, "SNM": SNOMED, "SNOMED": SNOMED, "SNOMEDCT": SNOMED,
                "I10": "http://hl7.org/fhir/sid/icd-10", "ICD10": "http://hl7.org/fhir/sid/icd-10",
                "CPT": "http://www.ama-assn.org/go/cpt"}

# Units that are valid UCUM as written. Anything else is kept as display text only: we never guess a code.
KNOWN_UCUM = {"mmol/L", "mg/dL", "g/dL", "g/L", "ug/dL", "ng/mL", "pg/mL", "ng/dL", "mEq/L", "U/L", "IU/L", "[IU]/L", "%", "fL", "pg",
              "10*3/uL", "10*6/uL", "10*9/L", "10*12/L", "/uL", "umol/L", "mL/min", "s", "min", "mm[Hg]", "kg", "g", "mg", "mL", "L",
              "mmol/mol", "mg/L", "ug/mL", "mIU/L", "[iU]/L", "U/mL", "Cel", "mosm/kg", "mm/h", "ratio", "{ratio}", "1"}

OBS_STATUS = {"F": "final", "P": "preliminary", "C": "corrected", "D": "entered-in-error", "W": "entered-in-error", "X": "cancelled",
              "I": "registered", "S": "preliminary", "R": "preliminary", "U": "final", "O": "registered"}
DR_STATUS = {"O": "registered", "I": "registered", "S": "registered", "A": "partial", "P": "preliminary", "R": "preliminary",
             "F": "final", "C": "corrected", "X": "cancelled", "Y": "unknown", "Z": "unknown"}
SR_STATUS = {"F": "completed", "C": "completed", "X": "revoked"}
GENDER = {"M": "male", "F": "female", "O": "other", "U": "unknown", "A": "other", "N": "unknown"}
INTERP_OK = {"N", "L", "H", "LL", "HH", "A", "AA", "<", ">", "U", "D", "B", "W", "S", "R", "I", "MS", "VS", "POS", "NEG", "DET", "ND", "IND"}


class MappingError(ValueError):
    """The message is well-formed HL7 but cannot be mapped faithfully. Do not retry unchanged."""


class UnsupportedMessage(MappingError):
    pass


@dataclass
class MapConfig:
    default_tz: str = "+0000"                         # applied to timestamps that carry no offset (a warning is raised)
    system_map: dict[str, str] = field(default_factory=dict)   # assigning authority namespace -> identifier system URI
    accept_processing_ids: tuple[str, ...] = ("P",)   # MSH-11. Test (T) and debug (D) traffic is rejected by default


@dataclass
class Conversion:
    bundle: dict
    warnings: list[str]
    control_id: str
    counts: dict


def rid(kind: str, *parts: str) -> str:
    return str(uuid.uuid5(NS, kind + "|" + "|".join(parts)))


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-") or "unspecified"


class _Ctx:
    def __init__(self, cfg: MapConfig):
        self.cfg = cfg
        self.warnings: list[str] = []
        self._seen: set[str] = set()

    def warn(self, text: str) -> None:
        if text not in self._seen:          # one line per distinct problem, not one per repeating segment
            self._seen.add(text)
            self.warnings.append(text)

    sender: str = "unknown-sender"

    def system_for(self, namespace: str, universal: str = "", utype: str = "", kind: str = "id") -> str:
        if universal and utype.upper() == "ISO":
            return f"urn:oid:{universal}"
        if namespace and namespace in self.cfg.system_map:
            return self.cfg.system_map[namespace]
        if namespace:
            self.warn(f"identifier system for authority '{namespace}' not in system_map; using urn:hl7v2:{_slug(namespace)}")
            return f"urn:hl7v2:{_slug(namespace)}"
        # No authority given: scope the identifier to the sending application and facility so two senders cannot collide.
        self.warn(f"{kind} identifier has no assigning authority; scoped to the sender ({self.sender})")
        return f"urn:hl7v2:{self.sender}:{kind}"

    def ts(self, value: str, label: str, want: str = "any") -> str | None:
        if not value:
            return None
        res = hl7_ts(value, self.cfg.default_tz)
        if res is None:
            self.warn(f"{label}: invalid timestamp ignored")
            return None
        out, precision = res
        if precision in ("day", "month", "year") or has_explicit_tz(value):
            pass
        else:
            self.warn(f"{label}: no time zone in message, assumed {self.cfg.default_tz}")
        if want == "date":
            return out[:10] if precision not in ("year", "month") else out
        return out


# ---------------------------------------------------------------- building blocks

def coding(seg: Segment, n: int, ctx: _Ctx, rep: int = 1) -> dict | None:
    """CWE/CE field -> CodeableConcept."""
    codings = []
    text = seg.get(n, 2, rep) or seg.get(n, 5, rep)
    for idc, txc, syc in ((1, 2, 3), (4, 5, 6)):
        code, disp, sysname = seg.get(n, idc, rep), seg.get(n, txc, rep), seg.get(n, syc, rep)
        if not code:
            continue
        key = sysname.upper()
        system = CODE_SYSTEMS.get(key)
        if system is None:
            system = f"urn:hl7v2:codesystem:{_slug(sysname)}" if sysname else "urn:hl7v2:codesystem:unspecified"
            ctx.warn(f"code system '{sysname or '(none)'}' is not a known terminology; mapped to a placeholder system")
        c = {"system": system, "code": code}
        if disp:
            c["display"] = disp
        codings.append(c)
    if not codings and not text:
        return None
    cc: dict = {}
    if codings:
        cc["coding"] = codings
    if text:
        cc["text"] = text
    return cc


def unit_of(seg: Segment, n: int) -> tuple[str, str | None]:
    code, text, sysname = seg.get(n, 1), seg.get(n, 2), seg.get(n, 3)
    display = code or text
    if not display:
        return "", None
    if sysname.upper() == "UCUM" or display in KNOWN_UCUM:
        return display, display
    return display, None


_NUM = r"[+-]?\d+(?:\.\d+)?"


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text.lstrip("+"))
    except InvalidOperation:
        return None


def quantity(value: Decimal, unit: tuple[str, str | None], comparator: str | None = None) -> dict:
    q: dict = {"value": value}
    if comparator:
        q["comparator"] = comparator
    if unit[0]:
        q["unit"] = unit[0]
    if unit[1]:
        q["system"], q["code"] = UCUM, unit[1]
    return q


def map_value(obx: Segment, ctx: _Ctx) -> dict:
    vtype = obx.get(2).upper()
    unit = unit_of(obx, 6)
    if obx.reps(5) > 1:
        ctx.warn("OBX-5 has repeating values; only the first is mapped")
    field_is_empty = not obx.has(5) or (5 < len(obx.raw_fields) and obx.raw_fields[5].split(obx.delims.rep)[0] == "")
    if field_is_empty:
        return {}
    raw = obx.get(5)
    if vtype == "NM":
        m = re.fullmatch(rf"(<=|>=|<|>)?\s*({_NUM})", raw)
        if m and (d := _dec(m.group(2))) is not None:
            return {"valueQuantity": quantity(d, unit, m.group(1))}
        ctx.warn("OBX-5 declared NM but is not numeric; kept as text")
        return {"valueString": raw}
    if vtype == "SN":
        cmp_, n1, sep, n2 = obx.get(5, 1), obx.get(5, 2), obx.get(5, 3), obx.get(5, 4)
        d1 = _dec(n1) if re.fullmatch(_NUM, n1 or "x") else None
        if d1 is not None and not sep and cmp_ in ("", "<", ">", "<=", ">=", "="):
            return {"valueQuantity": quantity(d1, unit, cmp_ if cmp_ not in ("", "=") else None)}
        d2 = _dec(n2) if re.fullmatch(_NUM, n2 or "x") else None
        if d1 is not None and d2 is not None and sep == "-":
            rng: dict = {"low": quantity(d1, unit), "high": quantity(d2, unit)}
            for k in ("low", "high"):
                rng[k].pop("comparator", None)
            return {"valueRange": rng}
        ctx.warn("OBX-5 structured numeric (ratio or titer) kept as text")
        return {"valueString": raw.replace(obx.delims.comp, "")}
    if vtype in ("ST", "TX", "FT"):
        return {"valueString": raw}
    if vtype in ("CE", "CWE", "CNE", "IS", "ID"):
        if vtype in ("IS", "ID"):
            return {"valueCodeableConcept": {"text": raw}}
        cc = coding(obx, 5, ctx)
        return {"valueCodeableConcept": cc} if cc else {}
    if vtype in ("DT", "DTM", "TS"):
        out = ctx.ts(raw, "OBX-5")
        return {"valueDateTime": out} if out else {"valueString": raw}
    if vtype == "TM" and re.fullmatch(r"\d{2}(\d{2})?(\d{2})?", raw):
        t = raw.ljust(6, "0")
        return {"valueTime": f"{t[:2]}:{t[2:4]}:{t[4:6]}"}
    raise MappingError(f"OBX-2 value type '{vtype}' is not supported (e.g. embedded documents); message rejected so it is not silently dropped")


def reference_range(obx: Segment, unit: tuple[str, str | None]) -> list[dict]:
    raw = obx.get(7)
    if not raw:
        return []
    rr: dict = {"text": raw}

    def q(x: str) -> dict:
        return quantity(Decimal(x.lstrip("+")), unit)

    m = re.fullmatch(rf"\s*({_NUM})\s*-\s*({_NUM})\s*", raw)
    if m:
        rr["low"], rr["high"] = q(m.group(1)), q(m.group(2))
    else:
        m = re.fullmatch(rf"\s*(<=?|>=?)\s*({_NUM})\s*", raw)
        if m:
            rr["high" if m.group(1).startswith("<") else "low"] = q(m.group(2))
    return [rr]


def interpretation(obx: Segment) -> list[dict]:
    out = []
    for r in range(1, obx.reps(8) + 1):
        code = obx.get(8, 1, r).upper()
        if not code:
            continue
        if code in INTERP_OK:
            out.append({"coding": [{"system": INTERP, "code": code}]})
        else:
            out.append({"text": code})
    return out


# ---------------------------------------------------------------- resources

def map_patient(pid: Segment, ctx: _Ctx) -> tuple[dict, tuple[str, str]]:
    idents = []
    for r in range(1, pid.reps(3) + 1):
        value = pid.get(3, 1, r)
        if not value:
            continue
        ident = {"system": ctx.system_for(pid.get(3, 4, r, sub=1), pid.get(3, 4, r, sub=2), pid.get(3, 4, r, sub=3), "patient"), "value": value}
        tcode = pid.get(3, 5, r)
        if re.fullmatch(r"[A-Z]{1,5}", tcode):
            ident["type"] = {"coding": [{"system": V2_0203, "code": tcode}]}
        idents.append(ident)
    if not idents:
        raise MappingError("PID-3 (patient identifier) is missing; a result cannot be attached to an unknown patient")
    key = next((i for i in idents if i.get("type", {}).get("coding", [{}])[0].get("code") == "MR"), idents[0])

    pat: dict = {"resourceType": "Patient", "identifier": idents}
    names = []
    for r in range(1, pid.reps(5) + 1):
        family = pid.get(5, 1, r, sub=1)
        given = [g for g in (pid.get(5, 2, r), pid.get(5, 3, r)) if g]
        if not family and not given:
            continue
        n: dict = {}
        if family:
            n["family"] = family
        if given:
            n["given"] = given
        if pid.get(5, 4, r):
            n["suffix"] = [pid.get(5, 4, r)]
        if pid.get(5, 5, r):
            n["prefix"] = [pid.get(5, 5, r)]
        use = {"L": "official", "M": "maiden"}.get(pid.get(5, 7, r).upper())
        if use:
            n["use"] = use
        names.append(n)
    if names:
        pat["name"] = names
    g = GENDER.get(pid.get(8).upper())
    if g:
        pat["gender"] = g
    elif pid.get(8):
        ctx.warn("PID-8 gender code not recognised; omitted")
    if pid.get(7):
        bd = ctx.ts(pid.get(7), "PID-7", want="date")
        if bd:
            pat["birthDate"] = bd
    addrs = []
    for r in range(1, pid.reps(11) + 1):
        a: dict = {}
        lines = [x for x in (pid.get(11, 1, r, sub=1), pid.get(11, 2, r)) if x]
        if lines:
            a["line"] = lines
        for comp, k in ((3, "city"), (4, "state"), (5, "postalCode"), (6, "country")):
            if pid.get(11, comp, r):
                a[k] = pid.get(11, comp, r)
        use = {"H": "home", "O": "work"}.get(pid.get(11, 7, r).upper())
        if use and a:
            a["use"] = use
        if a:
            addrs.append(a)
    if addrs:
        pat["address"] = addrs
    tele = []
    for field_no, use in ((13, "home"), (14, "work")):
        for r in range(1, pid.reps(field_no) + 1):
            number = pid.get(field_no, 1, r) or pid.get(field_no, 12, r)
            email = pid.get(field_no, 4, r)
            if number:
                tele.append({"system": "phone", "value": number, "use": use})
            if email:
                tele.append({"system": "email", "value": email, "use": use})
    if tele:
        pat["telecom"] = tele
    dec = pid.get(30).upper()
    if dec in ("Y", "N"):
        pat["deceasedBoolean"] = dec == "Y"
    return pat, (key["system"], key["value"])


def _ei(seg: Segment, n: int, ctx: _Ctx, kind: str) -> tuple[str, str] | None:
    value = seg.get(n, 1)
    if not value:
        return None
    ns, uid, utype = seg.get(n, 2), seg.get(n, 3), seg.get(n, 4)
    return ctx.system_for(ns, uid, utype, kind), value


def map_order_and_report(obr: Segment, orc: Segment | None, patient_ref: str, fallback_key: str, ctx: _Ctx) -> tuple[dict | None, dict, str]:
    placer = _ei(obr, 2, ctx, "placer-order") or (_ei(orc, 2, ctx, "placer-order") if orc else None)
    filler = _ei(obr, 3, ctx, "filler-order") or (_ei(orc, 3, ctx, "filler-order") if orc else None)
    key = (filler or placer or (None, fallback_key))[1]
    rstatus_code = obr.get(25).upper()
    if not rstatus_code:
        ctx.warn("OBR-25 (result status) missing; report status set to 'unknown'")
    code = coding(obr, 4, ctx)
    if code is None:
        ctx.warn("OBR-4 (test/panel code) missing; report code set to a generic text")
        code = {"text": "Unspecified laboratory report"}

    sr = None
    sr_id = rid("ServiceRequest", *(filler or placer or ("", fallback_key)))
    if placer or filler:
        idents = []
        for pair, tcode in ((placer, "PLAC"), (filler, "FILL")):
            if pair:
                idents.append({"type": {"coding": [{"system": V2_0203, "code": tcode}]}, "system": pair[0], "value": pair[1]})
        sr = {"resourceType": "ServiceRequest", "id": sr_id, "identifier": idents,
              "status": SR_STATUS.get(rstatus_code, "active" if rstatus_code else "unknown"), "intent": "order",
              "code": code, "subject": {"reference": patient_ref}}
        authored = ctx.ts(orc.get(9), "ORC-9") if orc and orc.get(9) else None
        if authored:
            sr["authoredOn"] = authored
    else:
        ctx.warn("no placer or filler order number; ServiceRequest not created and the report id falls back to the message control id")

    dr_id = rid("DiagnosticReport", *(filler or placer or ("", fallback_key)))
    dr: dict = {"resourceType": "DiagnosticReport", "id": dr_id,
                "identifier": [{"system": (filler or placer)[0], "value": (filler or placer)[1]}] if (filler or placer) else
                [{"system": "urn:hl7bridge:message", "value": fallback_key}],
                "status": DR_STATUS.get(rstatus_code, "unknown"),
                "category": [{"coding": [{"system": V2_0074, "code": "LAB"}]}],
                "code": code, "subject": {"reference": patient_ref}}
    if sr:
        dr["basedOn"] = [{"reference": f"ServiceRequest/{sr_id}"}]
    eff = ctx.ts(obr.get(7), "OBR-7")
    if eff:
        dr["effectiveDateTime"] = eff
    issued = ctx.ts(obr.get(22), "OBR-22")
    if issued and "T" in issued:
        dr["issued"] = issued
    return sr, dr, key


def map_observation(obx: Segment, index: int, dr: dict, dr_key: str, patient_ref: str, notes: list[str], ctx: _Ctx) -> dict:
    code = coding(obx, 3, ctx)
    if code is None:
        raise MappingError(f"OBX-{index}: observation identifier (OBX-3) is missing")
    set_id = obx.get(1) or str(index)
    oid = rid("Observation", dr_key, set_id, obx.get(3, 1), obx.get(4))
    scode = obx.get(11).upper()
    if not scode:
        ctx.warn("OBX-11 (result status) missing; observation status set to 'unknown'")
    unit = unit_of(obx, 6)
    obs: dict = {"resourceType": "Observation", "id": oid,
                 "identifier": [{"system": "urn:hl7bridge:observation", "value": f"{dr_key}/{set_id}"}],
                 "status": OBS_STATUS.get(scode, "unknown"),
                 "category": [{"coding": [{"system": OBS_CAT, "code": "laboratory"}]}],
                 "code": code, "subject": {"reference": patient_ref}}
    eff = ctx.ts(obx.get(14), "OBX-14") if obx.get(14) else dr.get("effectiveDateTime")
    if eff:
        obs["effectiveDateTime"] = eff
    value = map_value(obx, ctx)
    if value:
        obs.update(value)
    else:
        reason = "not-performed" if scode == "X" else "unknown"
        obs["dataAbsentReason"] = {"coding": [{"system": DAR, "code": reason}]}
    interp = interpretation(obx)
    if interp:
        obs["interpretation"] = interp
    rr = reference_range(obx, unit)
    if rr:
        obs["referenceRange"] = rr
    if notes:
        obs["note"] = [{"text": n} for n in notes]
    return obs


# ---------------------------------------------------------------- message

def convert(msg: Message, cfg: MapConfig | None = None) -> Conversion:
    cfg = cfg or MapConfig()
    ctx = _Ctx(cfg)
    ctx.sender = _slug(f"{msg.msh.get(3)}-{msg.msh.get(4)}".strip("-")) if (msg.msh.get(3) or msg.msh.get(4)) else "unknown-sender"
    mt = msg.msh.get(9, 1).upper() + "^" + msg.msh.get(9, 2).upper()
    if mt != "ORU^R01":
        raise UnsupportedMessage(f"message type {mt or '(missing)'} is not supported (only ORU^R01)")
    pid_mode = msg.msh.get(11).upper() or "P"
    if pid_mode not in cfg.accept_processing_ids:
        raise MappingError(f"MSH-11 processing id '{pid_mode}' is not accepted (test or debug traffic must not reach production)")
    version = msg.msh.get(12)
    if version and not re.match(r"^2\.[3-8]", version):
        ctx.warn(f"HL7 version {version} was not tested; mapping assumes 2.3 to 2.8")
    control = msg.control_id or "no-control-id"
    if not msg.control_id:
        ctx.warn("MSH-10 (message control id) is missing")

    pids = msg.all("PID")
    if len(pids) != 1:
        raise MappingError("exactly one PID segment is required" if pids else "PID segment is missing")
    patient, (psys, pval) = map_patient(pids[0], ctx)
    patient_full = f"urn:uuid:{rid('Patient', psys, pval)}"

    entries: list[dict] = [{
        "fullUrl": patient_full, "resource": patient,
        "request": {"method": "POST", "url": "Patient", "ifNoneExist": f"identifier={quote(psys, safe=':/')}%7C{quote(pval, safe='')}"}}]
    srs, drs, obss = [], [], []

    # walk the segments in order: ORC? OBR (NTE)* (OBX (NTE)*)*
    orc: Segment | None = None
    obr: Segment | None = None
    dr: dict | None = None
    dr_key = ""
    current_notes_target: str = ""        # "obr" or "obx"
    report_notes: list[str] = []
    pending_obx: tuple[Segment, int] | None = None
    obx_notes: list[str] = []
    obr_count = 0
    obx_count = 0

    def flush_obx() -> None:
        nonlocal pending_obx, obx_notes, obx_count
        if pending_obx is None:
            return
        assert dr is not None
        obs = map_observation(pending_obx[0], pending_obx[1], dr, dr_key, patient_full, obx_notes, ctx)
        obss.append(obs)
        dr.setdefault("result", []).append({"reference": f"Observation/{obs['id']}"})
        obx_count += 1
        pending_obx, obx_notes = None, []

    def flush_report() -> None:
        nonlocal report_notes
        flush_obx()
        if dr is not None:
            if report_notes:
                dr["conclusion"] = " ".join(report_notes)[:2000]
            drs.append(dr)
        report_notes = []

    for seg in msg.segments[1:]:
        if seg.name == "ORC":
            orc = seg
        elif seg.name == "OBR":
            flush_report()
            obr_count += 1
            fallback = f"{control}-{seg.get(1) or obr_count}"
            sr, dr, dr_key = map_order_and_report(seg, orc, patient_full, fallback, ctx)
            if sr:
                srs.append(sr)
            obr, orc, current_notes_target = seg, None, "obr"
        elif seg.name == "OBX":
            if dr is None:
                raise MappingError("OBX segment appears before any OBR")
            flush_obx()
            pending_obx = (seg, obx_count + 1)
            current_notes_target = "obx"
        elif seg.name == "NTE":
            text = seg.get(3) if seg.reps(3) <= 1 else " ".join(seg.get(3, 1, r) for r in range(1, seg.reps(3) + 1))
            if not text:
                continue
            (obx_notes if current_notes_target == "obx" and pending_obx else report_notes).append(text)
        elif seg.name in ("SPM", "PV1", "PV2", "NK1", "DG1", "IN1", "SFT", "TQ1", "TQ2", "CTD", "FT1"):
            ctx.warn(f"{seg.name} segment ignored (not mapped in this version)")
    flush_report()

    if not obss:
        raise MappingError("message contains no OBX results")
    for r in srs:
        entries.append({"resource": r, "request": {"method": "PUT", "url": f"ServiceRequest/{r['id']}"}})
    for r in drs:
        entries.append({"resource": r, "request": {"method": "PUT", "url": f"DiagnosticReport/{r['id']}"}})
    for r in obss:
        entries.append({"resource": r, "request": {"method": "PUT", "url": f"Observation/{r['id']}"}})

    bundle = {"resourceType": "Bundle", "type": "transaction",
              "identifier": {"system": "urn:hl7v2:message-control-id", "value": control}, "entry": entries}
    return Conversion(bundle, ctx.warnings, control,
                      {"Patient": 1, "ServiceRequest": len(srs), "DiagnosticReport": len(drs), "Observation": len(obss)})
