# Job apply assist (autofill)

The extension fills an employer's application form from the job's generated files and your saved profile.
**You review the form and press Submit yourself.** Nothing is ever submitted by the app or the extension.

## Rules

- Generation never marks a job applied. Autofill never marks a job applied either.
- A job becomes applied only when you press **I submitted it** in the extension panel (this calls the existing
  `ApplicationService.mark_applied`). The event history stays append-only.
- Autofill is logged as an `autofilled` event (new event type, additive; it does not count as employer activity).
- The extension never clicks Submit/Next/Apply, never solves CAPTCHAs and never creates accounts.
- Answers to eligibility questions (work authorization, sponsorship) are filled only from values you saved in
  Settings. If a value is blank the field is left empty and listed as "needs you".
- With an app password set, the profile is encrypted at rest. While the app is locked the apply endpoints answer
  423 and the extension says so.

## Data flow

```
popup (has the token, may call 127.0.0.1)            page (employer site, never sees the token)
  1. POST /api/apply/kit {url, company}  -> job, profile, file list
  2. GET  /api/apply/file?job=..&kind=pdf -> base64 bytes
  3. executeScript(autofill.js, args = kit + files)  ---->  fills fields, attaches files
  4. <---- report {filled, needs_you, files_attached}
  5. POST /api/apply/event {job, event: "autofilled", report}
  6. user submits on the site, then presses "I submitted it"
  7. POST /api/apply/event {job, event: "submitted_confirmed"}  -> mark_applied
```

The token stays in the popup; the page only receives the data to fill, injected into the isolated content-script
world. Endpoints sit behind the same token, Host and Origin checks as the existing intake endpoints.

## Server endpoints (all need the token)

| Endpoint | Purpose |
| --- | --- |
| `POST /api/apply/kit` | Finds the queued job by link (then by company) and returns `profile`, `job` and `files` metadata. 404 if no job; 409 if the job has no generated files; 423 if locked. |
| `GET /api/apply/file?job=<id>&kind=resume\|pdf\|cover` | Bytes of one file the job already owns (looked up from `generated_files`, never from a client path). |
| `POST /api/apply/event` | `autofilled` (log) or `submitted_confirmed` (mark applied, once). |

Writes happen on the UI thread, like job intake.

## Profile additions (Settings, additive)

`linkedin_url`, `github_url`, `website_url`, `work_authorization` (Yes/No/blank), `needs_sponsorship` (Yes/No/blank).
Old settings files load unchanged; the fields default to blank.

## Extension

`autofill.js` is injected on demand by the popup (`activeTab` + `scripting`, no new host permissions). It has a
site adapter per applicant system. Phase 1: **Greenhouse** (`boards.greenhouse.io`, `job-boards.greenhouse.io`,
embedded `greenhouse.io/embed` forms). Later: Lever, Ashby, then Workday.

An adapter returns a report: what it filled, which required fields are still empty, and which questions it did
not recognise. The popup shows that list so you know what to finish by hand.

## Phase 2 (not built yet)

- Answer bank for screening questions, with Claude drafting free-text answers.
- Every drafted answer is shown for approval before it is written into the form.

## Tests

- Python: kit lookup, file lookup (no path traversal), lock behaviour, event logging, applied only on confirm.
- Browser (`tests/test_autofill.py`): `autofill.js` runs in Qt WebEngine (real Chromium) against hand-written
  approximations of a classic and a script-built Greenhouse form (`tests/js/fixtures/`), including a controlled
  input that only reacts to real input events, a file input with an `accept` list, a native select, radios, a
  type-ahead field and self-identification questions.
- Not covered: live employer sites (the fixtures are approximations, not saved copies) and the popup UI, which has
  no automated test. Try it on a real Greenhouse form before relying on it; the adapter reports what it could not
  fill instead of guessing.
- Embedded forms (a Greenhouse iframe on a company page) are not handled in phase 1.
