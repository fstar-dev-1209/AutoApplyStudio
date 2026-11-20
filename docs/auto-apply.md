# Auto Apply

Auto Apply finds new jobs on Jobright, has your resume chat (Claude or ChatGPT) write the documents, and applies on
Greenhouse by itself. It is built to stop and tell you rather than guess.

```
Jobright (every ~hour)  ->  keep Greenhouse links  ->  Job Queue  ->  resume + cover letter (your chat)
                                                                          |
        Applications / Analytics  <-  "applied" only after a confirmation page  <-  fill, check, send
```

## What it does, step by step

1. **Find.** Once an hour (a random gap of 48-72 minutes, never a fixed clock; the interval is a setting) the bot reads
   your Jobright list in the bot browser. Your filters live in your Jobright account; the bot reads whatever that list
   shows, newest first, and stops at the first screen with nothing new. Only postings from the last day (setting) are
   kept. The list carries the employer's apply link. Only a Greenhouse link (or a company page that embeds Greenhouse)
   is opened on Jobright, and that page is read the same way the extension reads it: title, company, salary and the
   full sectioned description.

   **One batch per hour, freshest first.** At most 20 new jobs are queued per hour (*Queue at most, per hour* in
   Settings; for a different interval the batch is scaled to match). The postings are taken newest first, and a page
   is opened only until the batch is full. A good posting that did not fit is recorded as "Over this batch's limit" and
   is **not** kept for a later batch: an hour later it would be old news. Jobs of the previous batch that are still
   waiting for their resume when a batch with something fresher arrives are dropped too: they stay in the queue as
   *Skipped* with the reason "Expired", so you can see them. If nothing fresher arrives, nothing is dropped. Resumes
   are written freshest posting first (jobs you add by hand go before them), and applications are sent freshest
   posting first, so the newest jobs are always applied to first.
2. **Sort.** Other domains are kept in *Found on Jobright* (marked "Other site") without opening the Jobright page, and
   they are **never generated**. A list row with no apply link is opened only to find that link; if it is not Greenhouse
   the text is discarded. Duplicates, blocked companies, title words you reject, companies you already applied to,
   companies whose folder already exists, and companies that already have a job in the queue (or earlier in the same
   batch) are dropped without a visit, so a job that would only be skipped later never takes a place in the batch.
3. **Write.** New jobs enter the normal queue and the existing pipeline writes the resume and cover letter. The address
   of the chat conversation is saved on the job. The Chat tab shows two pages of the same account side by side
   (one sign-in): **Resumes** writes the documents and **Application questions** answers the questions of forms
   being filled, so neither waits for the other. They take turns only for the few seconds it takes to paste a prompt
   (the clipboard is shared).
4. **Apply.** The bot opens the form, reads every field and decides each answer *before* touching it:

   | Kind of question | Answered from |
   | --- | --- |
   | Name, email, phone, links, location, work authorization, sponsorship | Settings -> personal information (also editable on the *Your answers* tab) |
   | Current employer, job title, school, degree | The resume written for that job and your profile: never typed twice |
   | Years of experience (total, or with one skill) | The chat, which reads the resume (a date calculation from the resume is the fallback) |
   | Salary and salary ranges | One figure per job, inside the job's own posted range (see *Salary* below) |
   | Notice period, relocation, how you heard, etc. | *Your answers* tab (facts only you know) |
   | Multiple-choice "how many years with X" questions | The chat picks one of the form's own options |
   | Gender, race, veteran, disability, orientation | Your choice, **Decline by default** |
   | "Privacy notice" / "I certify" boxes | Ticked only if you allow it; text-message consent defaults to **no** |
   | Open questions ("Describe your RAG experience") | The job's own chat conversation, with your style prompt |
   | Anything else required | **Not guessed: the application stops and appears under Needs you** |

   Legal and personal questions (convictions, current salary, immigration status, date of birth, and similar) are never
   sent to a chat, even when a chat is available.

   **Salary.** Every job has its own figure, not one saved number. The limits are facts: the pay range the posting
   shows (or one stated in the job description) and *Minimum salary I accept* in Settings (default $80,000 a year).
   The chat that wrote the resume is asked once for a single figure and aims at the lower-middle of the range, 40-55% of
   the way from the low end to the high end (*Salary to ask for*), so the number gets replies and never looks like a
   stretch. The figure is then kept inside the limits (never above the posted maximum, never below your minimum) and
   written the way each question needs: digits, "$112,000", a low-high range, an hourly rate, or the option of a
   dropdown that holds it. The same figure is used for every salary question of the form. With no posted range the chat
   estimates a typical figure, never below your minimum; if the chat cannot be reached the figure is worked out from the
   band, or taken from *Expected salary* under *Your answers*. The record shows how the figure was reached (for example
   "Posted $100,000 - $110,000; aimed at 50% of the range"). Current salary and salary history are never answered.

   **Jobs that pay too little are not queued.** At discovery, a job whose posted pay clearly tops out below your minimum
   is left out and counted under "pay below your minimum" (setting: *Skip jobs whose posted pay tops out below my minimum
   salary*). When the pay is missing, unclear (no unit, "$60K+") or not in dollars, the job is kept.

   **Optional questions are left blank.** A question the form does not require is not answered at all: it is not
   asked of the chat and not filled from your profile, since the form accepts it empty. Required questions are
   answered as above, and the resume and cover letter are still attached (they are files, not questions). If a form
   turns out to need a field it did not mark as required, the site rejects the send, the application stops under
   *Needs you*, and the reason lists the questions that were left blank. The setting is *Leave questions blank when
   the form does not require them* (Settings -> Auto apply; on by default).
