# AI Resume-to-JD Screener & Talent Copilot

A complete Python/Streamlit proof of concept for human-led technical resume review.
Upload a PDF batch, select or edit a job description, and receive validated Gemini
scorecards, a ranked leaderboard, interview questions, and Excel/CSV downloads.

**This is an advisory proof of concept, not a certified production hiring system.**
AI output can be inaccurate or biased. A qualified human must review the original
resume and job requirements before making any hiring or rejection decision.

## Setup

Use Python 3.11 or newer (3.12 recommended) and a Google AI Studio API key with access
to `gemini-2.5-flash`. Model availability, billing, and quotas depend on your project
and region. Obtain a key at <https://aistudio.google.com/apikey>.

From the project directory on macOS/Linux:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python set_gemini_key.py
python -m streamlit run app.py --server.address 127.0.0.1
```

`set_gemini_key.py` prompts for the API key without echoing it and saves it in the
macOS login Keychain. The app reads it from Keychain when needed. `.env` is not read.
If the key is currently in `.env`, store the rotated key in Keychain, then remove the
`GEMINI_API_KEY` entry from `.env`. Never put the key in a command argument or commit it.

Open <http://localhost:8501>. The app's status indicator confirms a credential is
configured, not that Google has authenticated it successfully.

On Windows PowerShell, activate with `venv\Scripts\Activate.ps1` and set
`GEMINI_API_KEY` through a secure environment or secret manager. An existing virtual
environment can be reused. Dependencies have compatible major-version bounds; pin a
reviewed lockfile and use reproducible builds before any controlled deployment.

## Workflow

1. Select **Senior .NET / Cloud Backend Engineer**, **Full Stack Python Developer**,
   **UI Developer (3+ years)**, **DevOps Engineer (5+ years)**,
   **Java Developer (3+ years)**, or **Custom JD**. The populated text remains
   editable. Changing the preset resets the editor to the new preset, so retain any
   custom edits you need elsewhere.
2. Upload up to 20 text-based PDF resumes. Each must be at most 10 MB, 50 pages, and
   120,000 extracted characters. The JD is limited to 30,000 characters.
3. Confirm authorization to send this personal data to Google, then select
   **Screen Resumes**. Processing is sequential, with per-file progress and errors.
4. Review the counts, leaderboard, and individual scorecards against original evidence.
   Missing evidence is unverified, not proof of missing ability. Failed files receive
   no score and are excluded from **Total Screened** and decision counts.
5. Download Excel for a leaderboard, complete scorecards, and the original run's JD,
   model, timestamp, input fingerprint, and counts. CSV contains complete scorecards
   and source filenames, including interview questions and all matched skills.

Results survive ordinary reruns within the same browser session. Changing inputs
shows a stale-results warning; screening again replaces the previous batch. Clear
Results clears scorecards and run metadata, not uploaded files or the key input.
Session loss or server restart loses results. Downloads contain personal data and
must be stored and shared according to your organization's retention policy.

## Files

| File | Responsibility |
| --- | --- |
| `schema.py` | Strict Pydantic v2 output contract and score/decision consistency |
| `evaluator.py` | Bounded PDF extraction, evidence-based prompt, Gemini structured output |
| `app.py` | Session-isolated UI, batch orchestration, scorecards, and safe exports |
| `job_descriptions.py` | Editable preset job descriptions shown in the selector |
| `app_logging.py` | Daily log files with privacy-conscious application events |
| `gemini_credentials.py` | Reads the API key from Keychain or an environment variable |
| `set_gemini_key.py` | Securely prompts for and stores the key in Keychain |
| `requirements.txt` | Runtime dependencies |
| `test_screener.py` | Offline regression tests, including Streamlit application tests |
| `.streamlit/config.toml` | Upload cap, privacy-conscious telemetry default, native theme |

## Logs

Application logs are written under `logs/` to date-based files such as
`resume-screener-2026-11-09.log`. INFO, WARNING, and ERROR events include batch
progress and sanitized failure categories. Resume text, job-description text,
candidate names, filenames, API keys, and provider error bodies are not logged.
The `logs/` directory is excluded from Git.

Each screening batch gets a unique UUID/GUID. Every related app and Gemini log
line includes `[request_id=<GUID>]`, with one `REQUEST START` delimiter at the
beginning of the batch and no end delimiter. The same ID is included in Excel run metadata.
Concurrent sessions retain separate IDs; their lines may interleave in the shared
daily file, so filter by request ID to follow one batch. Events outside a batch
use `[request_id=-]`.
The regression tests do not write synthetic requests into application log files.

## Evaluation Contract

`evaluate_resume(resume_text, jd_text, candidate_filename)` uses the official
`google-genai` SDK and retrieves credentials from macOS Keychain, with an explicit
environment-variable fallback. The optional keyword-only `api_key` remains useful
for tests or trusted callers. Responses use `application/json`, a Gemini-
compatible JSON schema derived from `CandidateEvaluation`, and temperature `0.1`.
Pydantic still rejects extra fields locally because Gemini does not accept the
`additionalProperties` keyword. The parsed response is used directly, with
`model_validate_json(response.text)` as fallback. Schema or
consistency failures are reported, not silently repaired or assigned a default score.

The prompt weights core required skills/depth at 60%, relevant seniority/scope at
25%, and relevant delivery evidence at 15%. Transferable evidence receives partial
credit; nice-to-haves cannot replace hard requirements. This rubric is guidance to
a generative model, not an independently verified mathematical scoring algorithm.
Low temperature reduces variation but does not make scores deterministic or
scientifically calibrated across candidates or separate runs.

Decisions are enforced in Python: **Shortlist** at 75-100, **Review** at 55-74, and
**Reject** at 0-54. Interview questions must number 3-4. Scores must be real integers
within 0-100; unexpected fields, invalid labels, and blank skill entries are rejected.
The prompt requests filename fallback for missing names and 2-3 summary sentences;
those semantic requirements still need human verification.

The SDK uses a 60-second timeout per HTTP attempt, up to three attempts for transient
429/5xx errors, and bounded backoff. A batch can therefore take several minutes.
There is no background job queue, cancellation control, or durable resume processing.

## Tests

```bash
python -m unittest -v test_screener
```

Tests run without credentials or network calls. They check score boundaries, strict
validation, PDF extraction and failure handling, Gemini response paths, safe exports,
editable JD presets, batch continuation, result counts, and stale-run warnings.
They do **not** certify live model quality or provider availability. Perform a live
smoke test with authorized synthetic resumes before use with real candidate data.

## Security, Privacy, and Deployment

- Resumes and the JD are sent to Google Gemini. Review the current Gemini API terms,
  data-use rules for your account/tier, residency, and retention before uploading PII.
  Do not assume free and paid tiers have the same data protections.
- The app does not intentionally persist PDFs, keys, or results to disk and avoids
  globally caching them. Streamlit and infrastructure hold data in memory; this is
  not secure erasure or a guarantee about swap, crash dumps, proxy logs, or backups.
- Do not commit credentials. Local macOS setup uses Keychain; `.env` is ignored and
  is not loaded by the app. For hosted systems, use a secret manager and appropriately
  scoped credentials. Environment variables are supported as an explicit fallback.
- Treat local use as trusted-user only. The app has no authentication, authorization,
  per-user quota, tenant isolation guarantee, or durable audit trail. Keep it bound to
  loopback until these controls are in place. HTTPS and access controls are required
  for remote access; do not disable Streamlit's CORS/XSRF protections.
- PDF parsing is bounded but runs in-process, not a sandbox. Hostile or compressed
  PDFs can consume disproportionate CPU/memory before limits are applied. Public
  intake needs malware checks, process isolation, resource/time limits, rate limits,
  and a queued worker architecture. Scans require a separate reviewed OCR pipeline;
  encrypted PDFs are skipped rather than requesting passwords.
- Prompt instructions separate untrusted resume/JD content from the rubric. Prompt
  injection is still possible; never treat model output as authoritative or execute
  it. UI output is displayed as plain text; exports neutralize formula-like strings
  and remove illegal XML control characters. Keep downstream systems equally cautious.
- Do not use protected attributes for screening. The prompt prohibits demographic
  inference, but names and other personal data are still sent to the model. Deployments
  need job-related validation, representative fairness testing, candidate notices,
  legal/privacy review, accommodations, and meaningful human oversight.
- Before production, add authenticated access, request/resource budgets, isolated
  parsing, sanitized operational monitoring, retention/deletion controls, an approved
  provider data agreement, reproducible dependency builds, auditability, and live
  evaluation/quality gates. This repository does not substitute for those controls.

## Troubleshooting

- **No usable text:** Try opening the PDF and selecting its text. Empty, scanned,
  encrypted, corrupt, or over-limit PDFs are skipped without stopping the batch.
- **Authentication/access:** Check the key, enabled project, and model permissions.
- **Quota:** Check Google project rate limits/billing; retry later or use smaller batches.
- **Invalid scorecard:** Retry or review manually. Never use a fabricated fallback score.
- **Model unavailable:** Confirm `gemini-2.5-flash` is available for your project/region.
  A future model migration requires updating the model configuration and rerunning
  quality validation; it is not automatically substituted.