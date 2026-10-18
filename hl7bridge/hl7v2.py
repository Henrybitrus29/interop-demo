"""Minimal, strict HL7 v2.x parser, MLLP framing and ACK builder (no third-party dependencies)."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

SB, EB, CR = b"\x0b", b"\x1c", b"\r"
MAX_MESSAGE_BYTES = 1_000_000


class HL7Error(ValueError):
    """The message cannot be parsed. Resending the same bytes will not help."""


@dataclass(frozen=True)
class Delims:
    field: str = "|"
    comp: str = "^"
    rep: str = "~"
    esc: str = "\\"
    sub: str = "&"


def unescape(text: str, d: Delims) -> str:
    """Apply HL7 escape sequences. Unknown sequences are left untouched rather than guessed."""
    if d.esc not in text:
        return text
    out, i = [], 0
    table = {"F": d.field, "S": d.comp, "T": d.sub, "R": d.rep, "E": d.esc, ".br": "\n", ".sp": "\n"}
    while i < len(text):
        if text[i] != d.esc:
            out.append(text[i])
            i += 1
            continue
        j = text.find(d.esc, i + 1)
        if j == -1:                       # unterminated: keep literally
            out.append(text[i:])
            break
        code = text[i + 1:j]
        if code in table:
            out.append(table[code])
        elif re.fullmatch(r"X(?:[0-9A-Fa-f]{2})+", code):
            out.append(bytes.fromhex(code[1:]).decode("utf-8", "replace"))
        else:
            out.append(text[i:j + 1])
        i = j + 1
    return "".join(out)


@dataclass
class Segment:
    name: str
    raw_fields: list[str]                # raw_fields[n] is HL7 field n (index 0 is the segment name)
    delims: Delims = field(default_factory=Delims)

    def _comps(self, n: int, rep: int) -> list[str]:
        if n >= len(self.raw_fields):
            return []
        if self.name == "MSH" and n <= 2:             # MSH-1 and MSH-2 hold the delimiters themselves
            return [self.raw_fields[n]]
        reps = self.raw_fields[n].split(self.delims.rep)
        return reps[rep - 1].split(self.delims.comp) if 0 < rep <= len(reps) else []

    def get(self, n: int, comp: int = 1, rep: int = 1, sub: int | None = None) -> str:
        comps = self._comps(n, rep)
        if comp - 1 >= len(comps):
            return ""
        value = comps[comp - 1]
        if sub is not None:
            parts = value.split(self.delims.sub)
            value = parts[sub - 1] if sub - 1 < len(parts) else ""
        return unescape(value, self.delims).strip()

    def reps(self, n: int) -> int:
        if n >= len(self.raw_fields) or self.raw_fields[n] == "":
            return 0
        return len(self.raw_fields[n].split(self.delims.rep))

    def has(self, n: int) -> bool:
        return n < len(self.raw_fields) and self.raw_fields[n].strip() != ""


@dataclass
class Message:
    segments: list[Segment]
    delims: Delims

    def all(self, name: str) -> list[Segment]:
        return [s for s in self.segments if s.name == name]

    def first(self, name: str) -> Segment | None:
        return next((s for s in self.segments if s.name == name), None)

    @property
    def msh(self) -> Segment:
        return self.segments[0]

    @property
    def control_id(self) -> str:
        return self.msh.get(10)

    @property
    def message_type(self) -> str:
        t, e = self.msh.get(9, 1), self.msh.get(9, 2)
        return f"{t}^{e}" if e else t


def parse(raw: bytes | str) -> Message:
    if isinstance(raw, bytes):
        if len(raw) > MAX_MESSAGE_BYTES:
            raise HL7Error("message too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")   # many legacy senders still use ISO-8859-1
    else:
        text = raw
    text = text.lstrip("\ufeff\r\n \t")
    if not text.startswith("MSH") or len(text) < 8:
        raise HL7Error("message must start with an MSH segment")
    d = Delims(field=text[3], comp=text[4], rep=text[5], esc=text[6],
               sub=text[7] if text[7] not in (text[3], "\r", "\n") else "&")
    if len({d.field, d.comp, d.rep, d.esc}) != 4 or any(c.isalnum() for c in (d.field, d.comp, d.rep, d.esc)):
        raise HL7Error("invalid delimiter characters in MSH")
    segments: list[Segment] = []
    for line in re.split(r"\r\n|\r|\n", text):
        if not line.strip():
            continue
        name = line[:3]
        if not re.fullmatch(r"[A-Z][A-Z0-9]{2}", name):
            raise HL7Error(f"invalid segment name {name!r}")
        if name == "MSH":
            parts = line.split(d.field)
            fields = [parts[0], d.field] + parts[1:]     # MSH-1 is the separator itself, MSH-2 the encoding characters
        else:
            fields = line.split(d.field)
        segments.append(Segment(name, fields, d))
    if segments[0].name != "MSH":
        raise HL7Error("first segment is not MSH")
    return Message(segments, d)


# ---------------------------------------------------------------- timestamps

_TS = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.(\d{1,6}))?([+-]\d{4})?$")


def hl7_ts(value: str, default_tz: str = "+0000") -> tuple[str, str] | None:
    """Convert an HL7 timestamp to a FHIR date or dateTime string. Returns (value, precision) or None if invalid.
    A time without an offset gets `default_tz` (callers should warn): FHIR requires an offset on any dateTime with a time."""
    m = _TS.match(value.strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    if mo is None:
        return y, "year"
    if int(mo) < 1 or int(mo) > 12:
        return None
    if d is None:
        return f"{y}-{mo}", "month"
    try:
        datetime(int(y), int(mo), int(d))
    except ValueError:
        return None
    if hh is None:
        return f"{y}-{mo}-{d}", "day"
    if int(hh) > 23 or (mi and int(mi) > 59) or (ss and int(ss) > 60):
        return None
    zone = tz or default_tz
    zone = f"{zone[:3]}:{zone[3:]}"
    mi = mi or "00"
    if ss is None:
        return f"{y}-{mo}-{d}T{hh}:{mi}:00{zone}", "minute"
    out = f"{y}-{mo}-{d}T{hh}:{mi}:{ss}"
    if frac:
        out += "." + frac
    return out + zone, "second"


def has_explicit_tz(value: str) -> bool:
    return bool(re.search(r"[+-]\d{4}$", value.strip()))


# ---------------------------------------------------------------- MLLP

def frame(payload: bytes) -> bytes:
    return SB + payload + EB + CR


class Unframer:
    """Feed arbitrary byte chunks, get complete MLLP messages. Raises HL7Error if a frame grows past the limit."""

    def __init__(self, limit: int = MAX_MESSAGE_BYTES):
        self.buf = bytearray()
        self.limit = limit

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf.extend(chunk)
        out: list[bytes] = []
        while True:
            start = self.buf.find(SB)
            if start == -1:
                self.buf.clear()                       # bytes outside a frame are noise
                break
            end = self.buf.find(EB + CR, start + 1)
            if end == -1:
                if len(self.buf) - start > self.limit:
                    self.buf.clear()
                    raise HL7Error("MLLP frame exceeds size limit")
                if start > 0:
                    del self.buf[:start]
                break
            out.append(bytes(self.buf[start + 1:end]))
            del self.buf[:end + 2]
        return out


# ---------------------------------------------------------------- ACK

def build_ack(original: Message | None, code: str, text: str = "", now: datetime | None = None) -> bytes:
    """Build an ACK. `code` is AA (accepted), AE (processing error, sender may retry) or AR (rejected, do not resend as is)."""
    assert code in ("AA", "AE", "AR")
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d%H%M%S")
    clean = re.sub(r"[|^~\\&\r\n]", " ", text)[:200]
    if original is not None:
        m = original.msh
        sending_app, sending_fac = m.get(3), m.get(4)
        recv_app, recv_fac = m.get(5), m.get(6)
        control, version = m.get(10), m.get(12) or "2.5"
        trigger = m.get(9, 2)
    else:
        sending_app = sending_fac = recv_app = recv_fac = control = trigger = ""
        version = "2.5"
    ack_type = f"ACK^{trigger}" if trigger else "ACK"
    msh = f"MSH|^~\\&|{recv_app}|{recv_fac}|{sending_app}|{sending_fac}|{stamp}||{ack_type}|{uuid.uuid4().hex[:20]}|P|{version}"
    msa = f"MSA|{code}|{control}|{clean}"
    return (msh + "\r" + msa + "\r").encode("utf-8")