5. **Check, then send.** The filled form is read back; if a required field is empty the application is held. Only then
   is *Submit* pressed. Greenhouse's emailed security code is read from Outlook (in the bot browser) and entered.
6. **Record.** "Applied" is recorded only after a confirmation page is seen, with a screenshot and every answer given.
   If no confirmation appears, the job is held for you rather than marked either way. For every sent application the
   questions and answers are also written, in full, to `Application answers.txt` in the job's folder: each question,
   the answer, and where it came from (your profile, your saved answers, a built-in rule, or **written by the chat**,
   which is the one to review), then the optional questions left blank.

## Reports to Telegram

With a Telegram bot token and chat id in Settings (and *Also send status reports* ticked), the app sends one message
for each event. Each kind has its own icon and heading, so they can be told apart at a glance:

| Message | Heading | Says |
| --- | --- | --- |
| Job search (every cycle) | 🔎 **JOB SEARCH** | jobs listed and recent; Greenhouse and other sites; queued; the jobs left out and why (company already applied to, folder exists, another job at the company queued, block list, title words, same posting, over the hour's limit); old jobs dropped |
| Resume | 📄 **RESUME READY** / ❌ **RESUME FAILED** / ⏭ **RESUME SKIPPED** | the job, and the reason when it failed or was skipped |
| Application | ✅ **APPLIED** / ❌ **APPLICATION FAILED** / 🚫 **POSTING CLOSED** / 🧪 **DRY RUN** | the job, a link to the posting, a summary line, and for a sent application **its questions and answers inside the same message** |
| The app needs you | 🙋 **NEEDS YOU** / ⚠️ | what stopped it and why |

In an *Applied* message the questions and answers sit in a collapsed quote (tap to open). Each answer is marked with
where it came from: 👤 your profile, 💾 your saved answers, ⚙️ a built-in rule, 🤖 written by the chat (the ones worth a
look). Optional questions left blank are listed at the end. If the answers are too long for one message, the same
message is sent as a file with the result as its caption.

Your contact details, demographic and legal answers and pay are shown as 🔒 *kept private*; the full record stays on
this computer (`Application answers.txt` in the job's folder). A Telegram chat with a bot is not end-to-end encrypted,
which is why. Links never unfold into a preview card.

**If the app stops or freezes.** A small watchdog starts with the app and sends ⚠️ *Auto Apply Studio stopped*
(on its own), *was closed* (from its window), *is not responding* (the window has not answered for 3 minutes) or *is
responding again*, with the free memory on the server. It never starts the app again: start it yourself with
`Run Auto Apply Studio.bat`.

**Memory.** A chat page that stays open keeps growing. An idle page above 1.5 GB is replaced by a fresh one (checked every minute, never while it is writing a resume or answering a question).

**Closing** asks first, then ends the app at once: it never waits for a job in progress, which is recorded as
interrupted and tried again at the next start.

**Only what truly needs you waits for you.** A stop caused only by temporary problems (the chat page or the browser
was busy or closed, a click timed out, the security-code email was late) is recorded as a failure and retried by
itself after 30 minutes. A form the bot must not or cannot complete (it asks whether the application is automated,
asks for answers written without AI, or wants a document the app cannot make) is skipped for good, with a ⏭
*APPLICATION SKIPPED* message. A job that stops on the same thing twice is set aside.

**Where the application questions are asked.** In a ChatGPT tab of the bot browser (the Chrome that fills the
forms): one tab per application, opened when the form has its first question for the chat and closed when the
application is done. It runs on the browser's own thread, types into the page through the browser (no clipboard,
no window focus) and never touches the app window. Sign in once to the ChatGPT account for questions in the bot
browser (Auto Apply > Open the bot browser > chatgpt.com). Resumes are still written in the app's ChatGPT tab, with
its own account. Settings key `question_chat`: `chrome` (default) or `app` (the earlier questions pane in the app).

**Where the questions are asked.** With your real work history saved in Settings (every resume then names only your
real employers), each application's questions get a new chat of their own. Its first message holds the job
description (up to 6,000 characters), your resume for that job (compact, without the cover letter) and your facts;
the rest of the form's questions continue in that chat. Only saved answers about you as a person (relocation, start
date, travel, address, work authorization, checks) are reused; experience and skill questions are always answered
from that job's resume. Without a work history, the questions are asked in the conversation that wrote the resume.
The prompt is pasted once, as plain text, and sent only when it is really in the message box.

