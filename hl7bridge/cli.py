"""Command line: convert one file, run the MLLP receiver, push a bundle, replay the dead-letter store."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from . import hl7v2
from .fhirjson import dumps
from .mapper import MapConfig, MappingError, convert
from .mllp import MLLPServer
from .pipeline import Pipeline
from .sinks import DeadLetter, FhirSink, FileSink
from .validate import errors, validate_bundle


def _cfg(args) -> MapConfig:
    system_map = {}
    if getattr(args, "system_map", None):
        with open(args.system_map, encoding="utf-8") as f:
            system_map = json.load(f)
    return MapConfig(default_tz=args.default_tz, system_map=system_map,
                     accept_processing_ids=tuple(args.accept_processing.split(",")))


def _sink(args):
    if args.fhir_base:
        return FhirSink(args.fhir_base, token=os.environ.get("FHIR_TOKEN", ""))
    return FileSink(args.outbox)


def cmd_convert(args) -> int:
    with open(args.file, "rb") as f:
        raw = f.read()
    try:
        conv = convert(hl7v2.parse(raw), _cfg(args))
    except (hl7v2.HL7Error, MappingError) as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 1
    issues = validate_bundle(conv.bundle)
    for w in conv.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for i in issues:
        print(str(i), file=sys.stderr)
    out = dumps(conv.bundle)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    else:
        print(out)
    print(f"resources: {conv.counts}; validation errors: {len(errors(issues))}", file=sys.stderr)
    return 1 if errors(issues) else 0


def cmd_serve(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pipe = Pipeline(_sink(args), DeadLetter(args.dead_letter), _cfg(args))
    server = MLLPServer(pipe, args.host, args.port)
    print(f"MLLP receiver on {args.host}:{args.port}; sink: {'FHIR ' + args.fhir_base if args.fhir_base else 'files in ' + args.outbox}", file=sys.stderr)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_push(args) -> int:
    with open(args.bundle, encoding="utf-8") as f:
        bundle = json.load(f)
    res = FhirSink(args.fhir_base, token=os.environ.get("FHIR_TOKEN", "")).send(bundle)
    print("OK" if res.ok else f"FAILED ({'retryable' if res.retryable else 'permanent'}): {res.detail}")
    return 0 if res.ok else 1


def cmd_replay(args) -> int:
    pipe = Pipeline(_sink(args), DeadLetter(args.dead_letter), _cfg(args))
    print(json.dumps(pipe.replay()))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hl7bridge", description="HL7 v2 ORU^R01 to FHIR R4. Use synthetic data only.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, serve=False):
        p.add_argument("--system-map", help="JSON file mapping assigning authority names to identifier system URIs")
        p.add_argument("--default-tz", default="+0000", help="offset applied to timestamps that carry none (default +0000)")
        p.add_argument("--accept-processing", default="P", help="comma list of MSH-11 values to accept (default P)")
        if serve:
            p.add_argument("--fhir-base", help="FHIR server base URL; omit to write bundles to --outbox")
            p.add_argument("--outbox", default="outbox")
            p.add_argument("--dead-letter", default="dead-letter")

    p = sub.add_parser("convert", help="convert one HL7 file and validate the result")
    p.add_argument("file")
    p.add_argument("--out")
    common(p)
    p.set_defaults(fn=cmd_convert)

    p = sub.add_parser("serve", help="run the MLLP receiver")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=2575)
    common(p, serve=True)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("push", help="POST a bundle file to a FHIR server (token from FHIR_TOKEN)")
    p.add_argument("bundle")
    p.add_argument("--fhir-base", required=True)
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("replay", help="re-process the dead-letter store")
    common(p, serve=True)
    p.set_defaults(fn=cmd_replay)

    args = ap.parse_args(argv)
    return args.fn(args)
