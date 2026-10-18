"""Validation of the generated Bundle.

Layer 1: fhir.resources (R4B) models. They check structure, types, required elements and primitive formats, but NOT code
         bindings (an invalid status code passes), so layer 2 adds them.
Layer 2: required code bindings and a few invariants that matter for lab data.
Layer 3 (optional, CI): the official HAPI validator with real profiles; see docs/VALIDATION.md. Not run in this repo's sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from fhir.resources.R4B import get_fhir_model_class

REQUIRED_BINDINGS = {
    ("Observation", "status"): {"registered", "preliminary", "final", "amended", "corrected", "cancelled", "entered-in-error", "unknown"},
    ("DiagnosticReport", "status"): {"registered", "partial", "preliminary", "final", "amended", "corrected", "appended", "cancelled",
                                     "entered-in-error", "unknown"},
    ("ServiceRequest", "status"): {"draft", "active", "on-hold", "revoked", "completed", "entered-in-error", "unknown"},
    ("ServiceRequest", "intent"): {"proposal", "plan", "directive", "order", "original-order", "reflex-order", "filler-order",
                                   "instance-order", "option"},
    ("Patient", "gender"): {"male", "female", "other", "unknown"},
}


@dataclass
class Issue:
    severity: str          # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.where}: {self.message}"


def _refs(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference" and isinstance(v, str):
                out.append(v)
            else:
                _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)


def validate_bundle(bundle: dict, now: datetime | None = None) -> list[Issue]:
    issues: list[Issue] = []
    now = now or datetime.now(timezone.utc)
    if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
        return [Issue("error", "Bundle", "must be a transaction Bundle")]
    entries = bundle.get("entry") or []
    if not entries:
        return [Issue("error", "Bundle.entry", "bundle is empty")]

    known: set[str] = set()
    for i, e in enumerate(entries):
        res, req = e.get("resource"), e.get("request")
        where = f"entry[{i}]"
        if not isinstance(res, dict) or "resourceType" not in res:
            issues.append(Issue("error", where, "entry has no resource"))
            continue
        if not req or req.get("method") not in ("POST", "PUT") or not req.get("url"):
            issues.append(Issue("error", where, "entry needs request.method POST or PUT and request.url"))
        rt = res["resourceType"]
        if e.get("fullUrl"):
            if e["fullUrl"] in known:
                issues.append(Issue("error", where, f"duplicate fullUrl {e['fullUrl']}"))
            known.add(e["fullUrl"])
        if res.get("id"):
            key = f"{rt}/{res['id']}"
            if key in known:
                issues.append(Issue("error", where, f"duplicate resource {key}"))
            known.add(key)
            if req and req.get("method") == "PUT" and req.get("url") != key:
                issues.append(Issue("error", where, f"PUT url {req.get('url')} does not match resource {key}"))
        try:
            cls = get_fhir_model_class(rt)
            cls.model_validate(res)
        except Exception as exc:  # pydantic ValidationError carries one entry per problem
            for err in getattr(exc, "errors", lambda: [{"loc": (), "msg": str(exc)}])():
                issues.append(Issue("error", f"{where}.{rt}." + ".".join(str(x) for x in err.get("loc", ())), str(err.get("msg"))))
            continue
        for (t, element), allowed in REQUIRED_BINDINGS.items():
            if t == rt and element in res and res[element] not in allowed:
                issues.append(Issue("error", f"{where}.{rt}.{element}", f"'{res[element]}' is not in the required value set"))
        _rules(res, rt, where, now, issues)

    for i, e in enumerate(entries):
        refs: list[str] = []
        _refs(e.get("resource"), refs)
        for r in refs:
            if r not in known:
                issues.append(Issue("error", f"entry[{i}]", f"reference {r} does not resolve inside the bundle"))
    return issues


def _rules(res: dict, rt: str, where: str, now: datetime, issues: list[Issue]) -> None:
    if rt == "Patient":
        if not res.get("identifier"):
            issues.append(Issue("error", f"{where}.Patient", "patient has no identifier"))
        bd = res.get("birthDate")
        if bd and len(bd) == 10 and date.fromisoformat(bd) > now.date():
            issues.append(Issue("error", f"{where}.Patient.birthDate", "birth date is in the future"))
    if rt == "Observation":
        has_value = any(k.startswith("value") for k in res)
        if not has_value and "dataAbsentReason" not in res:
            issues.append(Issue("error", f"{where}.Observation", "needs a value or a dataAbsentReason"))
        if has_value and "dataAbsentReason" in res:
            issues.append(Issue("error", f"{where}.Observation", "value and dataAbsentReason are mutually exclusive"))
    if rt in ("Observation", "DiagnosticReport"):
        eff = res.get("effectiveDateTime")
        if eff and len(eff) > 10:
            try:
                when = datetime.fromisoformat(eff)
                if when > now + timedelta(days=1):
                    issues.append(Issue("error", f"{where}.{rt}.effectiveDateTime", "result time is in the future"))
            except ValueError:
                issues.append(Issue("error", f"{where}.{rt}.effectiveDateTime", "unparseable date time"))
    if rt == "DiagnosticReport" and res.get("status") in ("final", "corrected") and not res.get("result") and not res.get("conclusion"):
        issues.append(Issue("warning", f"{where}.DiagnosticReport", "final report has neither results nor a conclusion"))


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
