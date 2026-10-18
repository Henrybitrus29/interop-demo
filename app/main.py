"""Web layer for the interoperability demo (Starlette).

GET  /                 the page
GET  /api/samples      sample messages and invoices, with a one-line note each
POST /api/hl7          {"message": "<HL7 v2 text>"}   -> stage-by-stage result
POST /api/invoice      {"invoice": "<invoice JSON text>"} -> stage-by-stage result
GET  /healthz          liveness, plus which validation layers loaded (null = still warming up)

Nothing is stored and message contents are never logged. Inputs are size-limited and requests are rate-limited per
client, so a public link is safe to share.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import demo

log = logging.getLogger("demo.web")

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
SAMPLES = ROOT / "samples"

MAX_BODY_BYTES = 64 * 1024
RATE_LIMIT = int(os.getenv("DEMO_RATE_LIMIT", "40"))        # conversions per client per minute
RATE_WINDOW = 60.0

# ---------------------------------------------------------------- validation layers (optional at import time)

try:
    from hl7bridge.validate import errors as fhir_errors, validate_bundle as fhir_validate
except Exception as exc:                                    # fhir.resources not installed
    fhir_validate = fhir_errors = None
    log.warning("FHIR validation layer unavailable: %s", type(exc).__name__)

try:
    from einvoice.validate import is_available as _schematron_ready, validate_xml as schematron_validate
except Exception as exc:                                    # saxonche not installed
    schematron_validate = _schematron_ready = None
    log.warning("EN 16931 validation layer unavailable: %s", type(exc).__name__)

# Saxon keeps its compiled rule set per thread, and compiling takes a moment. One dedicated thread means it is
# compiled once, at start-up, and every invoice reuses it.
_saxon = ThreadPoolExecutor(max_workers=1, thread_name_prefix="saxon")
_schematron_ok: bool | None = None


async def _schematron_available() -> bool:
    global _schematron_ok
    if _schematron_ok is None:
        if schematron_validate is None or _schematron_ready is None:
            _schematron_ok = False
        else:
            _schematron_ok = bool(await asyncio.get_running_loop().run_in_executor(_saxon, _schematron_ready))
    return _schematron_ok


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_schematron_available())     # warm the rule set without delaying start-up
    try:
        yield
    finally:
        task.cancel()
        _saxon.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------- samples

def _load_samples() -> dict:
    manifest = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))
    folders = {"hl7": SAMPLES / "hl7", "invoice": SAMPLES / "invoices"}
    out: dict[str, list[dict]] = {}
    for kind, entries in manifest.items():
        out[kind] = [{"id": e["file"], "label": e["label"], "note": e["note"], "expect": e["expect"],
                      "text": (folders[kind] / e["file"]).read_text(encoding="utf-8")} for e in entries]
    return out


SAMPLE_DATA = _load_samples()

# ---------------------------------------------------------------- plumbing

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                               "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
}


class SecurityHeaders:
    """Plain ASGI middleware: add the headers above to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for k, v in SECURITY_HEADERS.items():
                    if k.lower().encode() not in present:
                        headers.append((k.lower().encode(), v.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


_hits: dict[str, deque] = defaultdict(deque)


def _client_id(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")          # Render puts the real address first
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _rate_limited(request: Request) -> bool:
    now = time.monotonic()
    if len(_hits) > 5000:
        _hits.clear()
    q = _hits[_client_id(request)]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _read_field(request: Request, field: str) -> tuple[str | None, JSONResponse | None]:
    """Read {"<field>": "<text>"} from the body with a size cap. Returns (text, None) or (None, error response)."""
    if _rate_limited(request):
        return None, _error(429, "Too many conversions from this connection. Wait a minute and try again.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None, _error(413, f"That is too large for the demo. The limit is {MAX_BODY_BYTES // 1024} KB.")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return None, _error(413, f"That is too large for the demo. The limit is {MAX_BODY_BYTES // 1024} KB.")
    try:
        data = json.loads(body)
    except ValueError:
        return None, _error(400, "The request body must be JSON.")
    value = data.get(field) if isinstance(data, dict) else None
    if not isinstance(value, str):
        return None, _error(400, f'The request body must be a JSON object with a text field "{field}".')
    return value, None


# ---------------------------------------------------------------- routes

async def index(request: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def samples(request: Request):
    return JSONResponse(SAMPLE_DATA, headers={"Cache-Control": "public, max-age=300"})


async def convert_hl7(request: Request):
    text, err = await _read_field(request, "message")
    if err:
        return err
    result = await run_in_threadpool(demo.run_hl7, text, fhir_validate, fhir_errors)
    return JSONResponse(result)


async def convert_invoice(request: Request):
    text, err = await _read_field(request, "invoice")
    if err:
        return err
    validator = schematron_validate if await _schematron_available() else None
    result = await asyncio.get_running_loop().run_in_executor(_saxon, demo.run_invoice, text, validator)
    return JSONResponse(result)


async def healthz(request: Request):
    # Must answer instantly: hosting health checks time out in seconds, and the EN 16931 rule set is compiled in the
    # background at start-up. null means "still warming up"; poll again in a few seconds.
    return JSONResponse({"status": "ok", "fhir_validation": fhir_validate is not None,
                         "en16931_validation": _schematron_ok})


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/samples", samples),
        Route("/api/hl7", convert_hl7, methods=["POST"]),
        Route("/api/invoice", convert_invoice, methods=["POST"]),
        Route("/healthz", healthz),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ],
    lifespan=lifespan,
)
app.add_middleware(SecurityHeaders)
