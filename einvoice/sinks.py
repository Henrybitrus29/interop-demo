"""Where a validated UBL document goes.

FileSink: writes each document to disk. Good for a first look or for feeding a separately-run AS4/Peppol client.
PeppolSandboxSink: POSTs to a Peppol test-network sandbox provider's HTTP API. No two sandbox providers use the
same API shape, so this is a thin, provider-agnostic wrapper: pass the full send URL and headers your provider's
docs give you. It is NOT a real Peppol Access Point and must never be pointed at a production endpoint.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class SinkResult:
    ok: bool
    retryable: bool = False
    detail: str = ""


class FileSink:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def send(self, xml: str, invoice_id: str = "") -> SinkResult:
        safe_id = "".join(c for c in invoice_id if c.isalnum() or c in "-_") or "invoice"
        name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{safe_id}-{uuid.uuid4().hex[:8]}.xml"
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(xml)
        return SinkResult(True, detail=name)


class PeppolSandboxSink:
    """POSTs a UBL document to a Peppol *test network* sandbox endpoint. Retries 5xx/429/408 and network errors;
    treats other 4xx as permanent failures. Never use this against a production Access Point from this project."""

    def __init__(self, send_url: str, headers: dict | None = None, timeout: float = 20.0, attempts: int = 3,
                 backoff: float = 0.5, session=None):
        import requests
        self.url, self.headers = send_url, dict(headers or {})
        self.timeout, self.attempts, self.backoff = timeout, attempts, backoff
        self.session = session or requests.Session()

    def send(self, xml: str, invoice_id: str = "") -> SinkResult:
        import requests
        headers = {"Content-Type": "application/xml", **self.headers}
        last = ""
        for attempt in range(1, self.attempts + 1):
            try:
                r = self.session.post(self.url, data=xml.encode("utf-8"), headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = f"network error: {type(exc).__name__}"
            else:
                if r.status_code in (200, 201, 202):
                    return SinkResult(True, detail=f"HTTP {r.status_code}")
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if not (r.status_code >= 500 or r.status_code in (408, 429)):
                    return SinkResult(False, retryable=False, detail=last)
            if attempt < self.attempts:
                time.sleep(self.backoff * attempt)
        return SinkResult(False, retryable=True, detail=last)


class DeadLetter:
    """Every invoice that failed mapping, validation or delivery, with the reason. Never silently lost.
    Files are owner-readable only: invoice data is financial/personal data. Encrypt this directory and set a
    retention policy before using this in production."""

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def put(self, source: str, stage: str, reason: str, issues: list[str] | None = None) -> str:
        import json
        stem = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        for suffix, data in ((".src", source.encode("utf-8")),
                             (".json", json.dumps({"stage": stage, "reason": reason[:500], "issues": (issues or [])[:50]},
                                                  indent=2).encode())):
            fd = os.open(os.path.join(self.dir, stem + suffix), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        return stem

    def items(self) -> list[str]:
        return sorted(f[:-4] for f in os.listdir(self.dir) if f.endswith(".src"))

    def read(self, stem: str) -> str:
        with open(os.path.join(self.dir, stem + ".src"), encoding="utf-8") as f:
            return f.read()

    def stage(self, stem: str) -> str:
        """Where this entry failed: "map", "validate" or "deliver". "" if the sidecar is missing or unreadable.
        It matters because the .src file holds the original invoice JSON for "map" failures but generated XML for
        the other two."""
        import json
        try:
            with open(os.path.join(self.dir, stem + ".json"), encoding="utf-8") as f:
                return str(json.load(f).get("stage", ""))
        except (OSError, ValueError, AttributeError):
            return ""

    def remove(self, stem: str) -> None:
        for suffix in (".src", ".json"):
            try:
                os.remove(os.path.join(self.dir, stem + suffix))
            except FileNotFoundError:
                pass
