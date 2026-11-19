"""Offline regression tests; no API key or external services required."""

import io
import logging
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from google.genai import errors
from keyring.errors import KeyringError
from openpyxl import load_workbook
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject, DictionaryObject, NameObject,
)
from streamlit.testing.v1 import AppTest

from app import build_export_frames, excel_export, input_fingerprint, spreadsheet_safe
from app_logging import (
    REQUEST_ID, DateBasedFileHandler, RequestIdFilter, logger, request_log_context,
)
from evaluator import (
    EvaluationError, _gemini_response_schema, evaluate_resume, extract_text_from_pdf,
)
from gemini_credentials import KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, get_gemini_api_key
from schema import CandidateEvaluation


def setUpModule():
    application_handlers = patch.object(logger, "handlers", [logging.NullHandler()])
    application_handlers.start()
    unittest.addModuleCleanup(application_handlers.stop)


def make_evaluation(**changes: object) -> CandidateEvaluation:
    payload = {
        "candidate_name": "Alex Morgan",
        "overall_fit_score": 80,
        "decision": "Shortlist",
        "executive_summary": "Backend engineer with Python delivery evidence. Strong core fit.",
        "matched_skills": ["Python", "SQL"],
        "missing_critical_skills": ["Production React experience is unverified"],
        "transferable_skills": ["Vue component development"],
        "experience_verdict": "Senior backend responsibilities evidenced; frontend depth unverified.",
        "tailored_interview_questions": [
            "How would you isolate and test a React component's API dependencies?",
            "How would you diagnose a slow SQL query in production?",
            "How would you make a retried API mutation idempotent?",
        ],
    }
    payload.update(changes)
    return CandidateEvaluation.model_validate(payload)


def make_pdf(text: str = "Alex Morgan Python SQL engineer") -> bytes:
    """Build a real selectable-text PDF using the existing PDF dependency."""
    writer = PdfWriter()
    for line in (text, "Relevant backend delivery experience"):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        })
        stream = DecodedStreamObject()
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class UploadedPDF(io.BytesIO):
    def __init__(self, name: str, data: bytes):
        super().__init__(data)
        self.name = name
        self.size = len(data)


class LoggingTests(unittest.TestCase):
    def test_concurrent_requests_keep_separate_ids(self):
        barrier = Barrier(2)

        def overlapping_request():
            with request_log_context() as request_id:
                barrier.wait(timeout=5)
                self.assertEqual(REQUEST_ID.get(), request_id)
                return request_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(overlapping_request) for _ in range(2)]
            request_ids = [request.result(timeout=10) for request in requests]
        self.assertNotEqual(*request_ids)
        self.assertEqual(REQUEST_ID.get(), "-")

    def test_request_ids_and_delimiters_are_scoped(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(logging.Formatter("%(request_id)s %(message)s"))
        logger.addHandler(handler)
        try:
            with request_log_context() as first_id:
                self.assertEqual(str(UUID(first_id)), first_id)
                logger.info("first batch")
                with request_log_context() as second_id:
                    self.assertNotEqual(first_id, second_id)
                    logger.warning("second batch")
                self.assertEqual(REQUEST_ID.get(), first_id)
            self.assertEqual(REQUEST_ID.get(), "-")
            with self.assertRaises(RuntimeError):
                with request_log_context() as failed_id:
                    raise RuntimeError("synthetic failure")
            self.assertEqual(REQUEST_ID.get(), "-")
        finally:
            logger.removeHandler(handler)
            handler.close()

        lines = output.getvalue().splitlines()
        self.assertIn(f"{first_id} first batch", lines)
        self.assertIn(f"{second_id} second batch", lines)
        for request_id in (first_id, second_id, failed_id):
            self.assertEqual(sum(
                line.startswith(request_id) and "REQUEST START" in line for line in lines
            ), 1)
        self.assertFalse(any("REQUEST END" in line for line in lines))

    def test_writes_levels_to_date_named_files(self):
        dates = iter((date(2026, 11, 9), date(2026, 11, 9), date(2026, 11, 10)))
        with tempfile.TemporaryDirectory() as directory:
            handler = DateBasedFileHandler(Path(directory), clock=lambda: next(dates))
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            test_logger = logging.getLogger(f"resume_screener_test_{id(self)}")
            test_logger.setLevel(logging.INFO)
            test_logger.addHandler(handler)
            try:
                test_logger.info("batch started")
                test_logger.warning("candidate skipped")
                test_logger.error("provider request failed")
            finally:
                test_logger.removeHandler(handler)
                handler.close()

            first_day = Path(directory) / "resume-screener-2026-11-09.log"
            next_day = Path(directory) / "resume-screener-2026-11-10.log"
            self.assertIn("INFO batch started", first_day.read_text(encoding="utf-8"))
            self.assertIn("WARNING candidate skipped", first_day.read_text(encoding="utf-8"))
            self.assertIn("ERROR provider request failed", next_day.read_text(encoding="utf-8"))


class CredentialTests(unittest.TestCase):
    def test_environment_key_takes_precedence(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "environment-test"}),
            patch("gemini_credentials.keyring.get_password") as get_password,
        ):
            self.assertEqual(get_gemini_api_key(), "environment-test")
            get_password.assert_not_called()

    def test_keychain_key_is_used_without_environment_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gemini_credentials.keyring.get_password", return_value="keychain-test") as get_password,
        ):
            self.assertEqual(get_gemini_api_key(), "keychain-test")
            get_password.assert_called_once_with(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)

    def test_unavailable_keychain_returns_no_credential(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gemini_credentials.keyring.get_password", side_effect=KeyringError("backend unavailable")),
        ):
            self.assertIsNone(get_gemini_api_key())


