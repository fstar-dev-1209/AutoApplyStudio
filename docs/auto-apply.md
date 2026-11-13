# Auto Apply

Auto Apply finds new jobs on Jobright, has your resume chat (Claude or ChatGPT) write the documents, and applies on
Greenhouse by itself. It is built to stop and tell you rather than guess.

```
Jobright (every ~10 min)  ->  keep Greenhouse links  ->  Job Queue  ->  resume + cover letter (your chat)
                                                                          |
        Applications / Analytics  <-  "applied" only after a confirmation page  <-  fill, check, send
```

## What it does, step by step

1. **Find.** Every 8-12 minutes (a random gap, never a fixed clock) the bot reads your Jobright list in the bot browser.
   Your filters live in your Jobright account; the bot reads whatever that list shows, newest first, and stops at the
   first screen with nothing new. Only postings from the last day (setting) are kept. The list carries the employer's
   apply link. Only a Greenhouse link (or a company page that embeds Greenhouse) is opened on Jobright, and that page
   is read the same way the extension reads it: title, company, salary and the full sectioned description.
2. **Sort.** Other domains are kept in *Found on Jobright* (marked "Other site") without opening the Jobright page, and
   they are **never generated**. A list row with no apply link is opened only to find that link; if it is not Greenhouse
   the text is discarded. Duplicates, blocked companies, title words you reject, and companies you already applied to
   are dropped without a visit.
3. **Write.** New jobs enter the normal queue and the existing pipeline writes the resume and cover letter. The address
   of the chat conversation is saved on the job.
4. **Apply.** The bot opens the form, reads every field and decides each answer *before* touching it:

   | Kind of question | Answered from |
   | --- | --- |
   | Name, email, phone, links, location, work authorization, sponsorship | Settings -> personal information (also editable on the *Your answers* tab) |
   | Current employer, job title, school, degree | The resume written for that job and your profile: never typed twice |
   | Years of experience (total, or with one skill) | The chat, which reads the resume (a date calculation from the resume is the fallback) |
   | Salary, notice period, relocation, how you heard, etc. | *Your answers* tab (facts only you know) |
   | Multiple-choice "how many years with X" questions | The chat picks one of the form's own options |
   | Gender, race, veteran, disability, orientation | Your choice, **Decline by default** |
   | "Privacy notice" / "I certify" boxes | Ticked only if you allow it; text-message consent defaults to **no** |
   | Open questions ("Describe your RAG experience") | The job's own chat conversation, with your style prompt |
   | Anything else required | **Not guessed: the application stops and appears under Needs you** |

   Legal and personal questions (convictions, current salary, immigration status, date of birth, and similar) are never
   sent to a chat, even when a chat is available.
5. **Check, then send.** The filled form is read back; if a required field is empty the application is held. Only then
   is *Submit* pressed. Greenhouse's emailed security code is read from Outlook (in the bot browser) and entered.
6. **Record.** "Applied" is recorded only after a confirmation page is seen, with a screenshot and every answer given.
   If no confirmation appears, the job is held for you rather than marked either way.

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
