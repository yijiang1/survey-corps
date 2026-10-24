# Survey Corps

A small local task manager for macOS that turns Outlook emails into to-dos.

> **This app is for my personal use.** It is shared as-is, with no support, and is tailored to my own setup
> (a work MacBook with Outlook for Mac). Feel free to read or borrow from the code.

## Features

**Emails to tasks**
- Drag an email from Outlook and drop it anywhere in the window to create a task. The subject, sender, date and body are saved, along with a copy of the original email.
- **Open in Outlook** button re-opens the saved email in Outlook.
- Links in email bodies are clickable. Outlook Safe Links / URL Defense wrappers are unwrapped for display (the click still goes through the scanner) and open in your default browser.
- Optional: auto-fill labels and a due date for each dropped email using an LLM (Argonne's Argo gateway; needs the lab network/VPN). Enable it with `data/config.json`: `{"argo_user": "<ANL username>"}` (optional `"argo_model"`, default `claudesonnet55`; if that model errors, it retries once on `claudesonnet5`). If an email (or screenshot) states no deadline, it's due at 5 PM three days after it was sent (hover the due date to see that it was a default). While Argo reads a new task, a status line shows under the search bar; when it's done, the list scrolls to the task and flashes it. It never overwrites labels or a due date you set, and does nothing if the gateway is unreachable. The email text is sent to that gateway.
- **Screenshots to tasks** (Argo's vision model): click the camera button next to the add box, or right-click the Task pet → Screenshot to Task…. The app hides, you drag over an area (Space picks a window, Esc cancels), and Argo reads the image into a task: title, sender, date, the text, plus labels, due date and action items. You can also drop an image file on the window or the Task pet, or ⌘V an image (e.g. one copied with ⌃⇧⌘4). The screenshot shows under **Screenshot & notes**; images live in `data/shots/` and are sent to Argo. The first capture asks for Screen Recording permission (System Settings → Privacy & Security).
- Add plain tasks by typing in the box at the top and pressing Enter. With Argo, plain language works: "review Bob's draft by Fri 3pm" becomes the task "Review Bob's draft", due Friday 15:00, with labels.
- When an email asks for several things, Argo lists them as **action items** under the task; click one to make it its own task (it shares the email).
- **Draft reply** (arrow button): type optional instructions in the field that opens on the card ("decline politely"), then **Draft in Outlook**; Argo writes the reply following them and your notes on the task. It opens as a new Outlook message for you to edit and send, and is also copied to the clipboard. Nothing is sent automatically. It's a new message, not a threaded reply; to keep the thread, open the email, hit Reply and paste.
- **Ask about one email**: under **Email & notes**, type a question in the ✨ Ask Argo box and press Enter ("what's the deadline?"). The email view hides Outlook's external-sender banners and folds the quoted thread behind **Show earlier messages**.
- **Ask**: type a question ending in `?` in the search box and press Enter ("what did Bob ask me about the beamtime?"). Argo answers from your saved emails and tasks.
- **Morning briefing**: once a day after 8:00 (`"briefing_hour"` in `data/config.json`), one notification with what's overdue and due today, and which three to start with.

**Settings** (Survey Corps menu → Settings…, or ⌘,), in its own window; changes save as you make them: Argo username and model, how often to retry work Argo missed, the morning briefing time (or off), the Library's editor app and an optional GitHub token. Saved to `data/config.json`; the token is never shown again after saving.

**Organising**
- Due dates with friendly labels ("Tomorrow 12:33 AM", "Fri 11:26 PM"). Tasks are grouped into Overdue, Today, Tomorrow, Next 7 days, Later and No due date.
- Labels: create new ones or reuse existing ones; each gets its own colour. Click a label to filter by it.
- Search across title, sender, email body, notes and labels (press `/` to focus, `Esc` to clear).
- Notes on each task, editable titles, and a collapsible Done section.

**Scout (second pet, second window)**
- A second pet (black hair, red scarf, spyglass) opens the **Scout** window. Its blue badge counts unread picks.
- Every few hours, it pulls new arXiv papers and GitHub repos matching your keywords, plus new videos from YouTube channels you list. Argo scores each item 0-10 against your interests, with a one-line reason why. Off VPN, items still show up, just unscored, and get scored on a later check.
- 📌 on a card adds a "Read: …" task (label Reading) with a two-line Argo summary in its notes.
- 📚 on a card moves it to the Library (with its labels), where Argo summarises it like any other link. It leaves Scout but still counts as a like for ranking.
- 👍 / 👎 / 📚 on each card feed the next round of ranking. ✕ hides a card without teaching Argo anything (👎 tells Argo you're less interested in things like it). You get a notification when something scores 8 or higher.
- **+ Add** in the Scout window: paste links (arXiv paper, YouTube video, GitHub repo, or any web page) to add them straight to your Library (Scout still scores them). Scout fills in the title, authors and abstract, and Argo scores them like everything else.
- **Interests** button in the Scout window: edit what you're interested in (plain words Argo ranks against), search terms (arXiv + GitHub, plus arXiv-only ones), YouTube channels (paste an `@handle`, a channel link or an ID) and how often to check. Saving starts a new check; **Reset to defaults** goes back to the built-in AI agents / RSI / ptychography setup. Settings are saved to `data/config.json` (optional `github_token` there too); results live in `data/feed.json`. Only titles and abstracts go to Argo.

**Library (third pet, third window)**
- A third pet (blond bob, open book) opens the **Library**: one place for your local projects, files and docs.
- Drag folders or files in from Finder, or use **+ Add** and paste paths (⌥⌘C in Finder copies them). Tick "Add each subfolder of a folder as its own item" and paste `~/Research/Projects` to add every project at once.
- Links work too: paste or drag in a YouTube video, arXiv paper, GitHub repo or any web page (Scout's link reader fills in the title and description). A link straight to a PDF is downloaded into `data/files` and added as a file.
- Each item has a **type**, what it is: Folders, Papers, Docs, Slides, Data, Code, Images, Videos, Websites or Other. It's set automatically from the file type or link (a PDF with an abstract or DOI counts as a paper) and you can change it inline. **Labels** are what it's about (projects, topics), any number per item. Drag an item onto a type or label in the sidebar to change it. **Edit types & labels…** adds, renames (renaming onto an existing name merges), recolours and deletes them.
- With Argo, each new item gets a one-line summary and labels, from its name plus a folder's file list and README, or a file's first few KB (PDFs too, read with macOS's PDFKit; scanned PDFs without text fall back to title and authors). It never overrides what you set; if Argo is down, it retries later.
- **Labels are shared** by all three windows: one vocabulary (suggested as you type), one colour per label (pick it in the Library), and renaming or deleting a label in **Edit types & labels…** applies everywhere. Scout cards take labels too (**+ label** on each card; Argo also tags new finds with your existing labels), and 📌 and 📚 pass them on. Scout items stay in the Scout window; move one to the Library with 📚 to see it there.
- **Label view**: click a label in the Library sidebar to see everything with it on one page: files, folders and links, plus tasks and emails. Click a type as well to narrow it (say, only the emails); the type counts follow the label.
- Search covers names, paths, types, labels, notes, summaries and that contents snippet. Typing filters instantly; press Enter on a question or request in plain words ("slides from my last beamtime talk") and Argo takes it: it searches the Library, your tasks and emails, and Spotlight, reads what looks promising, then answers with links to what it found (up to 6 rounds). Files it finds outside the Library can be added with one click. It only looks in your home folder, never in `~/Library` or dot-folders, and what it reads goes to Argo.
- Buttons on each item: Open, Show in Finder, open in your editor (`"library_editor"` in `data/config.json`, default Visual Studio Code), open in Terminal, copy path, and remove (only from the Library; the file stays on disk). Items that moved are flagged; fix the path under Notes & contents.
- Stored in `data/library.json`. Argo gets the summary inputs above, plus what an ask (`?`) search reads.

**macOS integration**
- macOS notification when a task comes due.
- Native app with a Dock icon, light and dark mode.
- A floating pet (an original chibi character holding a letter) sits on every desktop: click it to show/hide the app, drag it to move it.

**Pets** (all three)
- **Drop things on a pet**: an email or text on the Task pet makes a task, a link on Scout adds it to Scout and the Library, files or folders on the Librarian add them to the Library. The pet leans in while you drag, hops when it worked and shakes when it didn't.
- **Levi, the fourth pet, tracks your coding agents**: Claude Code (terminal, VS Code and the Claude desktop app) and Codex sessions active in the last 12 hours. A green ▸ badge counts the ones working (purple when one needs you), he thinks “…” while any works, and a click opens his window: sessions grouped by machine and project folder, the active ones first (working, waiting, still cached or used in the last hour) with older ones under “Earlier”, a live countdown of each idle session's prompt cache, and a search box. Click a session to jump to it (desktop and Codex sessions open directly; terminal ones bring their app forward). He speaks when a session finishes, asks for permission, or has 5 minutes of cache left. Claude Code reports through hooks in `~/.claude/settings.json` that post each hook's JSON to `/api/agent?tool=cc&pid=$PPID` (desktop sessions skip these and are read from their transcripts and the desktop app's session files). Codex threads come from the Codex app's `~/.codex/state_<n>.sqlite`, and `hooks/codex-notify.sh` goes first in its `notify` setting for the "finished" bubble. Claude Code on SSH machines (e.g. through VS Code Remote-SSH) shows up as “Claude Code @host”, and clicking it opens that folder in VS Code on the host. List the hosts in `data/config.json` as `"remotes": ["user@host", ...]` (passwordless SSH needed). The app keeps a reverse tunnel open from `/tmp/survey-corps-<user>.sock` on each host back to itself. It's a Unix socket, so other users on a shared machine can't reach the app. Every 30 s it also reads each host's recent transcripts over SSH, so sessions show up even when no hook has fired since the app started. The host's `~/.claude/settings.json` gets the same five hooks, each running `curl -s -m 2 --unix-socket /tmp/survey-corps-$USER.sock --data-binary @- "http://localhost/api/agent?tool=cc&host=$USER@$(hostname -f)" >/dev/null 2>&1; true`.
- **Busy**: a "…" thought bubble while Argo works for a pet, and each shows its own work: letters fly out of the Task pet's envelope and turn into ticks, a radar sweeps Scout's magnifier with sparkles, the Librarian turns pages, Levi types code on his laptop.
- **Speech bubbles**: the morning briefing, Scout's new picks, answers and routine results appear above the pet (click to open its window). Each pet has its own sound (Glass, Pop, Bottle, and a blade drawn for Levi; Funk when something failed); turn them off under right-click → All Pets → Sounds. Without the Mac app they fall back to macOS notifications.
- **Right-click** (or ctrl-click) a pet: Task pet: Quick Add, Ask; Scout: Check Now, Save a Link, Mark All Read; Librarian: Find, Add Files. Answers come back as a bubble.
- **Arrange and hide** (right-click a pet → All Pets): **Line Up Here** stacks all four on the edge and half of the screen that pet is on; **Keep Lined Up** re-stacks them whenever you drag one. A pet dropped within 24 pt of a screen edge snaps to it. **Tuck Into the Edge When the Mouse Is Away** slides them into the nearer side edge after 3 s, leaving a sliver showing. If another screen touches that edge, they use the far side instead (then the bottom, then the top), so they never end up on the other screen. They slide out when the mouse comes near or a pet speaks. **Hide Pets** hides them all; ⇧⌘P (Survey Corps menu), the Dock icon or a new speech bubble brings them back.
- **Routines** (Settings → Routines), one per line, `days hour pet: what to do`, e.g. `fri 17 task: summarize what I finished this week`. Days: `daily`, `weekdays` or `mon,wed,fri`. The pet answers in a bubble once that day; right-click it to run one now.

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
