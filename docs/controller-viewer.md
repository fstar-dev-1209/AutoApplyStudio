# Controller and viewer

Run one copy of the app on a VPS as the **controller** (it generates resumes with your Claude session) and another on
your own computer as the **viewer** (it shows the progress and lets you manage everything else). The controller keeps
the one real database.

Pick the mode in **Settings -> How this app runs**, then restart the app. A fresh install is **Standalone**, which
behaves exactly as before.

## What each mode does

| | Standalone | Controller | Viewer |
| --- | --- | --- | --- |
| Generates resumes (Claude) | yes | yes | **no** |
| Start / pause / stop / retry the queue | yes | yes | no - do it on the controller |
| Browser extension adds jobs | this computer | the VPS | (the extension talks to the controller) |
| Job Queue, Applications, Analytics, Generated Files | yes | yes | yes |
| Mark applied, stages, notes, interviews, messages | yes | yes | yes (sent to the controller) |
| Skip, remove, add links, paste a missing job description | yes | yes | yes (sent to the controller) |
| Settings: profile, prompt, Claude | yes | yes | no - set on the controller |
| Settings: theme, output folder, connection | yes | yes | yes |

## How the two stay in step

- The viewer keeps a **read-only copy** of the controller's database (`replica.db` in the app's settings folder,
  separate from any `studio.db`). It checks every few seconds and downloads a fresh copy only when something changed.
  The download is verified (checksum and integrity check) and swapped in as a whole; a failed download keeps the
  previous copy.
- Every change you make in the viewer is sent to the controller as a **command**. The controller applies it to the real
  database, then the viewer fetches the new copy, so the screen shows the result when the action returns. Only the
  actions in the table above can be sent; anything else is refused. Event history stays append-only.
- If the controller is unreachable the viewer keeps showing the last copy, and actions that need the controller show a
  message instead of changing anything.
- A repeated request (for example after a dropped connection) is recognised and never applied twice.

## Documents

Generated documents are **not downloaded**. The copy of the database contains each resume as JSON, so the viewer
rebuilds the Word/PDF/cover letter on your computer with the existing *Sync to disk* logic.

- When the viewer first connects, every resume already finished is taken as "handled" (nothing floods your disk). After
  that, each newly finished resume is rebuilt automatically while the viewer is running and connected. A job whose
  rebuild keeps failing is left for the manual button.
- A file you delete afterwards stays deleted until you press **Sync to disk**.
- The controller's output folder is translated to your own output folder (Settings -> Output), so the same
  `Name/Company/Role/` layout appears on your computer.
- Rebuilt documents use the controller's profile (shared read-only) and the same export code, so they match. The
  "2 months ago" end date of the current job is computed when a file is built, so a file rebuilt later shows the date
  from that later moment.
- Turn the automatic rebuild off with **Save each newly finished resume to this computer automatically** in
  Settings -> Controller connection.

## Setting up the VPS (Windows)

1. Install the app on the VPS and choose **Controller** in Settings. Restart.
2. Sign in to Claude once in the app (Remote Desktop). The session stays after you disconnect.
3. **Do not set an app password on the controller.** It would wait for the password after every reboot. A locked
   controller also cannot share its profile with the viewer.
4. Keep the app running, and leave *start the queue when a job arrives* on (Settings -> Browser extension) so jobs sent
   from the extension or the viewer are processed without you. Stopping or pausing the queue is done on the VPS.
5. Choose how the controller is reached (next section), then copy the **token** from Settings -> Browser extension.

## Reaching the controller safely

The link carries your resumes, profile and the power to change your data, protected only by the token. It is **not
encrypted by itself.** Use one of:

- **A VPN such as Tailscale or WireGuard (recommended).** Install it on the VPS and on your computer. In the
  controller's Settings -> Serve a viewer, set *Listen on* to the VPS's VPN address (or `0.0.0.0` with the firewall
  allowing only the VPN interface), and *Allowed names* to `that-address:47821`. Nothing is exposed to the internet.
- **An HTTPS reverse proxy** (Caddy, nginx) in front of the controller, with the app listening on `127.0.0.1` and the
  proxy's hostname:port in *Allowed names*.

Do not open the port to the internet directly. Regenerate the token (Settings -> Browser extension -> *New token*) if
you think it leaked; every viewer and extension then needs the new one.

## Connecting the viewer

1. Install the app on your computer and choose **Viewer**. Restart.
2. Settings -> Controller connection: enter the address (for example `http://100.64.0.5:47821`) and paste the token.
   **Test connection**, then **Save**. The sidebar shows *Controller: Connected*.

## The browser extension

Open the extension's setup (gear), enter the controller's address in **Server address**, paste the token and save. Chrome
asks once for permission to talk to that address. Leave the address empty to use the app on the same computer, as before.
The extension's checks (already applied to this company, already queued) read the controller's database, so they stay
correct whichever computer you are on.

## Auto Apply with a controller

The bot (finding jobs on Jobright, applying on Greenhouse) runs on the **controller** (see `auto-apply.md`). Its results
- the Needs you list, the history, the jobs found - are in the database, so the **viewer** shows them on its own
Auto Apply page, read-only: starting, stopping and answering are done on the controller. Applications the bot sends are
recorded exactly like ones you mark yourself, so they appear in the viewer's Applications and Analytics as usual.

## Limits to know about

- The viewer downloads the **whole** database copy when anything changes. While the controller is generating, small
  progress updates change it every few seconds, so expect steady small downloads. Sending only the differences would be
  the next optimisation if the database grows large.
- Switching a viewer back to Standalone does not merge anything: they use different database files.
- The controller generates with the web chat chosen in Settings (Claude or ChatGPT) or the Claude API, as before.
- Not covered by automated tests: a real VPS, Tailscale or HTTPS in front of it, a Claude login from a datacenter
  address (providers may add extra checks there), and Windows firewall rules.
