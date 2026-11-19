"""Session-isolated Streamlit workspace for human-led resume screening."""

import hashlib
import io
import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from app_logging import logger, request_log_context
from evaluator import (
    MAX_JD_CHARACTERS,
    MAX_PDF_BYTES,
    EvaluationError,
    evaluate_resume,
    extract_text_from_pdf,
)
from gemini_credentials import get_gemini_api_key
from job_descriptions import PRESET_JDS
from schema import CandidateEvaluation

MAX_BATCH_SIZE = 20
LEADERBOARD_COLUMNS = [
    "Candidate Name", "Score (%)", "Decision", "Top Matched Skills",
    "Critical Missing Skills",
]


def build_export_frames(evaluations: list[CandidateEvaluation], sources: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep the compact leaderboard and full, traceable scorecards in sync."""
    leaderboard_rows = []
    summary_rows = []
    for evaluation, source in zip(evaluations, sources, strict=True):
        leaderboard_rows.append({
            "Candidate Name": evaluation.candidate_name,
            "Score (%)": evaluation.overall_fit_score,
            "Decision": evaluation.decision,
            "Top Matched Skills": ", ".join(evaluation.matched_skills[:5]),
            "Critical Missing Skills": ", ".join(evaluation.missing_critical_skills),
        })
        summary_rows.append({
            "Candidate Name": evaluation.candidate_name,
            "Source PDF": source,
            "Score (%)": evaluation.overall_fit_score,
            "Decision": evaluation.decision,
            "Executive Summary": evaluation.executive_summary,
            "All Matched Skills": "\n".join(evaluation.matched_skills),
            "Critical Missing Skills": "\n".join(evaluation.missing_critical_skills),
            "Transferable Skills": "\n".join(evaluation.transferable_skills),
            "Experience Verdict": evaluation.experience_verdict,
            "Interview Questions": "\n".join(
                f"{number}. {question}"
                for number, question in enumerate(evaluation.tailored_interview_questions, 1)
            ),
        })
    leaderboard = pd.DataFrame(leaderboard_rows, columns=LEADERBOARD_COLUMNS)
    summary = pd.DataFrame(summary_rows)
    if not leaderboard.empty:
        leaderboard = leaderboard.sort_values("Score (%)", ascending=False, kind="stable")
        summary = summary.sort_values("Score (%)", ascending=False, kind="stable")
    return leaderboard.reset_index(drop=True), summary.reset_index(drop=True)


def spreadsheet_safe(value: object) -> object:
    """Prevent model- or filename-supplied spreadsheet formulas from executing."""
    if isinstance(value, str):
        # XML worksheets cannot represent several ASCII control characters.
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        # Spreadsheet applications may ignore leading whitespace before a formula.
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
    return value


def excel_export(
    leaderboard: pd.DataFrame, summary: pd.DataFrame, metadata: dict[str, str]
) -> bytes:
    """Build an in-memory workbook; do not persist candidate data to disk."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frames = {
            "Leaderboard": leaderboard,
            "Full Scorecards": summary,
            "Run Metadata": pd.DataFrame(list(metadata.items()), columns=["Field", "Value"]),
        }
        for sheet_name, frame in frames.items():
            frame.map(spreadsheet_safe).to_excel(writer, sheet_name=sheet_name, index=False)
            sheet = writer.sheets[sheet_name]
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                sheet.column_dimensions[column[0].column_letter].width = min(
                    65, max(18, len(str(column[0].value)) + 4)
                )
    return output.getvalue()


