"""parse -> map -> validate -> deliver, with an HL7 ACK for every message and a dead-letter entry for every failure."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from . import hl7v2
from .mapper import MapConfig, MappingError, convert
from .sinks import DeadLetter
from .validate import errors, validate_bundle

log = logging.getLogger("hl7bridge")


@dataclass
class Outcome:
    code: str                       # AA, AE, AR
    text: str
    control_id: str = ""
    counts: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    bundle: dict | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10] if value else ""


class Pipeline:
    def __init__(self, sink, dead_letter: DeadLetter | None = None, cfg: MapConfig | None = None):
        self.sink, self.dead, self.cfg = sink, dead_letter, cfg or MapConfig()

    def _fail(self, code: str, stage: str, reason: str, raw: bytes, msg: hl7v2.Message | None, issues: list[str] | None = None) -> tuple[Outcome, bytes]:
        control = msg.control_id if msg else ""
        if self.dead:
            self.dead.put(raw, stage, reason, _hash(control), issues)
        # Log the event, never the content: no names, ids, or values from the message.
        log.warning(json.dumps({"event": "message_failed", "stage": stage, "ack": code, "control_hash": _hash(control), "reason": reason[:120]}))
        return Outcome(code, reason, control), hl7v2.build_ack(msg, code, reason)

    def process(self, raw: bytes) -> tuple[Outcome, bytes]:
        try:
            msg = hl7v2.parse(raw)
        except hl7v2.HL7Error as exc:
            return self._fail("AR", "parse", f"unparseable message: {exc}", raw, None)
        try:
            conv = convert(msg, self.cfg)
        except MappingError as exc:
            return self._fail("AR", "map", str(exc), raw, msg)
        issues = validate_bundle(conv.bundle)
        errs = errors(issues)
        if errs:
            return self._fail("AR", "validate", f"generated FHIR failed validation ({len(errs)} errors)", raw, msg, [str(i) for i in errs])
        result = self.sink.send(conv.bundle)
        if not result.ok:
            code = "AE" if result.retryable else "AR"
            return self._fail(code, "deliver", f"delivery failed: {result.detail}", raw, msg)
        text = "OK" if not conv.warnings else f"OK with {len(conv.warnings)} warning(s)"
        log.info(json.dumps({"event": "message_processed", "ack": "AA", "control_hash": _hash(conv.control_id), "counts": conv.counts,
                             "warnings": len(conv.warnings)}))
        return Outcome("AA", text, conv.control_id, conv.counts, conv.warnings, conv.bundle), hl7v2.build_ack(msg, "AA", text)

    def replay(self) -> dict:
        """Re-run everything in the dead-letter store. Successes are removed; failures stay (and get a fresh entry only if the reason changed)."""
        assert self.dead is not None
        done = kept = 0
        for stem in self.dead.items():
            raw = self.dead.read(stem)
            dead, self.dead = self.dead, None        # do not re-queue while replaying
            try:
                outcome, _ = self.process(raw)
            finally:
                self.dead = dead
            if outcome.code == "AA":
                self.dead.remove(stem)
                done += 1
            else:
                kept += 1
        return {"replayed_ok": done, "still_failing": kept}