class SchemaTests(unittest.TestCase):
    def test_decision_boundaries(self):
        for score, decision in (
            (0, "Reject"), (54, "Reject"), (55, "Review"),
            (74, "Review"), (75, "Shortlist"), (100, "Shortlist"),
        ):
            with self.subTest(score=score):
                self.assertEqual(make_evaluation(overall_fit_score=score, decision=decision).decision, decision)

    def test_invalid_scores_and_decisions(self):
        for change in (
            {"overall_fit_score": -1}, {"overall_fit_score": 101},
            {"overall_fit_score": "80"}, {"overall_fit_score": True},
            {"decision": "Review"}, {"decision": "Hire"},
            {"candidate_name": " "}, {"unexpected": "field"},
            {"matched_skills": [" "]}, {"tailored_interview_questions": ["One?", "Two?"]},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                make_evaluation(**change)


class PDFTests(unittest.TestCase):
    def test_extracts_all_pages_and_restores_position(self):
        upload = UploadedPDF("alex.pdf", make_pdf())
        upload.seek(7)
        text = extract_text_from_pdf(upload)
        self.assertIn("Alex Morgan", text)
        self.assertIn("Relevant backend delivery", text)
        self.assertEqual(upload.tell(), 7)
        self.assertNotIn("\n", text)

    def test_empty_corrupt_and_blank(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        blank = io.BytesIO()
        writer.write(blank)
        for data in (b"", b"%PDF-1.4 corrupt", blank.getvalue()):
            self.assertEqual(extract_text_from_pdf(data), "")

    def test_encrypted_pdf_is_skipped(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("not-a-real-password")
        output = io.BytesIO()
        writer.write(output)
        self.assertEqual(extract_text_from_pdf(output.getvalue()), "")

    def test_resource_limits(self):
        data = make_pdf()
        with patch("evaluator.MAX_PDF_BYTES", 10):
            self.assertEqual(extract_text_from_pdf(data), "")
        with patch("evaluator.MAX_PDF_PAGES", 1):
            self.assertEqual(extract_text_from_pdf(data), "")
        with patch("evaluator.MAX_RESUME_CHARACTERS", 5):
            self.assertEqual(extract_text_from_pdf(data), "")


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("evaluator.genai.Client")
        self.factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = MagicMock()
        self.factory.return_value = self.client
        self.client.__enter__.return_value = self.client
        self.evaluation = make_evaluation()

    def evaluate(self):
        return evaluate_resume("Resume evidence", "Job requirements", "alex.pdf", api_key="test-only")

    def test_parsed_response_and_sdk_config(self):
        self.client.models.generate_content.return_value = SimpleNamespace(parsed=self.evaluation, text=None)
        self.assertEqual(self.evaluate(), self.evaluation)
        arguments = self.client.models.generate_content.call_args.kwargs
        self.assertEqual(arguments["model"], "gemini-2.5-flash")
        self.assertEqual(arguments["config"].response_mime_type, "application/json")
        self.assertNotIn("additionalProperties", arguments["config"].response_schema)
        self.assertEqual(set(arguments["config"].response_schema["required"]), set(CandidateEvaluation.model_fields))
        self.assertNotIn("additionalProperties", _gemini_response_schema())
        self.assertEqual(arguments["config"].temperature, 0.1)
        self.assertIn('"candidate_filename": "alex.pdf"', arguments["contents"])

    def test_json_fallback(self):
        self.client.models.generate_content.return_value = SimpleNamespace(
            parsed=None, text=self.evaluation.model_dump_json(),
        )
        self.assertEqual(self.evaluate(), self.evaluation)

    def test_invalid_and_empty_responses(self):
        for text in (None, "not-json", '{"private": "data"}'):
            self.client.models.generate_content.return_value = SimpleNamespace(parsed=None, text=text)
            with self.assertRaises(EvaluationError) as caught:
                self.evaluate()
            self.assertNotIn("private", str(caught.exception))

    def test_provider_error_is_sanitized(self):
        self.client.models.generate_content.side_effect = errors.ClientError(
            429, {"error": {"message": "private provider data", "status": "RESOURCE_EXHAUSTED"}},
        )
        with self.assertRaisesRegex(EvaluationError, "quota or rate limit") as caught:
            self.evaluate()
        self.assertNotIn("private", str(caught.exception))

    def test_bad_request_error_is_actionable_and_sanitized(self):
        self.client.models.generate_content.side_effect = errors.ClientError(
            400, {"error": {"message": "private provider data", "status": "INVALID_ARGUMENT"}},
        )
        with self.assertRaisesRegex(EvaluationError, "request settings") as caught:
            self.evaluate()
        self.assertNotIn("private", str(caught.exception))

    def test_missing_key_and_empty_input_do_not_call_api(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("evaluator.get_gemini_api_key", return_value=None),
        ):
            with self.assertRaises(EvaluationError):
                evaluate_resume("Resume", "Job", "alex.pdf")
        with self.assertRaises(EvaluationError):
            evaluate_resume("", "Job", "alex.pdf", api_key="test-only")
        self.factory.assert_not_called()

    def test_environment_key_and_session_isolation(self):
        self.client.models.generate_content.return_value = SimpleNamespace(parsed=self.evaluation, text=None)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "environment-test"}):
            evaluate_resume("Resume", "Job", "alex.pdf")
            self.assertEqual(self.factory.call_args.kwargs["api_key"], "environment-test")
            self.evaluate()
            self.assertEqual(os.environ["GEMINI_API_KEY"], "environment-test")
            self.assertEqual(self.factory.call_args.kwargs["api_key"], "test-only")


class ExportTests(unittest.TestCase):
    def test_sorting_and_all_summary_fields(self):
        leaderboard, summary = build_export_frames([
            make_evaluation(overall_fit_score=60, decision="Review"),
            make_evaluation(candidate_name="Higher Score"),
        ], ["review.pdf", "higher.pdf"])
        self.assertEqual(leaderboard.iloc[0]["Candidate Name"], "Higher Score")
        self.assertEqual(summary.iloc[0]["Source PDF"], "higher.pdf")
        self.assertIn("idempotent", summary.iloc[0]["Interview Questions"])

    def test_excel_cells_and_csv_are_formula_safe(self):
        for text in ("=1+1", "  =1+1", "+cmd", "-cmd", "@SUM(A1)", "\tcmd", "\r=1"):
            self.assertTrue(str(spreadsheet_safe(text)).startswith("'"))
        candidate = make_evaluation(candidate_name="=1+1", executive_summary="Some\x00text.")
        leaderboard, summary = build_export_frames([candidate], ["@resume.pdf"])
        workbook = load_workbook(io.BytesIO(excel_export(leaderboard, summary, {"JD": "=1+1"})))
        self.assertEqual(workbook.sheetnames, ["Leaderboard", "Full Scorecards", "Run Metadata"])
        self.assertEqual(workbook["Leaderboard"]["A2"].data_type, "s")
        self.assertNotIn("\x00", workbook["Full Scorecards"]["E2"].value)
        csv = summary.map(spreadsheet_safe).to_csv(index=False)
        self.assertIn("'=1+1", csv)

    def test_fingerprint_detects_same_name_content_changes(self):
        original = input_fingerprint("Job", [UploadedPDF("a.pdf", b"one")])
        self.assertNotEqual(original, input_fingerprint("Job", [UploadedPDF("a.pdf", b"two")]))
        self.assertNotEqual(original, input_fingerprint("Other job", [UploadedPDF("a.pdf", b"one")]))


class InterfaceTests(unittest.TestCase):
    def test_presets_custom_edits_and_disabled_action(self):
        app = AppTest.from_file("app.py").run(timeout=20)
        self.assertFalse(app.exception)
        self.assertTrue(next(button for button in app.button if "Screen Resumes" in button.label).disabled)
        app.selectbox[0].select("Full Stack Python Developer").run()
        self.assertIn("FastAPI", app.text_area[0].value)
        for preset, required_text in (
            ("UI Developer (3+ years)", "3+ years"),
            ("DevOps Engineer (5+ years)", "5+ years"),
            ("Java Developer (3+ years)", "3+ years"),
        ):
            app.selectbox[0].select(preset).run()
            self.assertIn(required_text, app.text_area[0].value)
        app.selectbox[0].select("DevOps Engineer (5+ years)").run()
        self.assertIn("Terraform", app.text_area[0].value)
        app.selectbox[0].select("Java Developer (3+ years)").run()
        self.assertIn("Spring Boot", app.text_area[0].value)
        app.selectbox[0].select("Custom JD").run()
        self.assertEqual(app.text_area[0].value, "")
        app.text_area[0].input("Custom Python role").run()
        self.assertEqual(app.text_area[0].value, "Custom Python role")

    def test_clear_results_clears_uploads_and_consent(self):
        app = AppTest.from_file("app.py").run(timeout=20)
        app.file_uploader[0].set_value([
            ("resume.pdf", make_pdf(), "application/pdf"),
        ]).run()
        app.checkbox[0].check().run()
        previous_uploader_id = app.file_uploader[0].id
        self.assertTrue(app.file_uploader[0].value)
        self.assertTrue(app.session_state.resume_consent)

        next(button for button in app.button if button.label == "Clear Results").click().run()

        self.assertNotEqual(app.file_uploader[0].id, previous_uploader_id)
        self.assertFalse(app.file_uploader[0].value)
        self.assertFalse(app.session_state.resume_consent)

    def test_batch_continues_and_renders_results(self):
        files = [
            UploadedPDF("good.pdf", make_pdf()),
            UploadedPDF("blank.pdf", b""),
            UploadedPDF("later.pdf", make_pdf()),
        ]
        with (
            patch("streamlit.file_uploader", return_value=files),
            patch("evaluator.evaluate_resume", return_value=make_evaluation()) as evaluate,
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-only"}),
        ):
            app = AppTest.from_file("app.py").run(timeout=20)
            app.checkbox[0].check().run()
            next(button for button in app.button if "Screen Resumes" in button.label).click().run(timeout=20)
            self.assertFalse(app.exception, str(app.exception))
            self.assertEqual(evaluate.call_count, 2)
            self.assertEqual(len(app.session_state.evaluations), 2)
            self.assertEqual(len(app.session_state.failures), 1)
            self.assertFalse(app.session_state.screening_in_progress)
            self.assertEqual(
                str(UUID(app.session_state.run_metadata["Request ID"])),
                app.session_state.run_metadata["Request ID"],
            )
            self.assertEqual(REQUEST_ID.get(), "-")
            self.assertEqual([metric.value for metric in app.metric], ["2", "2", "0", "0"])
            self.assertEqual(len(app.get("download_button")), 2)
            app.text_area[0].input("Changed criteria").run()
            self.assertTrue(any("Inputs have changed" in warning.value for warning in app.warning))
            next(button for button in app.button if button.label == "Clear Results").click().run()
            self.assertEqual(app.session_state.evaluations, [])
            self.assertEqual(app.session_state.run_metadata, {})


if __name__ == "__main__":
    unittest.main()