def input_fingerprint(jd_text: str, uploaded_files: list) -> str:
    """Detect stale results, including same-name uploads with different contents."""
    payload = {
        "jd": jd_text.strip(),
        "files": [
            (upload.name, hashlib.sha256(upload.getvalue()).hexdigest())
            for upload in uploaded_files
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def update_preset() -> None:
    """Widget callbacks run before rerender, so manual JD edits remain editable."""
    st.session_state.jd_text = PRESET_JDS.get(st.session_state.jd_preset, "")


def begin_screening() -> None:
    st.session_state.screening_in_progress = True


def render_skill_list(title: str, skills: list[str], empty_text: str) -> None:
    st.markdown(f"**{title}**")
    if skills:
        # Plain text prevents untrusted model output from becoming clickable markup.
        st.text("\n".join(f"- {skill}" for skill in skills))
    else:
        st.text(empty_text)


def render_results(current_fingerprint: str) -> None:
    evaluations = st.session_state.evaluations
    failures = st.session_state.failures
    if not st.session_state.run_metadata:
        return

    st.divider()
    st.subheader("Screening Results")
    if current_fingerprint != st.session_state.run_fingerprint:
        st.warning("Inputs have changed. The results below belong to the previous screening run.")
    st.caption(f"Run completed: {st.session_state.run_metadata['Completed (UTC)']}")
    metrics = st.columns(4)
    for column, label, value in zip(
        metrics,
        ["Total Screened", "Shortlisted", "Review", "Rejected"],
        [len(evaluations)] + [
            sum(evaluation.decision == decision for evaluation in evaluations)
            for decision in ("Shortlist", "Review", "Reject")
        ],
        strict=True,
    ):
        column.metric(label, value)

    if failures:
        st.warning(f"{len(failures)} file(s) could not be screened. No score was assigned.")
        with st.expander("Unprocessed Files", expanded=not evaluations):
            st.dataframe(pd.DataFrame(failures), hide_index=True, width="stretch")
    if not evaluations:
        return

    leaderboard, summary = build_export_frames(evaluations, st.session_state.evaluation_sources)
    st.markdown("### Candidate Leaderboard")
    st.dataframe(
        leaderboard,
        hide_index=True,
        width="stretch",
        column_config={
            "Score (%)": st.column_config.ProgressColumn(
                "Score (%)", min_value=0, max_value=100, format="%d%%"
            ),
            "Candidate Name": st.column_config.TextColumn(width="medium"),
            "Critical Missing Skills": st.column_config.TextColumn(width="large"),
        },
    )

    st.markdown("### Candidate Scorecards")
    ranked = sorted(
        zip(evaluations, st.session_state.evaluation_sources, strict=True),
        key=lambda pair: pair[0].overall_fit_score,
        reverse=True,
    )
    for evaluation, source in ranked:
        with st.expander(
            f"{evaluation.candidate_name} — Score: {evaluation.overall_fit_score}% [{evaluation.decision}]"
        ):
            st.caption(f"Source: {source}")
            st.text(evaluation.executive_summary)
            matched, missing = st.columns(2)
            with matched:
                render_skill_list("Matched Skills", evaluation.matched_skills, "None confirmed.")
            with missing:
                render_skill_list(
                    "Missing Critical Skills", evaluation.missing_critical_skills,
                    "No missing hard requirements identified.",
                )
            render_skill_list(
                "Transferable Skills", evaluation.transferable_skills,
                "No additional transferable skills identified.",
            )
            st.markdown("**Seniority & Experience**")
            st.text(evaluation.experience_verdict)
            with st.container(border=True):
                st.markdown("**Interviewer Cheat-Sheet**")
                for number, question in enumerate(evaluation.tailored_interview_questions, 1):
                    st.text(f"{number}. {question}")

    st.markdown("### Export")
    excel_column, csv_column = st.columns(2)
    # CSV includes the complete scorecard, not only the abbreviated skills columns.
    csv_bytes = summary.map(spreadsheet_safe).to_csv(index=False).encode("utf-8-sig")
    filename_stamp = st.session_state.run_metadata["Completed (UTC)"].replace(":", "-")
    excel_column.download_button(
        "Download Excel", data=excel_export(leaderboard, summary, st.session_state.run_metadata),
        file_name=f"screening-{filename_stamp}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:", width="stretch",
    )
    csv_column.download_button(
        "Download CSV", data=csv_bytes, file_name=f"screening-{filename_stamp}.csv",
        mime="text/csv", icon=":material/download:", width="stretch",
    )


def main() -> None:
    st.set_page_config(
        page_title="🎯 AI Resume-to-JD Screener & Talent Copilot",
        page_icon="📄", layout="wide",
    )
    for key, default in {
        "evaluations": [], "evaluation_sources": [], "failures": [],
        "run_metadata": {}, "run_fingerprint": "",
        "upload_widget_generation": 0, "resume_consent": False,
        "screening_in_progress": False,
        "jd_text": next(iter(PRESET_JDS.values())),
    }.items():
        st.session_state.setdefault(key, default)

    with st.sidebar:
        st.header("Screening Criteria")
        st.selectbox(
            "Job Description", [*PRESET_JDS, "Custom JD"],
            key="jd_preset", on_change=update_preset,
        )
        jd_text = st.text_area(
            "Job Description Text", key="jd_text", height=420,
            max_chars=MAX_JD_CHARACTERS,
        )
        key_available = bool(get_gemini_api_key())
        if key_available:
            st.success("Gemini API key configured")
        else:
            st.warning("Gemini API key required. Store it in macOS Keychain.")
        if st.button("Clear Results", icon=":material/delete:", width="stretch"):
            for key in ("evaluations", "evaluation_sources", "failures"):
                st.session_state[key] = []
            st.session_state.run_metadata = {}
            st.session_state.run_fingerprint = ""
            st.session_state.upload_widget_generation += 1
            st.session_state.resume_consent = False
            st.session_state.screening_in_progress = False

    st.title("🎯 AI Resume-to-JD Screener & Talent Copilot")
    st.warning(
        "Human review required. Scores and triage labels are AI-generated, may be wrong, "
        "and must not be used as automatic hiring or rejection decisions."
    )
    st.subheader("Candidate Resume")
    uploaded_files = st.file_uploader(
        "Upload PDF Resume", type=["pdf"], accept_multiple_files=True,
        help="Up to 20 text-based PDFs, 10 MB and 50 pages each. Scanned PDFs require OCR first.",
        key=f"resume_uploads_{st.session_state.upload_widget_generation}",
    )
    consent = st.checkbox(
        "I am authorized to send these resumes and the job description to Google Gemini.",
        key="resume_consent",
    )
    st.caption("Candidate data is sent to Google for evaluation. This app does not intentionally save it to disk.")
    valid_batch = 0 < len(uploaded_files) <= MAX_BATCH_SIZE
    if len(uploaded_files) > MAX_BATCH_SIZE:
        st.error(f"Upload at most {MAX_BATCH_SIZE} PDFs per batch.")
    fingerprint = input_fingerprint(jd_text, uploaded_files)
    st.button(
        "⚡ Screen Resumes", type="primary",
        disabled=(
            st.session_state.screening_in_progress
            or not (valid_batch and key_available and jd_text.strip() and consent)
        ),
        on_click=begin_screening,
    )
    if st.session_state.screening_in_progress:
        with request_log_context() as request_id:
            logger.info("Screening batch started (file_count=%d)", len(uploaded_files))
            st.session_state.evaluations = []
            st.session_state.evaluation_sources = []
            st.session_state.failures = []
            st.session_state.run_fingerprint = fingerprint
            # Capture the exact criteria, not whatever the sidebar contains on a later rerun.
            st.session_state.run_metadata = {
                "Request ID": request_id,
                "Completed (UTC)": "In progress",
                "Model": "gemini-2.5-flash",
                "Job Description": jd_text.strip(),
                "Input SHA-256": fingerprint,
                "Use": "Advisory screening only; human review required.",
            }
            progress = st.progress(0, text="Preparing screening batch...")
            for index, uploaded_file in enumerate(uploaded_files, 1):
                logger.info("Processing PDF %d of %d", index, len(uploaded_files))
                progress.progress(
                    (index - 1) / len(uploaded_files),
                    text=f"Analyzing {index} of {len(uploaded_files)}: {uploaded_file.name}...",
                )
                try:
                    if uploaded_file.size > MAX_PDF_BYTES:
                        raise EvaluationError("PDF exceeds the 10 MB file limit.")
                    resume_text = extract_text_from_pdf(uploaded_file)
                    if not resume_text:
                        raise EvaluationError(
                            "No usable text: PDF is empty, scanned, encrypted, corrupt, "
                            "or exceeds 50 pages / 120,000 extracted characters."
                        )
                    evaluation = evaluate_resume(
                        resume_text, jd_text, uploaded_file.name,
                    )
                    st.session_state.evaluations.append(evaluation)
                    st.session_state.evaluation_sources.append(uploaded_file.name)
                    logger.info("PDF %d of %d evaluated successfully", index, len(uploaded_files))
                except EvaluationError as exc:
                    st.session_state.failures.append({"File": uploaded_file.name, "Reason": str(exc)})
                    logger.warning("PDF %d of %d could not be evaluated: %s", index, len(uploaded_files), exc)
                except Exception:
                    # Do not expose provider details or private document data through a traceback.
                    st.session_state.failures.append({
                        "File": uploaded_file.name,
                        "Reason": "Unexpected processing failure. Retry or review manually.",
                    })
                    logger.error("Unexpected processing failure for PDF %d of %d", index, len(uploaded_files))
                progress.progress(index / len(uploaded_files), text=f"Processed {index} of {len(uploaded_files)}")
            st.session_state.run_metadata["Completed (UTC)"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            st.session_state.run_metadata["Successful"] = str(len(st.session_state.evaluations))
            st.session_state.run_metadata["Unprocessed"] = str(len(st.session_state.failures))
            logger.info(
                "Screening batch finished (successful=%d failed=%d)",
                len(st.session_state.evaluations),
                len(st.session_state.failures),
            )
            if st.session_state.evaluations:
                st.success(f"Screening complete: {len(st.session_state.evaluations)} candidate(s) evaluated.")
            else:
                st.error("No resumes could be evaluated. Review the file errors below.")
        st.session_state.screening_in_progress = False
        st.rerun()

    render_results(fingerprint)


if __name__ == "__main__":
    main()