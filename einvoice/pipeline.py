"""map -> validate -> deliver, with a dead-letter entry for every failure. Nothing is silently lost or sent unvalidated."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .mapper import MapConfig, MappingError, convert
from .sinks import DeadLetter
from .validate import Issue, errors, validate_xml

log = logging.getLogger("einvoice")


@dataclass
class Outcome:
    ok: bool
    stage: str = ""              # "map" | "validate" | "deliver" | "" (ok)
    reason: str = ""
    invoice_id: str = ""
    issues: list[Issue] = field(default_factory=list)
    xml: str | None = None


class Pipeline:
    def __init__(self, sink, dead_letter: DeadLetter | None = None, cfg: MapConfig | None = None,
                 skip_schematron: bool = False):
        """skip_schematron: still runs the mapper's own checks, but skips the slower official rule set — useful
        for quick local iteration, never for anything that will actually be sent."""
        self.sink, self.dead, self.cfg, self.skip_schematron = sink, dead_letter, cfg or MapConfig(), skip_schematron

    def _fail(self, stage: str, reason: str, source: dict | str, issues: list[str] | None = None) -> Outcome:
        src_text = source if isinstance(source, str) else json.dumps(source, default=str, indent=2)
        if self.dead:
            self.dead.put(src_text, stage, reason, issues)
        log.warning(json.dumps({"event": "invoice_failed", "stage": stage, "reason": reason[:160]}))
        return Outcome(False, stage, reason, issues=[Issue("n/a", "error", i) for i in (issues or [])] if issues else [])

    def process(self, invoice: dict) -> Outcome:
        invoice_id = str(invoice.get("invoice_number", ""))
        try:
            xml = convert(invoice, self.cfg)
        except MappingError as exc:
            return self._fail("map", str(exc), invoice)
        if not self.skip_schematron:
            issues = validate_xml(xml)
            errs = errors(issues)
            if errs:
                out = self._fail("validate", f"EN 16931 validation failed ({len(errs)} error(s))", xml, [str(i) for i in errs])
                out.invoice_id = invoice_id
                return out
        result = self.sink.send(xml, invoice_id)
        if not result.ok:
            out = self._fail("deliver", f"delivery failed: {result.detail}", xml)
            out.invoice_id = invoice_id
            return out
        log.info(json.dumps({"event": "invoice_sent", "invoice_id": invoice_id}))
        return Outcome(True, invoice_id=invoice_id, xml=xml)

    def _replay_one(self, source: str, stage: str) -> bool:
        """Re-run one dead-lettered entry. What the stored source is depends on where it failed:
        - "map": the original invoice JSON, so it goes through the whole pipeline again (map, validate, deliver)
        - "validate" / "deliver": the generated UBL XML, so it is validated (unless skipped) and delivered
        If the stage is unknown (an entry with no readable sidecar), the content decides: XML starts with "<"."""
        is_invoice_json = stage == "map" or (not stage and not source.lstrip().startswith("<"))
        dead, self.dead = self.dead, None            # do not re-queue the entry while replaying it
        try:
            if is_invoice_json:
                try:
                    invoice = json.loads(source)
                except ValueError:
                    return False
                return isinstance(invoice, dict) and self.process(invoice).ok
            if not self.skip_schematron and errors(validate_xml(source)):
                return False
            return self.sink.send(source).ok
        finally:
            self.dead = dead

    def replay(self) -> dict:
        """Re-run everything in the dead-letter store. Entries that now succeed are removed; entries that still
        fail stay where they are. One bad entry (or a validator/sink that raises) never aborts the whole run."""
        assert self.dead is not None
        done = kept = 0
        for stem in self.dead.items():
            try:
                ok = self._replay_one(self.dead.read(stem), self.dead.stage(stem))
            except Exception as exc:                 # keep going; the entry stays in the store
                log.warning(json.dumps({"event": "replay_error", "entry": stem, "error": type(exc).__name__}))
                ok = False
            if ok:
                self.dead.remove(stem)
                done += 1
            else:
                kept += 1
        return {"replayed_ok": done, "still_failing": kept}
