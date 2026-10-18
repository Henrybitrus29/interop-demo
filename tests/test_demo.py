"""The stage-by-stage logic behind the demo, run against every bundled sample and against injected validators."""
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from app import demo
from app.main import SAMPLE_DATA, fhir_errors, fhir_validate, schematron_validate

ROOT = Path(__file__).resolve().parent.parent


def states(result):
    return {s["id"]: s["state"] for s in result["rail"]}


class _Issue:
    def __init__(self, severity, where, message, rule_id=""):
        self.severity, self.where, self.message, self.rule_id = severity, where, message, rule_id


def sample(kind, name):
    return next(s for s in SAMPLE_DATA[kind] if s["id"] == name)["text"]


class ManifestTests(unittest.TestCase):
    def test_every_sample_file_is_listed_and_every_listing_exists(self):
        for kind, folder in (("hl7", "hl7"), ("invoice", "invoices")):
            on_disk = {p.name for p in (ROOT / "samples" / folder).iterdir()}
            listed = {s["id"] for s in SAMPLE_DATA[kind]}
            self.assertEqual(on_disk, listed, kind)


class Hl7SampleTests(unittest.TestCase):
    def test_each_sample_ends_where_its_label_says(self):
        for s in SAMPLE_DATA["hl7"]:
            with self.subTest(s["id"]):
                r = demo.run_hl7(s["text"], fhir_validate, fhir_errors)
                if s["id"] == "bad_future_result.hl7" and fhir_validate is None:
                    continue        # rejected by the FHIR validation layer, which is not installed here
                self.assertEqual(r["ok"], s["expect"] == "accepted", r["reason"])

    def test_good_message_shows_every_stage_and_never_claims_delivery(self):
        r = demo.run_hl7(sample("hl7", "oru_bmp.hl7"), None, None)
        self.assertEqual(states(r), {"parse": "pass", "map": "pass", "validate": "skipped", "deliver": "skipped"})
        self.assertEqual(r["ack_code"], "AA")
        self.assertFalse(r["validated"])                      # never claim validation that did not happen
        deliver = next(x for x in r["rail"] if x["id"] == "deliver")
        self.assertEqual(deliver["note"], "Not sent in this demo")

    def test_failure_marks_later_stages_not_reached(self):
        r = demo.run_hl7(sample("hl7", "bad_no_patient_id.hl7"))
        self.assertEqual(states(r), {"parse": "pass", "map": "fail", "validate": "notreached", "deliver": "notreached"})
        self.assertEqual(r["ack_code"], "AR")
        self.assertIn("MSA|AR|", r["outputs"]["ack"])
        self.assertEqual(r["outputs"]["bundle"], "")

    def test_unreadable_input_stops_at_parse(self):
        r = demo.run_hl7("this is not an HL7 message")
        self.assertEqual(states(r)["parse"], "fail")
        self.assertEqual(r["ack_code"], "AR")

    def test_extracted_results_keep_values_and_flags(self):
        r = demo.run_hl7(sample("hl7", "oru_bmp.hl7"))
        rows = {o["test"]: o for o in r["summary"]["observations"]}
        self.assertEqual(rows["Potassium"]["result"], "5.9 mmol/L")
        self.assertEqual(rows["Potassium"]["flag"], "High (H)")
        self.assertEqual(rows["Creatinine"]["result"], "1.40 mg/dL")        # trailing zero preserved
        self.assertEqual(r["summary"]["counts"]["Observation"], 7)

    def test_windows_and_unix_line_endings_both_parse(self):
        text = sample("hl7", "oru_bmp.hl7")
        for sep in ("\n", "\r\n", "\r"):
            with self.subTest(repr(sep)):
                r = demo.run_hl7(sep.join(text.splitlines()))
                self.assertTrue(r["ok"], r["reason"])

    def test_bundle_output_keeps_decimal_precision(self):
        r = demo.run_hl7(sample("hl7", "oru_bmp.hl7"))
        self.assertIn('"value": 1.40', r["outputs"]["bundle"])


class Hl7InjectedValidatorTests(unittest.TestCase):
    def good(self):
        return sample("hl7", "oru_bmp.hl7")

    def test_validator_errors_reject_at_validate_and_keep_the_bundle_visible(self):
        fake = lambda bundle: [_Issue("error", "entry[1]", "something is wrong")]
        r = demo.run_hl7(self.good(), fake, lambda issues: [i for i in issues if i.severity == "error"])
        self.assertFalse(r["ok"])
        self.assertEqual(states(r)["validate"], "fail")
        self.assertEqual(states(r)["deliver"], "notreached")
        self.assertEqual(r["ack_code"], "AR")
        self.assertTrue(r["outputs"]["bundle"])
        self.assertIn("1 error", r["reason"])
        self.assertTrue(any(i["source"] == "validator" and i["where"] == "entry[1]" for i in r["issues"]))

    def test_clean_validation_marks_the_result_validated(self):
        r = demo.run_hl7(self.good(), lambda b: [], lambda i: [])
        self.assertTrue(r["ok"] and r["validated"])
        self.assertEqual(states(r)["validate"], "pass")

    def test_a_crashing_validator_is_reported_not_hidden(self):
        def boom(bundle):
            raise RuntimeError("secret internal detail")
        r = demo.run_hl7(self.good(), boom, lambda i: [])
        self.assertTrue(r["ok"])
        self.assertFalse(r["validated"])
        self.assertEqual(states(r)["validate"], "skipped")
        self.assertNotIn("secret internal detail", json.dumps(r))

    def test_unexpected_mapper_crash_becomes_a_clean_rejection(self):
        with mock.patch.object(demo.hl7_mapper, "convert", side_effect=RuntimeError("secret internal detail")):
            r = demo.run_hl7(self.good())
        self.assertFalse(r["ok"])
        self.assertEqual(states(r)["map"], "fail")
        self.assertIn("unexpected error", r["reason"])
        self.assertNotIn("secret internal detail", json.dumps(r))


