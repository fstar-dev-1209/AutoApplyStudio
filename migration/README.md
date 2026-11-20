# Moving Auto Apply Studio to another computer

This folder holds what the app and its Claude Code sessions need to carry on elsewhere, taken on 2026-11-20.

| File | What it is | Where it goes on the new computer |
| --- | --- | --- |
| `data/studio.db` | the queue, applications, attempts, resumes (a consistent copy) | `%LOCALAPPDATA%\AutoApplyStudio\studio.db` |
| `data/settings.json` | all settings and your saved answers | `%LOCALAPPDATA%\AutoApplyStudio\settings.json` |
| `claude-history/*.jsonl.gz` | the Claude Code conversation history (GitHub tokens replaced with `[REDACTED]`) | unzip into `%USERPROFILE%\.claude\projects\<project folder>\` |
| `claude-history/memory/` | Claude Code's memory notes for this project | `%USERPROFILE%\.claude\projects\<project folder>\memory\` |

`<project folder>` is the repository path with `:` and `\` replaced by `-`, e.g. `C:\Users\Administrator\Desktop\AutoApplyStudio`
becomes `c--Users-Administrator-Desktop-AutoApplyStudio`.

## Restore

1. Clone the repository and set up Python as before (`python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`,
   `.venv\Scripts\python -m playwright install chromium` if the bot browser needs it).
2. With the app closed, copy `data/studio.db` and `data/settings.json` into `%LOCALAPPDATA%\AutoApplyStudio\`.
3. Unzip the history: `python -c "import gzip,shutil,sys; shutil.copyfileobj(gzip.open(sys.argv[1]), open(sys.argv[2],'wb'))" <file>.jsonl.gz <file>.jsonl`
   into the project folder above, and copy `memory\` next to it.

## What does not move (sign in or enter again)

Windows encrypts these for this one computer, so they are not in this folder:

- ChatGPT in the app's ChatGPT tab (resumes account), and ChatGPT, Jobright and Outlook in the bot browser (Auto Apply >
  Open the bot browser).
- The Telegram bot token, the Discord webhook and the proxy password (Settings).

## Keep in mind

This repository is public: everything in this folder can be read by anyone. Revoke the GitHub token that was used on the
old computer and create a new one.