**Answers the chat writes are checked before they are typed in.** An answer that talks about the information instead
of answering ("Address not provided", "I can't determine…", "my resume", "as an AI") is asked for once more; if it
is still not a real answer, an optional question is left blank and a required one stops for you. Your address,
preferred name, GPA and certifications come from *Your answers*, never from the chat.

## Modes (Auto Apply page)

- **Off**: nothing is applied (finding jobs can still run).
- **Dry run**: fills and checks every form, saves a screenshot, sends nothing. Use this first.
- **Auto**: sends applications. Switching to it asks for confirmation. A daily limit (default 40), active hours and a
  pause of about a minute or two between applications apply. **Stop now** abandons the job in progress before it is sent.

## When it needs you

*Needs you* lists each job it stopped on, with the reason and a screenshot. For each: open the posting, **I applied**
(marks it applied), **try again** (after you added the missing fact), or **skip**. You are also alerted by Discord or
Telegram if configured (Settings -> Auto apply), once per problem rather than every cycle.

A captcha is for a person. The bot alerts you and waits (up to 10 minutes) in that tab for you to complete it in the
bot browser, then carries on. It does not try to solve or bypass it.

## Setting it up on the VPS

1. Install the app on the VPS. Run it as **Standalone** or **Controller** (see `controller-viewer.md`); your own computer
   can be the viewer and sees the same lists.
2. **Settings -> Resume generation**: choose Claude or ChatGPT, set the proxy (and its login if it has one), restart.
   Open the **Claude/ChatGPT** page and sign in once.
3. **Settings -> Auto apply**: tick *Look for new jobs on Jobright*, set the time zone and active hours, daily limit, the
   bot browser, and (optionally) Discord / Telegram.
4. **Auto Apply -> Open bot browser.** In that window sign in to **Jobright** (set your filters) and **Outlook**. The
   bot keeps these sign-ins in its own profile.
5. **Auto Apply -> Your answers**: fill the facts and (optionally) your style prompt for open questions.
6. Leave it on **Dry run** for a few days (see *Checking it works*), then switch to **Auto**.

A headed browser needs a live desktop. On a Windows VPS, disconnect the Remote Desktop session instead of signing out.

## Checking it works (do this before Auto)

Three scripts print what the bot really sees on live pages, because the sites change without notice:

```
.venv\Scripts\python scripts\diagnose_chat.py chatgpt        # the chat page: composer, buttons, replies
.venv\Scripts\python scripts\capture_jobright.py             # what Jobright sends, and how the reader reads it
.venv\Scripts\python scripts\diagnose_apply.py <greenhouse job url>   # a dry run on one real posting
```

A field the dry run misreads, or a question it should have answered, is a one-line fix in `core/form_answers.py`
(label rules) or your facts. The site-specific selectors are each in one place: `core/chatgpt_chat_scripts.py`,
`core/claude_chat_scripts.py`, `core/jobright.py`, `core/form_scan.py`, `core/mail_code.py`.

## What is and is not verified

Automated tests run the real code in a real Chrome against stand-in pages that imitate each site's behaviour (type-ahead
dropdowns, uploads, validation, an emailed code, a captcha frame, a closed posting, an embedded form, a Jobright-style
list loaded by script, an Outlook-style inbox, a ChatGPT/Claude-style chat). They prove the logic and the safety rules.
They do **not** prove the live sites still match: that needs the three scripts above on your machine. Not tested: a
real Greenhouse posting, a real Jobright session, ChatGPT/Claude from a datacenter proxy, Outlook web, and the
Greenhouse security-code email in practice.

## Things to know

- Automated use of Jobright, ChatGPT/Claude web and employers' forms can be against their terms; accounts can be limited
  or applications ignored. Keep the daily limit modest.
- ChatGPT/Claude have message limits. Each Greenhouse job costs one generation plus one message per open question.
- Only Greenhouse is automated. Other sites are listed for manual use; each needs its own adapter.
- Years of experience *with a specific skill* is answered by the chat (from the resume), not by the total in your facts.
- A company's own careers page that embeds Greenhouse (`gh_jid` links) is supported; two spellings of the same posting
  from different sources may not be recognised as duplicates.
