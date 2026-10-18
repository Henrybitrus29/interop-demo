"""Validate a generated UBL document against the official EN 16931 Schematron rules.

Uses the pre-compiled XSLT from CEN/TC 434's `eInvoicing-EN16931` project (see `vendor/en16931/`), run through
SaxonC-HE via the `saxonche` Python package — the same engine the Java reference tool (validator + Saxon jar) uses,
with no JVM required. This is the actual EU reference rule set, not a reimplementation.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from saxonche import PySaxonProcessor

XSLT_PATH = Path(__file__).resolve().parent.parent / "vendor" / "en16931" / "EN16931-UBL-validation.xslt"

_FAILED = re.compile(r'<svrl:failed-assert[^>]*\bid="([^"]+)"[^>]*\bflag="(\w+)"[^>]*>.*?<svrl:text>(.*?)</svrl:text>', re.S)
_FAILED_NOFLAG = re.compile(r'<svrl:failed-assert[^>]*\bid="([^"]+)"[^>]*>.*?<svrl:text>(.*?)</svrl:text>', re.S)
_WS = re.compile(r"\s+")


@dataclass
class Issue:
    rule_id: str
    severity: str        # "error" | "warning"
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: [{self.rule_id}] {self.message}"


class ValidatorUnavailable(RuntimeError):
    """The EN 16931 XSLT could not be loaded (missing file, bad Saxon install)."""


# Saxon's compiled stylesheet object is not documented as thread-safe for concurrent transforms from one processor
# instance, and a PySaxonProcessor cannot be shared across threads either, so each thread gets its own compiled
# copy, built once and cached. Compiling takes under a second; do not compile per call.
_local = threading.local()


def _engine():
    cached = getattr(_local, "engine", None)
    if cached is not None:
        return cached
    if not XSLT_PATH.exists():
        raise ValidatorUnavailable(f"EN 16931 XSLT not found at {XSLT_PATH}")
    proc = PySaxonProcessor(license=False)
    xslt = proc.new_xslt30_processor()
    try:
        exe = xslt.compile_stylesheet(stylesheet_file=str(XSLT_PATH))
    except Exception as exc:  # pragma: no cover - depends on the local Saxon install
        raise ValidatorUnavailable(f"could not compile the EN 16931 XSLT: {exc}") from exc
    _local.engine = (proc, exe)
    return _local.engine


def validate_xml(xml: str) -> list[Issue]:
    """Run the official Schematron rules over one UBL document (as a string) and return every failed assertion."""
    _, exe = _engine()
    # saxonche transforms from a file or a pre-parsed node; round-tripping through a temp-free in-memory source
    # isn't exposed for XSLT30, so this writes to a short-lived temp file.
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", encoding="utf-8", delete=False) as f:
        f.write(xml)
        path = f.name
    try:
        out = exe.transform_to_string(source_file=path)
    finally:
        Path(path).unlink(missing_ok=True)
    if out is None:
        raise ValidatorUnavailable("Saxon returned no output; the input is likely not well-formed XML")
    issues = []
    for rule_id, flag, text in _FAILED.findall(out):
        issues.append(Issue(rule_id, "warning" if flag == "warning" else "error", _WS.sub(" ", text).strip()))
    if not issues:
        for rule_id, text in _FAILED_NOFLAG.findall(out):   # some rule sets omit @flag; treat as error
            issues.append(Issue(rule_id, "error", _WS.sub(" ", text).strip()))
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


@lru_cache(maxsize=1)
def is_available() -> bool:
    try:
        _engine()
        return True
    except ValidatorUnavailable:
        return False
