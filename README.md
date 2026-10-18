# Interoperability live demo

One small web app that lets anyone try two projects in a browser, with no install:

- **HL7 v2 to FHIR bridge**: paste a hospital lab message (HL7 v2 ORU^R01), see it parsed, mapped to a FHIR R4 Bundle, checked, and the acknowledgement (ACK) the sender would get back.
- **Peppol e-invoice**: paste an invoice as JSON, see it mapped to a Peppol BIS 3.0 (EN 16931, UBL 2.1) XML invoice and checked against the official rules.

Each result shows a **stage rail** (Parse, Map, Validate, Deliver) so a visitor sees exactly where a message passed or was rejected. Delivery is always "Not sent in this demo".

Use only synthetic data. Nothing is stored and message contents are never logged, but this is a public page.

## Run it locally

```
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Check http://127.0.0.1:8000/healthz:

```
{"status":"ok","fhir_validation":true,"en16931_validation":true}
```

`en16931_validation` is `null` for a few seconds after start while the official rules compile, then `true`. If either is `false`, the page still works but says honestly that the check was not run ("Converted, not validated"). Fix the install before sharing the link.

## Tests

```
python -m unittest discover -s tests -t .
```

Tests that need `fhir.resources` or `saxonche` are skipped if those are missing. Run them with the full `requirements.txt` installed before you publish.

## Deploy on Render (free)

1. Push this folder to a GitHub repository (the folder contents at the repo root).
2. In Render: **New > Blueprint** (or **New > Web Service**), pick the repo. `render.yaml` and the `Dockerfile` already describe the service.
3. Wait for the build, then open `https://<your-service>.onrender.com/healthz` and confirm both validation flags are `true`.
4. Open the site and try every sample, both tabs.
5. Edit `app/static/config.json`: fill in your name and the links to your two GitHub repositories (empty links are hidden). Commit and push.
6. Put the URL on your CV, LinkedIn and GitHub profile.

### Things to know about the free tier

- **Cold start.** The service sleeps when idle. The first visit after a quiet period can take 30 to 60 seconds. Open the link yourself shortly before an interview or application round.
- **Memory.** Free instances have 512 MB. The official-rules engine (Saxon) is the heaviest part. If the service restarts or logs show it was killed for memory, `en16931_validation` will stay `false` or the service will restart repeatedly. Fixes: upgrade the instance size, or deploy elsewhere with more memory.
- **Rate limit.** 40 conversions per client per minute by default. Change it with the `DEMO_RATE_LIMIT` environment variable. Inputs over 64 KB are refused.

## Layout

```
app/            web layer (main.py), pure logic (demo.py), page (static/)
hl7bridge/      copy of the HL7 to FHIR project
einvoice/       copy of the Peppol e-invoice project
vendor/en16931/ official CEN/TC 434 Schematron rules
samples/        example messages and invoices, with manifest.json
tests/          logic tests and web tests
```

`hl7bridge/` and `einvoice/` are copies. When you change either original project, copy the package folder over again (and `vendor/en16931/` if the rules change), then re-run the tests.

## Honest limits

- It demonstrates the conversion and validation, not live delivery to a hospital system or the Peppol network.
- FHIR checking is the `fhir.resources` structural layer plus the project's own rules. The full HAPI validator is not run.
- Invoice checking uses the official EN 16931 / Peppol Schematron rules. It is not a certified Peppol access point.
