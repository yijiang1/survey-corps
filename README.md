# Survey Corps

A small local task manager for macOS that turns Outlook emails into to-dos.

> **This app is for my personal use.** It is shared as-is, with no support, and is tailored to my own setup
> (a work MacBook with Outlook for Mac). Feel free to read or borrow from the code.

## Features

**Emails to tasks**
- Drag an email from Outlook and drop it anywhere in the window to create a task. The subject, sender, date and body are saved, along with a copy of the original email.
- **Open in Outlook** button re-opens the saved email in Outlook.
- Links in email bodies are clickable. Outlook Safe Links / URL Defense wrappers are unwrapped for display (the click still goes through the scanner) and open in your default browser.
- Optional: auto-fill labels and a due date for each dropped email using an LLM (Argonne's Argo gateway; needs the lab network/VPN). Enable it with `data/config.json`: `{"argo_user": "<ANL username>"}` (optional `"argo_model"`). It never overwrites labels or a due date you set, and does nothing if the gateway is unreachable. The email text is sent to that gateway.
- Add plain tasks by typing in the box at the top and pressing Enter. With Argo, plain language works: "review Bob's draft by Fri 3pm" becomes the task "Review Bob's draft", due Friday 15:00, with labels.
- When an email asks for several things, Argo lists them as **action items** under the task; click one to make it its own task (it shares the email).
- **Draft reply** (arrow button): Argo writes a reply, using your notes on the task as instructions ("decline politely"). It opens as a new Outlook message for you to edit and send, and is also copied to the clipboard. Nothing is sent automatically. It's a new message, not a threaded reply; to keep the thread, open the email, hit Reply and paste.
- **Ask**: type a question ending in `?` in the search box and press Enter ("what did Bob ask me about the beamtime?"). Argo answers from your saved emails and tasks.
- **Morning briefing**: once a day after 8:00 (`"briefing_hour"` in `data/config.json`), one notification with what's overdue and due today, and which three to start with.

**Organising**
- Due dates with friendly labels ("Tomorrow 12:33 AM", "Fri 11:26 PM"). Tasks are grouped into Overdue, Today, Tomorrow, Next 7 days, Later and No due date.
- Labels: create new ones or reuse existing ones; each gets its own colour. Click a label to filter by it.
- Search across title, sender, email body, notes and labels (press `/` to focus, `Esc` to clear).
- Notes on each task, editable titles, and a collapsible Done section.

**Scout (second pet, second window)**
- A second pet (black hair, red scarf, spyglass) opens the **Scout** window. Its blue badge counts unread picks.
- Every few hours, it pulls new arXiv papers and GitHub repos matching your keywords, plus new videos from YouTube channels you list. Argo scores each item 0-10 against your interests, with a one-line reason why. Off VPN, items still show up, just unscored, and get scored on a later check.
- 📌 on a card adds a "Read: …" task (label Reading) with a two-line Argo summary in its notes.
- 👍 / 👎 / ☆ on each card feed the next round of ranking. You get a notification when something scores 8 or higher.
- **+ Add** in the Scout window: paste links (arXiv paper, YouTube video, GitHub repo, or any web page) to save them straight to Saved. Scout fills in the title, authors and abstract, and Argo scores them like everything else.
- **Interests** button in the Scout window: edit what you're interested in (plain words Argo ranks against), search terms (arXiv + GitHub, plus arXiv-only ones), YouTube channels (paste an `@handle`, a channel link or an ID) and how often to check. Saving starts a new check; **Reset to defaults** goes back to the built-in AI agents / RSI / ptychography setup. Settings are saved to `data/config.json` (optional `github_token` there too); results live in `data/feed.json`. Only titles and abstracts go to Argo.

**Library (third pet, third window)**
- A third pet (blond bob, open book) opens the **Library**: one place for your local projects, files and docs.
- Drag folders or files in from Finder, or use **+ Add** and paste paths (⌥⌘C in Finder copies them). Tick "Add each subfolder as its own item" and paste `~/Research/Projects` to add every project at once.
- Each item sits in one **group** (Projects, Papers, Docs, Data to start) and can have any number of coloured **labels**. Change them inline, or drag an item onto a group or label in the sidebar. **Edit groups & labels…** adds, renames (renaming onto an existing name merges), recolours and deletes them.
- With Argo, each new item gets a one-line summary, a group and labels, from its name plus a folder's file list and README, or a file's first few KB (PDFs: title and authors only). It never overrides what you set; if Argo is down, it retries later.
- Search covers names, paths, groups, labels, notes, summaries and that contents snippet. End the search with `?` to ask Argo ("where's my beamtime analysis?").
- Buttons on each item: Open, Show in Finder, open in your editor (`"library_editor"` in `data/config.json`, default Visual Studio Code), open in Terminal, copy path, and remove (only from the Library; the file stays on disk). Items that moved are flagged; fix the path under Notes & contents.
- Stored in `data/library.json`. Only the summary inputs above go to Argo.

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
