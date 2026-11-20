---
name: freshness-and-two-chat-pages
description: "Why discovery queues one freshest-first batch per hour (cap 20, stale jobs dropped) and why the chat tab has two pages (resumes / form questions)"
metadata:
  node_type: memory
  type: project
  originSessionId: 2388784c-8fe3-4b6f-9743-e7f5d43b032e
  modified: 2026-11-16T06:14:35.021Z
---

The owner wants the newest postings applied to first, so old queued jobs must never be generated or applied "instead of" fresh ones.

- Discovery runs hourly (interval 60, 48-72 min jitter) and queues at most `discovery_hourly_cap` (20) jobs per batch, freshest posting first. Postings that did not fit are recorded as filtered "Over this batch's limit" and never revisited.
- Waiting discovery jobs from an earlier batch (older than half the interval, no resume yet, not started) are set to Skipped "Expired..." only when the new batch has something fresher; otherwise left alone. Manual jobs are never expired.
- Everything the queue would reject later (company already applied / folder exists / another job at that company in the queue or earlier in the same batch) is rejected at discovery time so it does not use a slot.
- Order: generation = manual jobs first (FIFO) then discovered by posting time; apply = by posting time (not by which resume finished last). See core/job_order.py.
- The chat tab shows two pages of ONE account (same profile = one sign-in): "Resumes" and "Application questions". Form questions never wait for generation. Only the paste moment is serialized (shared clipboard) via ChatService.paste_in_progress(); entry points refuse/defer rather than wait, because both pages run on the UI thread in nested event loops (waiting inside a nested loop would deadlock).

**Why:** the owner worried that one shared chat page made apply and generation fight, and that a backlog makes the bot apply to stale jobs.
**How to apply:** do not reintroduce a backlog that carries over between batches, a global generation hold for questions, or a second chat account/profile (separate login, and the job's conversation lives in the original account).
