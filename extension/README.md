# Application Studio browser extension (Chrome / Edge)

Sends the job you are viewing, with its description, to the Application Studio desktop app.

## Install (once)
1. Open `chrome://extensions` (or `edge://extensions`) and turn on **Developer mode**.
2. **Load unpacked** and choose this `extension` folder.
3. In the app: **Settings -> Browser extension**, copy the token.
4. Click the extension icon, paste the token, **Save**.

## Using a controller on a VPS
In the popup's setup (gear) enter the controller's address under **Server address** (for example
`http://100.64.0.5:47821`), paste its token and save. Chrome asks once for permission to reach that address. Leave the
address empty to use the app on this computer. See `docs/controller-viewer.md` for how to reach the controller safely.

## Use
Open a job posting, click the extension icon, check the link, company and description, then **Add to queue**.
If you already applied to that company, or the link is already queued, it is not added.
Select the description text on the page first if the automatic detection picks the wrong part.

## Fill an application (Greenhouse, phase 1)
On a Greenhouse application page (`boards.greenhouse.io`, `job-boards.greenhouse.io`) the popup switches to
**Fill this form**. It matches the page to a finished job (by link, then company, else you pick one), fills name,
contact details, links and Yes/No eligibility answers from **Settings -> Personal information**, and attaches the
PDF resume and DOCX cover letter when the form accepts them. Only empty fields are filled.

It never submits, never answers self-identification questions, and lists what you must finish under **Needs you**.
After you press Submit on the site, reopen the popup and press **I submitted it** to mark the job applied.
Forms embedded in a company's own page (an iframe) are not supported yet. After updating the extension, reload it
on `chrome://extensions`.

The app must be running. It listens on `127.0.0.1:47821` only; every request needs the token.
