"""Command line: map + validate one invoice, send to a sandbox, replay the dead-letter store."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .mapper import MapConfig, MappingError, convert
from .pipeline import Pipeline
from .sinks import DeadLetter, FileSink, PeppolSandboxSink
from .validate import errors, is_available, validate_xml


def cmd_convert(args) -> int:
    with open(args.file, encoding="utf-8") as f:
        invoice = json.load(f)
    try:
        xml = convert(invoice, MapConfig())
    except MappingError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 1
    failed = False
    if not args.skip_validate:
        if not is_available():
            print("warning: EN 16931 validator (Saxon/saxonche) is not available; skipping", file=sys.stderr)
        else:
            issues = validate_xml(xml)
            for i in issues:
                print(str(i), file=sys.stderr)
            if errors(issues):
                print(f"validation errors: {len(errors(issues))}", file=sys.stderr)
                failed = True
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(xml)
    else:
        print(xml)
    return 1 if failed else 0       # a document that fails the official rules must not look like a success to a script or CI


def cmd_send(args) -> int:
    with open(args.file, encoding="utf-8") as f:
        invoice = json.load(f)
    headers = json.loads(args.headers) if args.headers else {}
    sink = PeppolSandboxSink(args.sandbox_url, headers=headers) if args.sandbox_url else FileSink(args.outbox)
    pipe = Pipeline(sink, DeadLetter(args.dead_letter))
    outcome = pipe.process(invoice)
    if outcome.ok:
        print(f"OK: {outcome.invoice_id}")
        return 0
    print(f"FAILED at {outcome.stage}: {outcome.reason}", file=sys.stderr)
    for i in outcome.issues:
        print(f"  {i}", file=sys.stderr)
    return 1


def cmd_replay(args) -> int:
    sink = PeppolSandboxSink(args.sandbox_url, headers=json.loads(args.headers) if args.headers else {}) if args.sandbox_url else FileSink(args.outbox)
    pipe = Pipeline(sink, DeadLetter(args.dead_letter))
    print(json.dumps(pipe.replay()))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(prog="einvoice", description="ERP invoice -> UBL/EN16931. Use synthetic data only.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("convert", help="convert one JSON invoice to UBL XML and validate it")
    p.add_argument("file")
    p.add_argument("--out")
    p.add_argument("--skip-validate", action="store_true")
    p.set_defaults(fn=cmd_convert)

    p = sub.add_parser("send", help="convert, validate, and deliver (to a file outbox or a sandbox URL)")
    p.add_argument("file")
    p.add_argument("--sandbox-url", help="Peppol test-network sandbox send URL; omit to write to --outbox instead")
    p.add_argument("--headers", help="JSON object of extra headers for --sandbox-url (e.g. an API key)")
    p.add_argument("--outbox", default="outbox")
    p.add_argument("--dead-letter", default="dead-letter")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("replay", help="re-validate and resend everything in the dead-letter store")
    p.add_argument("--sandbox-url")
    p.add_argument("--headers")
    p.add_argument("--outbox", default="outbox")
    p.add_argument("--dead-letter", default="dead-letter")
    p.set_defaults(fn=cmd_replay)

    args = ap.parse_args(argv)
    return args.fn(args)
