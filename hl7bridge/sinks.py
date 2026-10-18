"""Where a validated Bundle goes, and the dead-letter store for everything that could not be processed."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .fhirjson import dumps


@dataclass
class SinkResult:
    ok: bool
    retryable: bool = False
    detail: str = ""


class FileSink:
    """Writes each bundle to a file. Handy for demos and for feeding a bulk loader."""

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def send(self, bundle: dict) -> SinkResult:
        name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.json"
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(dumps(bundle))
        return SinkResult(True, detail=name)


class FhirSink:
    """POSTs the transaction Bundle to a FHIR server base URL.

    5xx, 429, 408 and network errors are retryable (the sender should resend: ACK AE). Other 4xx are permanent (ACK AR).
    A 200 whose entries contain an error status is treated as a failure, because a server may report partial problems there.
    """

    def __init__(self, base_url: str, token: str = "", timeout: float = 15.0, attempts: int = 3, backoff: float = 0.5, session=None):
        import requests
        self.url = base_url.rstrip("/")
        self.token, self.timeout, self.attempts, self.backoff = token, timeout, attempts, backoff
        self.session = session or requests.Session()

    def send(self, bundle: dict) -> SinkResult:
        import requests
        headers = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = dumps(bundle, indent=None).encode("utf-8")
        last = ""
        for attempt in range(1, self.attempts + 1):
            try:
                r = self.session.post(self.url, data=body, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = f"network error: {type(exc).__name__}"
            else:
                if r.status_code in (200, 201):
                    return self._check_response(r)
                last = f"HTTP {r.status_code}: {self._outcome_text(r)}"
                if not (r.status_code >= 500 or r.status_code in (408, 429)):
                    return SinkResult(False, retryable=False, detail=last)
            if attempt < self.attempts:
                time.sleep(self.backoff * attempt)
        return SinkResult(False, retryable=True, detail=last)

    @staticmethod
    def _outcome_text(r) -> str:
        try:
            data = r.json()
            if data.get("resourceType") == "OperationOutcome":
                return "; ".join(i.get("diagnostics") or (i.get("details") or {}).get("text", "") for i in data.get("issue", []))[:300]
        except ValueError:
            pass
        return r.text[:200]

    @staticmethod
    def _check_response(r) -> SinkResult:
        try:
            data = r.json()
        except ValueError:
            return SinkResult(False, retryable=False, detail="server returned a non-JSON success response")
        if data.get("resourceType") == "Bundle":
            bad = [e.get("response", {}).get("status", "") for e in data.get("entry", [])
                   if not str(e.get("response", {}).get("status", "2")).startswith("2")]
            if bad:
                return SinkResult(False, retryable=False, detail=f"{len(bad)} entries failed: {bad[0]}")
        return SinkResult(True)


class DeadLetter:
    """Keeps every message that could not be processed, with the reason, so nothing is ever silently lost.
    Files are owner-readable only. Raw HL7 contains patient data: encrypt this directory and set a retention policy in real use."""

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def put(self, raw: bytes, stage: str, reason: str, control_hash: str = "", issues: list[str] | None = None) -> str:
        stem = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        for suffix, data in ((".hl7", raw), (".json", json.dumps({"stage": stage, "reason": reason[:500], "control_hash": control_hash,
                                                                  "issues": (issues or [])[:50]}, indent=2).encode())):
            fd = os.open(os.path.join(self.dir, stem + suffix), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        return stem

    def items(self) -> list[str]:
        return sorted(f[:-4] for f in os.listdir(self.dir) if f.endswith(".hl7"))

    def read(self, stem: str) -> bytes:
        with open(os.path.join(self.dir, stem + ".hl7"), "rb") as f:
            return f.read()

    def remove(self, stem: str) -> None:
        for suffix in (".hl7", ".json"):
            try:
                os.remove(os.path.join(self.dir, stem + suffix))
            except FileNotFoundError:
                pass
