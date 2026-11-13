# Application tracking — design notes

## Five separate concerns (never one status field)

| Concern | Where it lives | Set by |
|---|---|---|
| A. Document generation | `jobs.status` (Queued … Done / Failed / Skipped) | queue engine |
| B. Submitted? | `jobs.applied_at` | explicit user action only |
| C. Employer response | `jobs.response_status`, `first_response_at` | messages, stage moves, rejections |
| D. Recruitment stage | `jobs.app_stage` (not_applied → applied → recruiter_contact → screening → technical → final → offer) | user / interview scheduling |
| E. Final outcome | `jobs.outcome` (rejected, withdrawn, position_closed, accepted) | user; reversible only by an explicit *reopen* |

Generating documents never touches B–E. A finished job simply becomes visible in **Applications** (`status = Done OR applied OR staged`).

## Event sourcing

`job_events` is append-only. Columns added in schema v2: `notes`, `corrects_id`.

* Corrections are new rows (`event_type = 'correction'`, `corrects_id` → the amended row) that **void / restore / re-time / re-note** the target.
* `core/lifecycle.effective_events()` applies corrections; `core/lifecycle.replay()` folds them into `Facts` (stage, applied date, outcome, response, first-reach time of each stage, …).
* The summary columns on `jobs` are a cache of `replay()`, refreshed in the same transaction as every write (`ApplicationDatabase.refresh_summary`). Analytics replay the same events, so the two can never disagree.
* The queue worker upserts whole `JobItem`s it loaded earlier; `TRACKING_COLUMNS` are excluded from that `UPDATE` so a stale snapshot cannot clobber tracking data.

Supporting tables (all cascade with the job): `interviews`, `messages`, `job_actions` (next action + follow-ups).

## Migrations (`core/migrations.py`)

Versioned in `meta.schema_version`; each runs in one transaction; additive only (no drops/renames/rewrites). Before upgrading a non-empty database a copy is written next to it as `studio.pre-v2.bak`. v2 backfills `imported / docs_generated / generation_failed / skipped_duplicate / applied / stage_changed / outcome` events from v1 rows (payload `backfilled: true`) and recomputes every summary. The legacy `application_stage` column is preserved verbatim and no longer written.

## Time

All instants are stored as UTC ISO-8601. Calendar filters ("today", "this month", custom ranges) use **local** midnights converted to UTC (`core/timeutil.range_for`), so DST days are 23/25 h long and a 19:00 UTC-8 event belongs to that local day. Interviews keep their own IANA zone; wall-clock input is converted with `zoneinfo` (`tzdata` is bundled).

## Analytics definitions

See `core/analytics.py` (module docstring) and the "How numbers are counted" note on the Analytics screen:
period counts vs. cohort rates; rates carry numerator/denominator and show "—" for an empty denominator; archived rows stay in history but leave the "right now" tiles; reopened applications count once; "reached stage X" includes later stages.

## Duplicate-company policy

Unchanged default: an existing `<root>/<Name>/<Company>` folder skips the job. A per-job `duplicate_override` (Add-to-Queue checkbox, or "Allow this company again" on a skipped job) bypasses it explicitly; the new role is written to its own role folder (`… (2)` if the role folder already has files).

## Generated files and sync to disk

Documents live on disk; the database keeps their paths and the validated resume JSON (`jobs.resume_json`). If a user deletes files, **Sync to disk** (`core/job_export.sync_jobs_to_disk`) rebuilds them from that JSON:

- only files that are **missing** are written, at their original paths (the folder is recreated if needed); existing files are never overwritten, so hand-edited resumes are safe;
- works for one row or many: Job Queue (row menu, bulk bar, inspector), Generated Files (preview pane, list rows, the toolbar button for the multi-selection or everything restorable in view);
- only finished jobs with stored JSON are eligible; others are reported, not changed;
- each restore appends a `files_regenerated` event (`mode: sync`).

Generated Files shows first-page thumbnails (DOCX rendered by `ui/doc_preview.py`, PDF via QtPdf) and a larger preview of the selection.

## Credentials

`core/secrets.py`: Windows DPAPI (current-user), stored in `credentials.dat`; never in `settings.json`. A plaintext key from an older `settings.json` is migrated on first load and the original file is kept once as `settings.json.pre-redesign.bak`.

## Developing

```
python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest            # headless (QT_QPA_PLATFORM=offscreen is set by tests/conftest.py)
set AUTO_APPLY_STUDIO_HOME=C:\temp\studio-home   # run the app against a scratch data folder
```

## Browser extension intake

`extension/` (Chrome/Edge, Manifest V3) reads the job from the page the user is viewing and, after the user
confirms the link, company and description in its popup, posts it to the desktop app (`core/browser_intake.py`).

Jobright and HiringCafe pages have their own readers in `extension/extract.js` (the page's embedded job data first,
then what the page shows); every other site uses the generic reader. They read the title, company, salary and a
sectioned description (summary, responsibilities, qualifications required/preferred, benefits, company). The only
link saved is the original job link, i.e. the company's own apply address when the page shows one (the aggregator
page is not stored). The popup also reads the salary; the
app keeps the posting's wording (`salary_text`) and a parsed range (`salary_min`/`salary_max`/`salary_period`/
`salary_currency`, schema v3, `core/salary.py`). A posting without pay is normal and never blocks adding.

- Server: loopback only (`127.0.0.1:47821`), bearer token kept in OS-backed storage (`intake_token`), `Host` and
  `Origin` checks (only `chrome-extension://` origins), 1 MB body limit.
- Endpoints: `GET /api/status`, `POST /api/check` (applied-company and queued-link check), `POST /api/jobs`.
- Rules: a company with an **applied** job (names compared ignoring case, punctuation and suffixes like Inc/LLC)
  or a link already in the queue is refused (HTTP 409). The popup also checks before sending.
- The job is added as Queued with its description (no web fetch). The worker starts automatically when idle,
  never when the user paused it or Claude is not signed in (setting: "Start the queue automatically...").
- The popup detects company and role as well as it can; the user corrects them before sending. After generation the
  queue engine fills a still-empty company/role from the resume and checks applied companies once more (a job whose
  company was unknown, e.g. a pasted link, is skipped with "Already applied to ..."; "Allow duplicate" overrides it).
