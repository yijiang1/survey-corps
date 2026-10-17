# Survey Corps

A small local task manager for macOS that turns Outlook emails into to-dos.

> **This app is for my personal use.** It is shared as-is, with no support, and is tailored to my own setup
> (a work MacBook with Outlook for Mac). Feel free to read or borrow from the code.

## Features

**Emails to tasks**
- Drag an email from Outlook and drop it anywhere in the window to create a task. The subject, sender, date and body are saved, along with a copy of the original email.
- **Open in Outlook** button re-opens the saved email in Outlook.
- Links in email bodies are clickable. Outlook Safe Links / URL Defense wrappers are unwrapped for display (the click still goes through the scanner) and open in your default browser.
- Add plain tasks by typing in the box at the top and pressing Enter.

**Organising**
- Due dates with friendly labels ("Tomorrow 12:33 AM", "Fri 11:26 PM"). Tasks are grouped into Overdue, Today, Tomorrow, Next 7 days, Later and No due date.
- Labels: create new ones or reuse existing ones; each gets its own colour. Click a label to filter by it.
- Search across title, sender, email body, notes and labels (press `/` to focus, `Esc` to clear).
- Notes on each task, editable titles, and a collapsible Done section.

**macOS integration**
- macOS notification when a task comes due.
- Native app with a Dock icon, light and dark mode.
- A floating pet (an original chibi character holding a letter) sits on every desktop: click it to show/hide the app, drag it to move it.

**Privacy**
- Everything stays on the Mac. Data lives in `data/` next to `app.py` (ignored by git); nothing is sent to any cloud service.
- The server only listens on `127.0.0.1` and rejects requests from other websites.

## How it works

- `app.py`: a dependency-free Python 3 server (standard library only) that stores tasks in `data/todos.json`, parses `.eml` files, serves the single-page UI, and sends due-date notifications via `osascript`.
- `mac/SurveyCorps.swift`: a small AppKit app that starts `app.py`, shows the UI in a WebKit window, and receives Outlook drags natively (Outlook hands over emails as file promises, which Chromium browsers don't accept). It also draws the floating pet.
- `mac/build.sh`: compiles the app and installs it to `~/Applications/Survey Corps.app`.

## Setup

Requirements: macOS, Xcode Command Line Tools (`xcode-select --install`, which provides `python3` and `swiftc`), Outlook for Mac.

```bash
bash mac/build.sh
open ~/Applications/"Survey Corps.app"
```

Optional: put any image at `mac/icon-source.png` before building to use it as the app icon (not included in the repo).

Then right-click the Dock icon → **Options → Keep in Dock**, and **Assign To: All Desktops** if you want the window on every desktop.

Without the Mac app, you can also run `python3 app.py`, which opens the UI in Safari (Safari accepts Outlook drags; Chrome/Brave do not).

The app looks for `app.py` relative to where the Swift source was compiled, so rebuild after moving the folder.