@unittest.skipIf(fhir_validate is None, "FHIR validation layer (fhir.resources) is not installed")
class Hl7RealValidatorTests(unittest.TestCase):
    def test_future_dated_result_is_rejected_at_validate(self):
        r = demo.run_hl7(sample("hl7", "bad_future_result.hl7"), fhir_validate, fhir_errors)
        self.assertFalse(r["ok"])
        self.assertEqual(states(r)["validate"], "fail")
        self.assertTrue(any("future" in i["message"] for i in r["issues"]))

    def test_good_samples_validate_clean(self):
        for name in ("oru_bmp.hl7", "oru_cbc_corrected.hl7", "oru_culture_preliminary.hl7"):
            with self.subTest(name):
                r = demo.run_hl7(sample("hl7", name), fhir_validate, fhir_errors)
                self.assertTrue(r["ok"] and r["validated"], r["issues"])


class InvoiceTests(unittest.TestCase):
    def test_each_sample_ends_where_its_label_says(self):
        for s in SAMPLE_DATA["invoice"]:
            with self.subTest(s["id"]):
                r = demo.run_invoice(s["text"], schematron_validate)
                self.assertEqual(r["ok"], s["expect"] == "accepted", r["reason"] or r["issues"])

    def test_rejection_at_map_produces_no_xml(self):
        r = demo.run_invoice(sample("invoice", "bad_missing_seller.json"))
        self.assertEqual(states(r), {"map": "fail", "validate": "notreached", "deliver": "notreached"})
        self.assertEqual(r["outputs"]["xml"], "")
        self.assertIn("seller", r["reason"])

    def test_not_json_is_explained(self):
        r = demo.run_invoice("{ nope")
        self.assertEqual(states(r)["map"], "fail")
        self.assertIn("valid JSON", r["reason"])
        self.assertFalse(demo.run_invoice("[1, 2]")["ok"])

    def test_sub_cent_price_keeps_its_precision_in_the_summary(self):
        r = demo.run_invoice(sample("invoice", "electricity_metered.json"))
        line = r["summary"]["lines"][0]
        self.assertEqual((line["unit_price"], line["net"], line["unit"]), ("0.0125", "12.50", "KWH"))
        self.assertEqual(r["summary"]["totals"]["payable"], "15.13")

    def test_credit_note_is_labelled_as_one(self):
        r = demo.run_invoice(sample("invoice", "credit_note.json"))
        self.assertEqual(r["summary"]["document"], "Credit note")

    def test_validator_errors_reject_at_validate_but_keep_the_xml_visible(self):
        fake = lambda xml: [_Issue("error", "", "totals do not add up", rule_id="BR-CO-15")]
        r = demo.run_invoice(sample("invoice", "simple_invoice.json"), fake)
        self.assertFalse(r["ok"])
        self.assertEqual(states(r), {"map": "pass", "validate": "fail", "deliver": "notreached"})
        self.assertTrue(r["outputs"]["xml"])
        self.assertEqual(r["issues"][0]["where"], "BR-CO-15")

    def test_warnings_do_not_reject(self):
        fake = lambda xml: [_Issue("warning", "", "just a warning", rule_id="PEPPOL-X")]
        r = demo.run_invoice(sample("invoice", "simple_invoice.json"), fake)
        self.assertTrue(r["ok"] and r["validated"])
        self.assertEqual(len(r["issues"]), 1)

    def test_missing_validator_never_claims_validity(self):
        r = demo.run_invoice(sample("invoice", "simple_invoice.json"), None)
        self.assertTrue(r["ok"])
        self.assertFalse(r["validated"])
        self.assertEqual(states(r)["validate"], "skipped")


class PrettyXmlTests(unittest.TestCase):
    def test_indenting_changes_layout_only(self):
        for s in SAMPLE_DATA["invoice"]:
            if s["expect"] != "accepted":
                continue
            with self.subTest(s["id"]):
                from einvoice.mapper import convert
                original = convert(json.loads(s["text"]))
                pretty = demo.pretty_xml(original)

                def flat(xml):
                    return [(e.tag, (e.text or "").strip(), sorted(e.attrib.items())) for e in ET.fromstring(xml.encode()).iter()]
                self.assertEqual(flat(original), flat(pretty))
                self.assertGreater(pretty.count("\n"), 20)


if __name__ == "__main__":
    unittest.main()
