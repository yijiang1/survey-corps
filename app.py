#!/usr/bin/env python3
"""Survey Corps: local todo list fed by emails dragged from Outlook. Run: python3 app.py"""
from collections import Counter
import base64, html, inspect, json, os, re, socket, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
DATA = Path(__file__).resolve().parent / "data"
MAILS = DATA / "mails"   # original .eml copies, opened in Outlook via "Open mail"
INBOX = DATA / "inbox"   # fallback: drag emails from Outlook into this Finder folder
SHOTS = DATA / "shots"   # screenshots turned into tasks
IMAGE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
DB = DATA / "todos.json"
CONFIG = DATA / "config.json"  # optional: {"argo_user": "<ANL username>", "argo_model": "claudesonnet55"}
ARGO = "https://apps.inside.anl.gov/argoapi/v1/chat/completions"
MODEL, BACKUP = "claudesonnet55", "claudesonnet5"  # default Argo model, and the one tried when it errors
BRIEFED = DATA / "briefed"  # date of the last morning briefing
RAN = DATA / "routines_ran.json"  # {routine line: date it last ran}
lock = threading.Lock()
retry_busy = threading.Lock()
argo_ok, argo_at = None, 0.0  # last Argo call: outcome (None = not tried yet) and when; shown in the UI
busy, busy_lock = Counter(), threading.Lock()  # Argo calls in flight, by tag: task id, "ask", "briefing", "scout", "check", "lib…"
bubbles, pets_seen = [], 0.0  # speech bubbles for the Mac app's pets [{"id", "pet", "title", "text"}]; when it last polled


def load():
    return json.loads(DB.read_text()) if DB.exists() else []


def save(todos):
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(todos, indent=1, ensure_ascii=False))
    tmp.replace(DB)  # atomic, so a crash never leaves a half-written db


def email_text(msg):
    try:
        part = msg.get_body(("plain", "html"))
        text = part.get_content() if part else ""
    except Exception:
        return "(could not decode body)"
    if part and part.get_content_type() == "text/html":
        text = re.sub(r"<(style|script|head)\b.*?</\1>", "", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text, flags=re.I)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r".*ZjQcmQRYFpfpt.*\n?", "", text)  # Proofpoint banner markers
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\s*\n\s*\n\s*", "\n\n", text).strip()


def clean_suggestion(text, now):
    """Pull {"labels", "due", "title", "actions"} out of the model's reply, dropping anything malformed or already past."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
    labels = [str(x).strip() for x in d.get("labels") or [] if str(x).strip()][:3]
    due = str(d.get("due") or "")
    try:
        ok = datetime.strptime(due, "%Y-%m-%dT%H:%M") > now
    except ValueError:
        ok = False
    actions = [str(x).strip()[:200] for x in d.get("actions") or [] if str(x).strip()][:8]
    return {"labels": [l for l in labels if len(l) <= 24], "due": due if ok else "",
            "title": str(d.get("title") or "").strip()[:200], "actions": actions if len(actions) > 1 else []}


def config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


SETTINGS = {  # editable in the app's Settings dialog -> (default, max for ints / None for text)
    "argo_user": ("", None), "argo_model": (MODEL, None), "retry_minutes": (10, 1440),
    "briefing_hour": (8, 24), "library_editor": ("Visual Studio Code", None), "github_token": ("", None),
    "routines": ("", None)}


def get_settings():
    cfg = config()
    out = {k: cfg.get(k, d) for k, (d, _) in SETTINGS.items()}
    out["github_token_set"], out["github_token"] = bool(out["github_token"]), ""  # never send the token back
    return out


def set_settings(new):
    with config_lock:
        cfg = config()
        for k, v in new.items():
            if k not in SETTINGS:
                continue
            d, hi = SETTINGS[k]
            v = max(0, min(hi, int(v))) if hi else str(v).strip()
            if k == "github_token" and not v:
                continue  # blank = keep; clear_github_token removes it
            cfg[k] = v if v != "" else d
            if v == "" or v == d and k not in ("retry_minutes", "briefing_hour"):
                cfg.pop(k, None)  # back to the default
        if new.get("clear_github_token"):
            cfg.pop("github_token", None)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False))
        tmp.replace(CONFIG)


def argo_on():
    return bool(config().get("argo_user"))


def argo(prompt, max_tokens, timeout=120, tag="", image=None):
    """Ask the lab's Argo LLM (with an optional image file it can see). Returns the reply text, or None if unconfigured,
    down or unreachable (e.g. no VPN)."""
    global argo_ok, argo_at
    cfg = config()
    if not cfg.get("argo_user"):
        return None
    u = urlparse(ARGO)
    try:  # off VPN the host resolves but connections hang until the timeout: find out in 3 s instead of minutes
        socket.create_connection((u.hostname, u.port or 443), timeout=3).close()
    except OSError:
        argo_ok, argo_at = False, time.time()
        return None
    content = prompt if not image else [{"type": "text", "text": prompt},
                                        {"type": "image_url", "image_url": {"url": data_url(image)}}]
    models = list(dict.fromkeys([cfg.get("argo_model") or MODEL, BACKUP]))
    with busy_lock:
        busy[tag] += 1
    try:
        for i, model in enumerate(models):
            req = urllib.request.Request(ARGO, json.dumps({
                "model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}).encode(),
                {"Content-Type": "application/json", "Authorization": "Bearer " + cfg["argo_user"]})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    text = json.load(r)["choices"][0]["message"]["content"]
                break
            except (urllib.error.HTTPError, KeyError, IndexError, ValueError):  # model errored: try the backup
                if i == len(models) - 1:
                    raise
    except Exception:  # down, 5xx, timeout, off VPN, malformed reply: all the same to callers
        argo_ok = False
        return None
    finally:
        argo_at = time.time()
        with busy_lock:
            busy[tag] -= 1
    argo_ok = True
    return text


def data_url(path):
    """A screenshot as a data: URL, shrunk to the 1568 px Claude reads anyway (so big Retina shots stay small)."""
    small = Path(str(path) + ".send.jpg")
    if subprocess.run(["sips", "-Z", "1568", "-s", "format", "jpeg", str(path), "--out", str(small)],
                      capture_output=True).returncode == 0:
        data, mime = small.read_bytes(), "image/jpeg"
        small.unlink()
    else:  # no sips (not a Mac): send it as is
        data, mime = Path(path).read_bytes(), IMAGE.get(Path(path).suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def check_argo():
    argo("Reply with OK.", 5, timeout=20, tag="check")


def suggest(t, known):
    """Labels, due date and action items for a dropped email (or a cleaned title for a typed task).
    None if Argo didn't answer; {} if it answered nonsense."""
    now = datetime.now()
    labels = ("- labels: up to 3 short labels (1-2 words, Title Case). Reuse an existing label whenever one fits; "
              "invent a new one only if none does.\n")
    if t["eml"] or t.get("shot"):  # a screenshot reads like an email: its date, sender and asks
        prompt = (f"Today is {now:%A %Y-%m-%d %H:%M} (local time). Existing labels: {json.dumps(known)}.\n"
                  "Below is an email I saved as a to-do. Reply with ONLY a JSON object "
                  '{"labels": [...], "due": "...", "actions": [...]}.\n' + labels +
                  '- due: when I must act by, as local YYYY-MM-DDTHH:MM. Resolve relative dates ("by Friday") against the '
                  'email\'s own Date header; if only a date is given use 17:00. Use "" if the email states no deadline '
                  "(do not invent one).\n"
                  "- actions: if the email asks ME to do several distinct things, each one as a short imperative to-do "
                  "(max 12 words); otherwise [].\n\n"
                  f"Subject: {t['title']}\nFrom: {t['sender']}\nDate: {t['date']}\n\n{t['body'][:3000]}")
    else:
        prompt = (f"Today is {now:%A %Y-%m-%d %H:%M} (local time). Existing labels: {json.dumps(known)}.\n"
                  "Below is a to-do I typed. Reply with ONLY a JSON object "
                  '{"title": "...", "labels": [...], "due": "..."}.\n'
                  "- title: the task restated cleanly, without the date/time words, first letter capitalised.\n" + labels +
                  '- due: the deadline it mentions, as local YYYY-MM-DDTHH:MM resolved against today; if only a date is '
                  'given use 17:00. Use "" if it mentions no time (do not invent one).\n\n' + t["body"][:3000])
    reply = argo(prompt, 800, tag="new:" + t["id"])  # "new:": the page shows a status line, then scrolls to the task
    return None if reply is None else clean_suggestion(reply, now)


def new_input(fn):
    """Marks a new task's whole Argo job (read, then labels/due) as one busy "new:<id>" stretch, for the page's status line."""
    def run(t):
        tag = "new:" + t["id"]
        with busy_lock:
            busy[tag] += 1
        try:
            return fn(t)
        finally:
            with busy_lock:
                busy[tag] -= 1
    return run


@new_input
def enrich(t):
    """Background: fill in labels/due on a freshly dropped email, unless the user already set them."""
    s = suggest(t, all_labels())
    with lock:
        todos = load()
        cur = next((x for x in todos if x["id"] == t["id"]), None)
        if not cur or not argo_on():
            return
        if s is None:  # Argo down: retry_argo() tries again later
            cur["retry"] = "enrich"
            return save(todos)
        cur.pop("retry", None)
        s = s or {"labels": [], "due": "", "title": "", "actions": []}  # nonsense reply: still give emails a default due
        if s["labels"] and not cur.get("labels"):
            cur["labels"] = sorted(set(s["labels"]))
        if s["due"] and not cur["due"]:
            cur["due"], cur["notified"] = s["due"], False
        if not cur["due"] and (t["eml"] or t.get("shot")):  # it states no deadline: 3 days after it was sent
            cur["due"], cur["notified"], cur["due_auto"] = default_due(cur), False, True
        if s["title"] and not t["eml"] and cur["title"] == t["title"]:  # typed task the user hasn't renamed
            cur["title"], cur["body"] = s["title"], "" if cur["body"] == t["title"] else cur["body"]
        if s["actions"] and not cur.get("actions"):
            cur["actions"] = s["actions"]
        save(todos)


def default_due(t):
    """17:00, 3 days after the email was sent (its Date header); when that's unreadable, after it was added."""
    try:
        sent = parsedate_to_datetime(t["date"]).astimezone().replace(tzinfo=None)  # in local time
    except (TypeError, ValueError, IndexError):
        sent = datetime.fromisoformat(t["added"])
    return (sent + timedelta(days=3)).strftime("%Y-%m-%dT17:00")


def blank():
    return {"id": uuid.uuid4().hex[:12], "done": False, "due": "", "notified": False, "note": "",
            "added": datetime.now().isoformat(timespec="minutes")}


def make_todo(raw, name):
    t = blank()
    tid = t["id"]
    ext = Path(name).suffix.lower()
    if ext in IMAGE:  # a screenshot: Argo reads it in the background
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / f"{tid}{ext}").write_bytes(raw)
        t.update(title="Screenshot", sender="", date="", body="", eml=False, shot=ext)
        threading.Thread(target=read_shot, args=(t,), daemon=True).start()
        return t
    msg = message_from_bytes(raw, policy=policy.default)
    if name.lower().endswith(".eml") or (msg["From"] and msg["Subject"] is not None):
        (MAILS / f"{tid}.eml").write_bytes(raw)
        text = email_text(msg)
        t.update(title=str(msg["Subject"] or "(no subject)"), sender=str(msg["From"] or ""),
                 date=str(msg["Date"] or ""), body=text.strip()[:3000], eml=True)
    else:  # plain text (manual todo, or Outlook only handed over text)
        text = raw.decode("utf-8", "replace").strip()
        t.update(title=(text.splitlines() or ["(empty)"])[0][:200], sender="", date="",
                 body=text[:3000], eml=False)
    threading.Thread(target=enrich, args=(t,), daemon=True).start()
    return t


def clean_shot(text):
    """{"title", "sender", "date", "text"} from the model's reply; None if it isn't that JSON."""
    try:
        d = json.loads(re.search(r"\{.*\}", text, re.S).group())
    except (AttributeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return {k: str(d.get(k) or "").strip()[:3000 if k == "text" else 200] for k in ("title", "sender", "date", "text")}


@new_input
def read_shot(t):
    """Background: Argo reads a screenshot (an email, chat, calendar invite, web page...) into the task's title and text,
    then labels/due/action items are filled in as for any other task."""
    reply = argo("This is a screenshot I took to make a to-do from. Reply with ONLY a JSON object "
                 '{"title": "...", "sender": "...", "date": "...", "text": "..."}.\n'
                 "- title: what I need to do, as a short imperative to-do (max 12 words); if it's just a message, its subject.\n"
                 '- sender: who it is from (name <email> if shown), or "".\n'
                 '- date: when it was sent or posted, as shown, or "".\n'
                 "- text: all the readable text that matters (message body, deadlines, links, names), transcribed as "
                 "plain text; skip window chrome, menus and toolbars.", 3000, tag="new:" + t["id"], image=SHOTS / f"{t['id']}{t['shot']}")
    s = None if reply is None else clean_shot(reply)
    with lock:
        todos = load()
        cur = next((x for x in todos if x["id"] == t["id"]), None)
        if not cur or not argo_on():
            return
        if reply is None:  # Argo down: retry_argo() tries again later
            cur["retry"] = "shot"
            return save(todos)
        cur.pop("retry", None)
        if s:
            if cur["title"] == "Screenshot" and s["title"]:  # unless the user renamed it meanwhile
                cur["title"] = s["title"]
            cur.update(sender=s["sender"], date=s["date"], body=s["text"])
        save(todos)
        t = dict(cur)
    if s:
        enrich(t)


def summarize(t):
    """Background: a 2-line summary of a Scout reading task in its note, unless the user has written one meanwhile."""
    text = argo("In 2 short lines: what is this and why might it matter to me?\n"
                f"My interests: {({**SCOUT, **config()})['scout_profile']}\n\n{t['title'].removeprefix('Read: ')}\n{t['body']}", 600, tag=t["id"])
    with lock:
        todos = load()
        cur = next((x for x in todos if x["id"] == t["id"]), None)
        if not cur or not argo_on():
            return
        if text is None:  # Argo down: retry_argo() tries again later
            cur["retry"] = "summary"
        else:
            cur.pop("retry", None)
            if not cur["note"]:
                cur["note"] = text.strip()
        save(todos)


def retry_argo():
    """Redo Argo work that failed while it was down. Stops at the first failure: still down, try next round."""
    if not retry_busy.acquire(blocking=False):
        return
    try:
        with lock:
            jobs = [({"summary": summarize, "shot": read_shot}.get(t["retry"], enrich), t)
                    for t in load() if t.get("retry") and not t["done"]]
        with lib_lock:
            items = load_lib()["items"]
        jobs += [(lib_enrich, x["id"]) for x in items if x.get("retry")]
        # links for items from before Related existed, or whose link pass failed: a few per round
        jobs += [(lib_relate, x["id"]) for x in items if "related" not in x and not x.get("retry")][:RELATE_ROUND]
        for fn, arg in jobs:
            fn(arg)
            if not argo_ok:
                break
        if argo_on() and time.time() - argo_at > 600:  # idle: ping it so the status in the UI stays current
            check_argo()
    finally:
        retry_busy.release()


def draft_reply(t, how=""):
    """Argo drafts a reply (following `how`, if given); it opens as a new Outlook message (never sent) and is copied to the clipboard."""
    msg = message_from_bytes((MAILS / f"{t.get('mail', t['id'])}.eml").read_bytes(), policy=policy.default)
    text = argo("Draft my reply to the email below. Write only the reply body: plain text, no subject line, no signature, "
                "match the sender's tone and language, keep it short."
                + (f" My notes on how to answer: {t['note']}" if t["note"] else "")
                + (f" Instructions for this reply: {how}" if how else "")
                + f"\n\nFrom: {t['sender']}\nDate: {t['date']}\nSubject: {msg['Subject']}\n\n{t['body']}", 1500, timeout=90, tag=t["id"])
    if not text:
        return None
    text = text.strip()
    subprocess.run(["pbcopy"], input=text.encode())
    subj = str(msg["Subject"] or "")
    subj = subj if subj.lower().startswith("re:") else "Re: " + subj
    url = (f"mailto:{urllib.parse.quote(parseaddr(str(msg['Reply-To'] or msg['From'] or ''))[1])}?"
           + urllib.parse.urlencode({"subject": subj, "body": text}, quote_via=urllib.parse.quote))
    if subprocess.run(["open", "-a", "Microsoft Outlook", url]).returncode:
        subprocess.run(["open", url])
    return text


STOP = set("a an the and or of to in on at for with about what who whom when where which how did does do is are was "
           "were be been i me my we our you your it its this that these those from by any all there have has had "
           "ask asked tell told say said".split())


def top_hits(items, q, hay, n=12):
    """The n items whose text (hay) mentions the question's keywords most."""
    words = [w for w in re.findall(r"\w+", q.lower()) if w not in STOP and len(w) > 2]
    # ponytail: keyword-overlap ranking over <= a few thousand items; embeddings if it misses obvious matches
    return sorted(items, key=lambda t: -sum(hay(t).lower().count(w) for w in words))[:n]


def ask(q):
    """Answer a question from saved emails/tasks: keyword-pick the likeliest ones, then let Argo read them."""
    with lock:
        todos = load()
    hits = top_hits(todos, q, lambda t: " ".join([t["title"], t["sender"], t["date"], t["body"], t["note"], *t.get("labels", [])]))
    ctx = "\n\n".join(f"[{t['title']}] from {t['sender'] or '-'} | {t['date'] or t['added']} | due {t['due'] or 'none'} | "
                       f"{'done' if t['done'] else 'open'}\n{t['body'][:1500]}" + (f"\nMy notes: {t['note']}" if t["note"] else "")
                       for t in hits)
    return argo(f"Today is {datetime.now():%A %Y-%m-%d}. Answer my question using only the saved emails/tasks below. "
                "Be brief and plain text; name the item (by its [subject]) the answer comes from. "
                f"If they don't contain the answer, say so.\n\nQuestion: {q}\n\n{ctx}", 2000, timeout=90, tag="ask")


def ask_mail(t, q):
    """Answer a question about one saved email."""
    return argo(f"Today is {datetime.now():%A %Y-%m-%d}. Answer my question about the email below. Be brief and plain text. "
                f"If the email doesn't say, say so.\n\nQuestion: {q}\n\nFrom: {t['sender']}\nDate: {t['date']}\n"
                f"Subject: {t['title']}\n\n{t['body']}", 1500, timeout=90, tag=t["id"])


def briefing():
    """Once a day after briefing_hour (default 8): one notification with what's overdue/due today and where to start."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < config().get("briefing_hour", 8) or (BRIEFED.exists() and BRIEFED.read_text() == today):
        return
    BRIEFED.write_text(today)
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    with lock:
        todos = [t for t in load() if not t["done"] and t["due"]]
    late = sorted((t for t in todos if t["due"] <= stamp), key=lambda t: t["due"])
    due = sorted((t for t in todos if t["due"] > stamp and t["due"].startswith(today)), key=lambda t: t["due"])
    if not late and not due:
        return
    head = ", ".join(x for x in (f"{len(late)} overdue" if late else "", f"{len(due)} due today" if due else "") if x)
    lines = "\n".join(f"- {t['title']} (due {t['due']}{', OVERDUE' if t in late else ''})" for t in (late + due)[:30])
    def send():  # in its own thread, so a slow or down Argo never holds up due-date notifications
        tip = argo(f"It's {now:%A %H:%M}. These are my overdue and due-today tasks. In under 25 words, tell me which 3 to "
                   "start with (short names only). Plain text, no preamble.\n" + lines, 300, timeout=60, tag="briefing")
        say("task", f"Good morning: {head}", (tip or "Start with: " + "; ".join(t["title"] for t in (late + due)[:3])).strip())
    threading.Thread(target=send, daemon=True).start()


def say(pet, title, text):
    """Speech bubble from a pet ("task", "scout", "library"); a macOS notification if the Mac app isn't running."""
    for _ in range(20):  # right after launch the app hasn't polled yet: give it 10 s
        if pets_seen:
            break
        time.sleep(0.5)
    if time.time() - pets_seen > 60:
        return notify(title, text)
    with busy_lock:
        bubbles.append({"id": (bubbles[-1]["id"] + 1) if bubbles else 1, "pet": pet, "title": title, "text": text})
        del bubbles[:-20]


# ---- Coding agents on this Mac: Claude Code reports through hooks, Codex is read from its session logs ----
# ponytail: in memory, so a restart forgets idle Claude sessions until their next prompt
cc = {}  # {session_id: {"cwd", "state", "since", "pid"}}
cc_ended = set()  # sessions that sent SessionEnd: their transcript may still be fresh, but they're gone
CLAUDE = Path.home() / ".claude" / "projects"
transcripts = {}  # path -> (mtime, transcript_info), so a poll re-reads only transcripts that changed
CC_STATE = {"SessionStart": "idle", "UserPromptSubmit": "working", "Notification": "waiting", "Stop": "idle"}
CODEX = Path.home() / ".codex"  # state_<n>.sqlite: the Codex app's thread list
WINDOW = 12 * 3600  # sessions active this recently are listed
DESKTOP = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
desktop, desktop_at = {}, 0.0  # Claude Code session id -> {"id": local_…, "title": sidebar title}; when last read


def agent_event(e, tool="cc", pid=0, host=""):
    """A Claude Code hook payload (tool "cc") or a Codex notify payload; a bubble when one finishes or needs you.
    host: "user@host" when the hook ran on a remote machine (through tunnel()); its pid and transcript aren't ours."""
    where = Path(e.get("cwd") or "?").name
    if tool == "codex":
        src = codex_query("select thread_source from threads where id = ?", (str(e.get("thread-id")),))
        if src and src[0][0] != "user":
            return  # Codex's own Guardian review of an approval, which finishes right after your prompt; not your thread
        if e.get("type") == "agent-turn-complete":
            msg = flat(str(e.get("last-assistant-message") or ""), 90)
            threading.Thread(target=say, args=("levi", "Codex", f"Finished in {where}" + (f": {msg}" if msg else "")), daemon=True).start()
        return
    sid, ev = e.get("session_id"), e.get("hook_event_name")
    if ev == "SessionEnd":
        cc_ended.add(sid)
        return cc.pop(sid, None)
    if ev == "Notification" and (e.get("notification_type") == "idle_prompt" or "waiting for your input" in str(e.get("message"))):
        return  # the 60 s "still idle" reminder after Stop, not a question
    if not sid or ev not in CC_STATE:
        return
    if sid not in cc:
        cwd = e.get("cwd") or ""
        cc[sid] = {"cwd": cwd, "state": "", "since": 0, "pid": 0 if host else pid, "host": host,
                   "tool": remote_tool(host) if host else "Claude Code", "open": remote_open(host, cwd) if host else app_of(pid)}
    a = cc[sid]
    if a["state"] != CC_STATE[ev]:
        a["state"], a["since"] = CC_STATE[ev], time.time()
    if ev in ("Stop", "Notification") and e.get("transcript_path") and not host:
        a["cached_until"] = cache_expiry(Path(e["transcript_path"])) or a.get("cached_until", 0)
    if ev in ("Stop", "Notification"):
        text = f"Finished in {where}" if ev == "Stop" else f"Needs you in {where}: {flat(str(e.get('message') or ''), 90)}"
        threading.Thread(target=say, args=("levi", a.get("tool", "Claude Code"), text), daemon=True).start()


def transcript_info(transcript):
    """{"cwd", "title", "entry", "last", "cached_until"} from the tail of a Claude Code transcript. last: the newest
    timestamped entry (the desktop app also rewrites a file's metadata when you only open it, so mtime isn't activity).
    cached_until: when its prompt cache runs out = last API call + the TTL that call wrote (1 h or 5 min); 0 if unknown."""
    out = {"cwd": "", "title": "", "entry": "", "last": 0, "cached_until": 0}
    try:
        with transcript.open("rb") as f:
            head = f.read(65536).decode(errors="ignore")  # cwd: where it started (a session can cd away later)
            f.seek(max(0, f.seek(0, 2) - 300_000))  # the tail is enough: the title repeats, the last call is last
            lines = f.read().decode(errors="ignore").splitlines()
    except OSError:
        return out
    for line in reversed(lines):
        try:
            d = json.loads(line)
            out["title"] = out["title"] or d.get("customTitle") or d.get("aiTitle") or ""  # yours, else Claude's
            out["cwd"], out["entry"] = out["cwd"] or d.get("cwd") or "", out["entry"] or d.get("entrypoint") or ""
            if not out["last"] and d.get("timestamp"):
                out["last"] = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
            if not out["cached_until"] and d.get("type") == "assistant" and d["message"].get("usage"):
                made = d["message"]["usage"].get("cache_creation") or {}
                ttl = 300 if made.get("ephemeral_5m_input_tokens") and not made.get("ephemeral_1h_input_tokens") else 3600
                out["cached_until"] = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp() + ttl
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if all(out.values()):
            break
    start = re.search(r'"cwd":\s*("(?:[^"\\]|\\.)*")', head)
    out["cwd"] = json.loads(start[1]) if start else out["cwd"]
    return out


def remote_tool(host):
    return f"Claude Code @{host.split('@')[-1].split('.')[0]}"


def remote_open(host, cwd):
    return f"vscode://vscode-remote/ssh-remote+{host}{urllib.parse.quote(cwd)}"


remote = {}  # "user@host" -> [{"id", "mtime", **transcript_info}]: that host's recent transcripts, from remote_scan()
REMOTE_SCAN = inspect.getsource(transcript_info) + f"""
import json, re, time
from datetime import datetime
from pathlib import Path
out = []
for p in (Path.home() / ".claude" / "projects").glob("*/*.jsonl"):
    m = p.stat().st_mtime
    if time.time() - m < {WINDOW}:
        out.append(dict(id=p.stem, mtime=m, **transcript_info(p)))
print(json.dumps(out))
"""


def remote_scan(target):
    """Reads an SSH host's recent Claude Code transcripts with transcript_info() run there, so its sessions show
    even when no hook has reported since this server started (or since they started, if before the hooks).
    Keeps the last result when the host is unreachable: those rows just age."""
    try:
        r = subprocess.run(["ssh", "-x", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", target, "python3 -"],
                           input=REMOTE_SCAN, capture_output=True, text=True, timeout=60)
        remote[target] = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, ValueError):
        pass


def cache_expiry(transcript):
    return transcript_info(transcript)["cached_until"]


def app_of(pid):
    """file:// URL of the app a process runs under (Terminal, iTerm, VS Code…), found by walking up its parents."""
    for _ in range(15):
        if pid <= 1:
            return ""
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout.split(None, 1)
        if len(out) < 2:
            return ""
        if ".app/" in out[1]:
            return Path(out[1][:out[1].index(".app/") + 4]).as_uri()
        pid = int(out[0])
    return ""


def desktop_session(cli_id):
    """{"id", "title"} of a Claude desktop session, from the desktop app's own session files (re-read each minute,
    so sidebar renames show). Its title is the sidebar's; the transcript's can be stale, e.g. a fork keeps the parent's."""
    global desktop, desktop_at
    if time.time() - desktop_at > 60:
        found = {}
        for f in DESKTOP.glob("*/*/local_*.json"):
            try:
                d = json.loads(f.read_text())
                found[d["cliSessionId"]] = {"id": d["sessionId"], "title": d.get("title") or ""}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        desktop, desktop_at = found, time.time()
    return desktop.get(cli_id, {})


def codex_query(sql, args=()):
    """Rows from the Codex app's thread list, read-only; [] if Codex isn't installed or its schema changed."""
    dbs = sorted(CODEX.glob("state_*.sqlite"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if not dbs:
        return []
    # mode=ro can't open a WAL database while Codex is closed (no -shm to read); then nothing writes it, so immutable is safe
    for mode in ("mode=ro", "immutable=1"):
        try:
            db = sqlite3.connect(f"file:{dbs[-1]}?{mode}", uri=True)
            try:
                return db.execute(sql, args).fetchall()
            finally:
                db.close()
        except sqlite3.OperationalError as e:
            if "unable to open" not in str(e):
                return []
        except sqlite3.Error:
            return []
    return []


def codex_threads(since):
    """[(id, title, cwd, rollout path, last active ms)]: your unarchived Codex threads active since `since`
    (not Codex's own Guardian-review subthreads)."""
    return codex_query("select id, coalesce(nullif(name, ''), nullif(title, ''), cwd), cwd, rollout_path,"
                       " max(updated_at_ms, recency_at_ms) as ms from threads"
                       " where thread_source = 'user' and archived = 0 and ms > ?", (since * 1000,))


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        pass
    return True


def row(now, tool, where, state, age, open_, host, cwd, sid="", until=0):
    """One session for Levi. host: "user@host" if remote; until: when its prompt cache runs out (shown when idle)."""
    return {"tool": tool, "where": where, "state": state, "mins": int(age) // 60, "open": open_,
            "host": host.split("@")[-1].split(".")[0], "dir": cwd,
            **({"cache": max(0, int(until - now)) // 60, "id": sid, "until": until}
               if until and state != "working" else {})}


def agents(now=None):
    """[row()]: Claude Code sessions with hooks are exact; other Claude sessions (the desktop app's, or ones started
    before the hooks, or since this server started, on remote hosts) and Codex threads come from their logs:
    active within WINDOW, working = written in the last minute. Hooked rows still take title and folder from the log."""
    now = now or time.time()
    for sid in [s for s, a in cc.items() if a["pid"] and not alive(a["pid"])]:
        cc.pop(sid, None)  # closed without SessionEnd (killed terminal, crash)
    for sid in [s for s, a in cc.items() if not a["pid"] and a["state"] != "working" and now - a["since"] > WINDOW]:
        cc.pop(sid, None)  # remote: no pid to check, so one quiet for WINDOW is taken as gone
    logs = {}  # session id -> (transcript_info, mtime, "user@host" or "")
    for p in CLAUDE.glob("*/*.jsonl"):  # every Claude Code session, desktop app included
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if now - mtime > WINDOW:
            continue
        if transcripts.get(p, (0,))[0] != mtime:
            transcripts[p] = (mtime, transcript_info(p))
        logs[p.stem] = (transcripts[p][1], mtime, "")
    for target, found in remote.items():  # ponytail: hosts sharing a home (NFS) list the same logs; the first host's name wins
        for t in found:
            logs.setdefault(t["id"], (t, t["mtime"], target))
    out = []
    for sid, a in cc.items():
        t, host = logs.get(sid, ({}, 0, ""))[0], a.get("host", "")
        if a["state"] == "waiting" and t.get("last", 0) > a["since"] + 5:  # answered (or auto mode took it): no hook says so
            a["state"], a["since"] = "working", t["last"]
        cwd = t.get("cwd") or a["cwd"]  # where it started: a session can cd away
        out.append(row(now, a.get("tool", "Claude Code"), t.get("title") or Path(cwd).name, a["state"], now - a["since"],
                       remote_open(host, cwd) if host else a.get("open", ""), host, cwd, sid,
                       a.get("cached_until") or t.get("cached_until", 0)))
    for sid, (t, mtime, host) in logs.items():
        age = now - (t["last"] or mtime)
        if sid in cc or sid in cc_ended or t["entry"].startswith("sdk") or age > WINDOW:
            continue  # tracked by hooks, closed, `claude -p` and scripted runs, or only opened, not used
        dk = desktop_session(sid) if t["entry"] == "claude-desktop" else {}
        out.append(row(now, remote_tool(host) if host else "Claude app" if t["entry"] == "claude-desktop" else "Claude Code",
                       dk.get("title") or t["title"] or Path(t["cwd"]).name or "?", "working" if age < 60 else "idle", age,
                       remote_open(host, t["cwd"]) if host else f"claude://claude.ai/epitaxy/{dk['id']}" if dk else "",
                       host, t["cwd"], sid, t["cached_until"]))
    for tid, name, cwd, rollout, ms in codex_threads(now - WINDOW):
        try:
            last = max(ms / 1000, Path(rollout).stat().st_mtime)  # the log moves while a turn runs
        except (OSError, TypeError):
            last = ms / 1000
        out.append(row(now, "Codex", name, "working" if now - last < 60 else "idle", now - last, f"codex://threads/{tid}", "", cwd or ""))
    order = {"waiting": 0, "working": 1}
    return sorted(out, key=lambda a: (order.get(a["state"], 2), a["mins"]))


warned = set()  # (session id, cache expiry) already warned about


def warn_caches(now=None):
    """Levi speaks once per cache period when an idle session's prompt cache has 5 minutes or less left."""
    now = now or time.time()
    for a in agents(now):
        left = a.get("until", 0) - now
        if 0 < left <= 300 and (a["id"], a["until"]) not in warned:
            warned.add((a["id"], a["until"]))
            say("levi", a["tool"], f"{a['where']}: cache expires in {max(1, round(left / 60))} min")


def agents_loop():
    while True:
        try:
            for target in config().get("remotes", []):
                remote_scan(target)
            warn_caches()
        except Exception as e:  # a bad transcript must not stop the warnings for good
            print("agents:", e)
        time.sleep(30)


def tunnel(target):
    """Keeps /tmp/survey-corps-<user>.sock on an SSH host (config "remotes": ["user@host", ...]) forwarded to this
    server, so Claude Code hooks there can report (hooks/README). A Unix socket, mode 0600: other users on a shared
    machine can't reach this server, as they could a forwarded TCP port. Retries each minute (off VPN, asleep)."""
    ssh = ["ssh", "-x", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    while True:
        # sshd leaves the socket behind on disconnect, and a stale one blocks the next forward
        r = subprocess.run(ssh + [target, 'rm -f /tmp/survey-corps-"$USER".sock; echo "$USER"'],
                           capture_output=True, text=True)
        user = r.stdout.strip()
        if r.returncode == 0 and user:
            # `cat` on our stdin, not -N: if this server dies, the pipe closes and the tunnel ends with it
            subprocess.Popen(ssh + ["-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
                                    "-R", f"/tmp/survey-corps-{user}.sock:127.0.0.1:{PORT}", target, "cat >/dev/null"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()
        time.sleep(60)


def notify(title, msg):
    subprocess.run(["osascript", "-e", "on run argv",
                    "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "default"',
                    "-e", "end run", title, msg])


ROUTINE = re.compile(r"(daily|weekdays|(?:mon|tue|wed|thu|fri|sat|sun)(?:,(?:mon|tue|wed|thu|fri|sat|sun))*)\s+(\d{1,2})\s+"
                     r"(task|scout|library):\s*(.+)", re.I)


def routines():
    """Settings' Routines box, one per line: "fri 17 task: summarize what I finished this week". Bad lines are skipped."""
    out = []
    for line in config().get("routines", "").splitlines():
        m = ROUTINE.fullmatch(line.strip())
        if m and int(m[2]) < 24:
            out.append({"line": line.strip(), "days": m[1].lower(), "hour": int(m[2]), "pet": m[3].lower(), "prompt": m[4]})
    return out


def run_routine(r):
    """Argo answers the routine's prompt from that pet's data; the answer comes back as the pet's speech bubble."""
    now = datetime.now()
    if r["pet"] == "task":
        with lock:
            todos = load()
        week = (now - timedelta(days=7)).isoformat(timespec="minutes")
        todos = [t for t in todos if not t["done"] or t.get("done_at", "") >= week][:80]
        ctx = "\n".join(f"- {t['title']} | {'done ' + t.get('done_at', '') if t['done'] else 'open'} | due {t['due'] or 'none'} | "
                        f"added {t['added']} | {', '.join(t.get('labels', []))}" for t in todos)
        what = "my open tasks and those I finished in the last 7 days"
    elif r["pet"] == "scout":
        with feed_lock:
            items = load_feed()["items"]
        items = sorted((x for x in items if x["added"] >= (now - timedelta(days=7)).isoformat()), key=lambda x: -(x["score"] or 0))[:60]
        ctx = "\n".join(f"- [{x['src']}] {x['title']} | score {x['score']} | {x['why']}" + (" | saved" if x["saved"] else "") for x in items)
        what = "papers, videos and repos my Scout found in the last 7 days, scored 0-10 for me"
    else:
        with lib_lock:
            items = load_lib()["items"]
        ctx = "\n".join(f"- {x['name']} | {x['path']} | {x['group']} | opened {x.get('opened', 'never')} | {x['summary']}" for x in items[:150])
        what = "my library of local projects, files and docs"
    text = argo(f"It's {now:%A %Y-%m-%d %H:%M}. Below are {what}. {r['prompt']}\n"
                "Answer briefly (under 80 words), plain text, no preamble.\n\n" + ctx, 600, timeout=90,
                tag="lib:routine" if r["pet"] == "library" else "scout" if r["pet"] == "scout" else "routine")
    say(r["pet"], r["prompt"][:60], (text or "Argo is unavailable right now (down, or off VPN).").replace("**", "").strip())


def due_routines(now=None):
    """Routines whose day and hour have come and that haven't run today; marks them as run."""
    now, ran = now or datetime.now(), json.loads(RAN.read_text()) if RAN.exists() else {}
    today, day = now.strftime("%Y-%m-%d"), now.strftime("%a").lower()
    out = [r for r in routines() if ran.get(r["line"]) != today and now.hour >= r["hour"]
           and (r["days"] == "daily" or r["days"] == "weekdays" and now.weekday() < 5 or day in r["days"].split(","))]
    if out:
        RAN.write_text(json.dumps({**ran, **{r["line"]: today for r in out}}))
    return out


def watcher():
    last_retry = 0
    while True:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        with lock:
            todos, changed = load(), False
            for f in sorted(INBOX.glob("*.eml")):
                todos.insert(0, make_todo(f.read_bytes(), f.name))
                f.unlink()
                changed = True
            for t in todos:
                if t["due"] and not t["done"] and not t["notified"] and t["due"] <= now:
                    notify("Todo due", t["title"])
                    t["notified"] = changed = True
            if changed:
                save(todos)
        briefing()
        for r in due_routines():
            threading.Thread(target=run_routine, args=(r,), daemon=True).start()
        mins = config().get("retry_minutes", 10)  # 0 = automatic retry off (the header's Retry button still works)
        if mins and time.time() - last_retry > mins * 60:  # and right after launch
            last_retry = time.time()
            threading.Thread(target=retry_argo, daemon=True).start()
        time.sleep(20)


# ---- Scout: new arXiv papers, YouTube videos and GitHub repos, ranked by Argo against your interests ----
FEED = DATA / "feed.json"  # {"last": "...", "errors": [...], "seen": [ids], "items": [...]}
feed_lock, scout_busy = threading.Lock(), threading.Lock()
SCOUT = {  # defaults; edited in the Scout window ("Interests"), which saves overrides to data/config.json
    "scout_profile": "Two equally important areas. (1) AI/LLM agents (agent architectures, tool use, autonomous research agents, "
                     "agent benchmarks) and recursive self-improvement (RSI): self-improving and self-evolving agents and models. "
                     "(2) Ptychography (X-ray and electron), including reconstruction algorithms and software.",
    "scout_keywords": ["recursive self-improvement", "self-improving agent", "self-evolving agent", "autonomous research agent",
                       "ptychography"],                       # arXiv + GitHub
    "scout_arxiv_keywords": ["LLM agent", "AI agent", "agentic"],  # arXiv only (too noisy as GitHub searches)
    "scout_youtube": ["UCMLtBahI5DMrt0NPvDSoIRQ Machine Learning Street Talk", "UCNJ1Ymd5yFuUPtn21xtRbbw AI Explained",
                      "UCXl4i9dYBrFOabk0xGmbkRA Dwarkesh Patel", "UCxBcwypKK-W3GHd_RZ9FZrQ Latent Space",
                      "UC55ODQSvARtgSyc8ThfiepQ Sam Witteveen", "UCbfYPyITQ-7l4upoX8nvctg Two Minute Papers",
                      "UC_x36zCEGilGpB1m-V4gmjg IndyDevDan"],  # "<channel id> <name>"
    "scout_hours": 3,  # how often to check
}
config_lock = threading.Lock()


def channel(line):
    """A YouTube channel line: "UC... name" is kept; an @handle or channel URL is looked up to "UC... name"."""
    tok = line.split()[0]
    if re.fullmatch(r"UC[\w-]{22}", tok):
        return " ".join(line.split())
    url = tok if tok.startswith("http") else "https://" + tok if "youtube.com" in tok else "https://www.youtube.com/@" + tok.lstrip("@")
    page = fetch(url, BROWSER).decode("utf-8", "replace")
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
    if not m:
        raise ValueError(f"no YouTube channel found for {tok}")
    name = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    return m.group(1) + (" " + html.unescape(name.group(1)) if name else "")


def clean_settings(d):
    """Validated scout_* overrides from the Interests form (YouTube lines are resolved separately)."""
    lines = lambda k: [" ".join(str(x).split()) for x in d.get(k) or [] if str(x).strip()][:30]
    out = {k: lines(k) for k in ("scout_keywords", "scout_arxiv_keywords", "scout_youtube")}
    out["scout_profile"] = " ".join(str(d.get("scout_profile") or "").split())[:3000] or SCOUT["scout_profile"]
    try:
        out["scout_hours"] = max(1, min(48, int(d.get("scout_hours") or 3)))
    except (TypeError, ValueError):
        out["scout_hours"] = 3
    return out


def save_settings(d):
    """Save the form (or {"reset": true} to go back to the defaults). Returns an error message, or None."""
    if d.get("reset"):
        new = {}
    else:
        new = clean_settings(d)
        bad = []
        for i, line in enumerate(new["scout_youtube"]):
            try:
                new["scout_youtube"][i] = channel(line)
            except Exception as e:
                bad.append(f"{line} ({e})")
        if bad:
            return "Couldn't find YouTube channel: " + "; ".join(bad)
    with config_lock:
        cfg = {k: v for k, v in config().items() if not k.startswith("scout_")}
        cfg.update(new)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False))
        tmp.replace(CONFIG)
    return None
ATOM, YT, MEDIA = "{http://www.w3.org/2005/Atom}", "{http://www.youtube.com/xml/schemas/2015}", "{http://search.yahoo.com/mrss/}"


def load_feed():
    return json.loads(FEED.read_text()) if FEED.exists() else {"last": "", "errors": [], "seen": [], "items": []}


def save_feed(f):
    tmp = FEED.with_suffix(".tmp")
    tmp.write_text(json.dumps(f, indent=1, ensure_ascii=False))
    tmp.replace(FEED)


BROWSER = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en", "Cookie": "CONSENT=YES+1"}  # pages that serve bots a stub or a consent wall


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "survey-corps", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def flat(s, n=1200):
    return " ".join((s or "").split())[:n]


def parse_arxiv(xml):
    out = []
    for e in ET.fromstring(xml).iter(ATOM + "entry"):
        aid = re.sub(r"v\d+$", "", e.findtext(ATOM + "id", "").split("/abs/")[-1])
        authors = [a.findtext(ATOM + "name", "") for a in e.findall(ATOM + "author")]
        out.append({"id": "arxiv:" + aid, "src": "arXiv", "title": flat(e.findtext(ATOM + "title")),
                    "url": "https://arxiv.org/abs/" + aid, "date": e.findtext(ATOM + "published", "")[:10],
                    "by": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                    "text": flat(e.findtext(ATOM + "summary"))})
    return out


def parse_youtube(xml):
    return [{"id": "yt:" + e.findtext(YT + "videoId"), "src": "YouTube", "title": flat(e.findtext(ATOM + "title")),
             "url": "https://www.youtube.com/watch?v=" + e.findtext(YT + "videoId"),
             "by": e.findtext(f"{ATOM}author/{ATOM}name", ""), "date": e.findtext(ATOM + "published", "")[:10],
             "text": flat(e.findtext(f"{MEDIA}group/{MEDIA}description"))}
            for e in ET.fromstring(xml).iter(ATOM + "entry")]


def parse_youtube_page(page):
    """Videos from a channel's /videos page (its embedded ytInitialData), used when the RSS feed fails."""
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not m:
        raise ValueError("no video list on the channel page")
    name = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    by, found = html.unescape(name.group(1)) if name else "", []

    def walk(o):
        if isinstance(o, dict):
            v = o.get("lockupViewModel")
            if v and v.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
                found.append(v)
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(json.loads(m.group(1)))
    out = []
    for v in found[:15]:
        md = v["metadata"]["lockupMetadataViewModel"]
        parts = [p.get("accessibilityLabel") or p["text"]["content"]
                 for r in md.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                 for p in r.get("metadataParts", []) if "text" in p]
        # ponytail: the page only says "4 days ago", so the date is approximate
        age = next((re.match(r"(\d+) (minute|hour|day|week|month|year)s? ago", x) for x in parts if x.endswith(" ago")), None)
        days = int(age.group(1)) * {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[age.group(2)] if age else 0
        out.append({"id": "yt:" + v["contentId"], "src": "YouTube", "title": flat(md["title"]["content"]),
                    "url": "https://www.youtube.com/watch?v=" + v["contentId"], "by": by,
                    "date": (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"), "text": ""})
    return out


def youtube(c):
    """A channel's latest videos: the RSS feed, or the channel page when YouTube's feed is failing (it often 404s)."""
    try:
        return parse_youtube(fetch("https://www.youtube.com/feeds/videos.xml?channel_id=" + c))
    except Exception:
        page = fetch(f"https://www.youtube.com/channel/{c}/videos",
                     BROWSER)
        return parse_youtube_page(page.decode("utf-8", "replace"))


def parse_github(data):
    return [{"id": "gh:" + r["full_name"], "src": "GitHub", "title": r["full_name"], "url": r["html_url"],
             "by": f"★ {r['stargazers_count']}" + (f" · {r['language']}" if r.get("language") else ""),
             "date": r["pushed_at"][:10], "text": flat(r.get("description"))}
            for r in json.loads(data)["items"]]


def page_meta(page):
    """{name or property: [contents]} from a page's <meta> tags, in either attribute order."""
    out = {}
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        a = {k.lower(): html.unescape(v) for k, v in re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', tag)}
        k = a.get("name") or a.get("property")
        if k and "content" in a:
            out.setdefault(k.lower(), []).append(a["content"])
    return out


def link_item(url):
    """A feed item for a pasted link: an arXiv paper, YouTube video, GitHub repo, or any other web page."""
    url = url.strip()
    url = url if re.match(r"https?://", url) else "https://" + url
    today = datetime.now().strftime("%Y-%m-%d")
    m = re.search(r"arxiv\.org/(?:abs|pdf|html)/(.+?)(?:v\d+)?(?:\.pdf)?/?(?:[?#].*)?$", url)
    if m:  # the abstract page (not the rate-limited export API) has everything in citation_* tags
        aid, url = m.group(1), "https://arxiv.org/abs/" + m.group(1)
        meta = page_meta(fetch(url, BROWSER).decode("utf-8", "replace"))
        one = lambda *ks: next((meta[k][0] for k in ks if meta.get(k)), "")
        authors = [" ".join(reversed(a.split(", ", 1))) for a in meta.get("citation_author", [])]  # "Last, First"
        return {"id": "arxiv:" + aid, "src": "arXiv", "title": flat(one("citation_title", "og:title")), "url": url,
                "by": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                "date": one("citation_date", "citation_online_date").replace("/", "-")[:10] or today,
                "text": flat(one("citation_abstract", "og:description"))}
    m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})", url)
    if m:
        url = "https://www.youtube.com/watch?v=" + m.group(1)
        o = json.loads(fetch("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url)))
        return {"id": "yt:" + m.group(1), "src": "YouTube", "title": flat(o["title"]), "url": url,
                "by": o.get("author_name", ""), "date": today, "text": ""}
    m = re.match(r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/?#].*)?$", url)
    if m:
        return parse_github(json.dumps({"items": [json.loads(fetch(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}"))]}))[0]
    page = fetch(url, BROWSER).decode("utf-8", "replace")
    meta = page_meta(page)
    tag = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    title = (meta.get("og:title") or meta.get("twitter:title") or [html.unescape(tag.group(1)) if tag else url])[0]
    return {"id": "link:" + url, "src": "Link", "title": flat(title, 300), "url": url,
            "by": urlparse(url).netloc.removeprefix("www."), "date": today,
            "text": flat((meta.get("og:description") or meta.get("description") or [""])[0])}


def score_one(item_id):
    """Background: Argo's score and one-line reason for a single (manually added) item."""
    cfg = {**SCOUT, **config()}
    with feed_lock:
        f = load_feed()
        x = next((x for x in f["items"] if x["id"] == item_id), None)
        liked = [y["title"] for y in f["items"] if (y["vote"] > 0 or y["saved"]) and y["id"] != item_id][:15]
        disliked = [y["title"] for y in f["items"] if y["vote"] < 0][:15]
    scores = rank([x], cfg, liked, disliked) if x else {}
    if 0 in scores:
        with feed_lock:
            f = load_feed()
            for y in f["items"]:
                if y["id"] == item_id:
                    y["score"], y["why"], labels = scores[0]
                    if labels and not y.get("labels") and y["score"] >= 5:  # never over the user's own labels
                        y["labels"] = labels
            save_feed(f)


def clean_scores(text, known=()):
    """{index: (score 0-10, why, labels from `known`)} from the model's JSON array reply. Each {...} is parsed on its own,
    so a reply cut off mid-array still keeps the entries before the cut; malformed ones are dropped."""
    out = {}
    for m in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            r = json.loads(m.group(0))
            k = {l.lower(): l for l in known}
            out[int(r["i"])] = (max(0.0, min(10.0, float(r["score"]))), flat(str(r.get("why", "")), 160),
                                sorted({k[str(l).lower()] for l in r.get("labels") or [] if str(l).lower() in k})[:2])
        except (KeyError, TypeError, ValueError):
            pass
    return out


def rank(items, cfg, liked, disliked):
    known = all_labels()
    lines = "\n".join(f"{i}. [{x['src']}] {x['title']} ({x['by']}): {x['text'][:400]}" for i, x in enumerate(items))
    prompt = (f"My interests: {cfg['scout_profile']}\n"
              + (f"Recently I liked: {'; '.join(liked)}\n" if liked else "")
              + (f"Recently I was NOT interested in: {'; '.join(disliked)}\n" if disliked else "")
              + "Rate how much I'd want to see each item below, 0 (irrelevant) to 10 (must read). Reply with ONLY a JSON "
              'array: [{"i": <number>, "score": <0-10>, "why": "<why it matters to me, max 15 words>"'
              + (', "labels": [<0-2 of my labels that clearly fit>]}]\n'
                 f"My labels: {json.dumps(known)}. Use only these, exactly as written; [] if none clearly fits.\n\n" if known else "}]\n\n")
              + lines)
    return clean_scores(argo(prompt, 100 * len(items) + 200, tag="scout"), known)


def run_scout():
    """Fetch every source, keep the unseen items, then score anything not scored yet (in batches)."""
    if not scout_busy.acquire(blocking=False):
        return
    try:
        cfg = {**SCOUT, **config()}
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        gh = {"Authorization": "Bearer " + cfg["github_token"]} if cfg.get("github_token") else {}
        # one query per keyword, so a busy topic (agents) can't crowd a quiet one (ptychography) out of the results
        jobs = [(f"arXiv '{k}'", lambda k=k: parse_arxiv(fetch("https://export.arxiv.org/api/query?search_query="
                                                              + urllib.parse.quote(f'all:"{k}"')
                                                              + "&sortBy=submittedDate&sortOrder=descending&max_results=15")))
                for k in cfg["scout_keywords"] + cfg["scout_arxiv_keywords"]]
        jobs += [("YouTube " + (" ".join(line.split()[1:]) or line.split()[0]), lambda c=line.split()[0]: youtube(c))
                 for line in cfg["scout_youtube"]]
        jobs += [(f"GitHub '{k}'", lambda k=k: parse_github(fetch("https://api.github.com/search/repositories?sort=stars&per_page=10&q="
                                                                  + urllib.parse.quote(f'"{k}" pushed:>{since}'), gh)))
                 for k in cfg["scout_keywords"]]
        found, errors, arxiv_down, ok = [], [], False, 0
        for name, job in jobs:
            is_arxiv = name.startswith("arXiv")
            if is_arxiv and arxiv_down:
                continue  # rate-limited/down: more calls only extend the block
            try:
                found += job()
                ok += 1
            except Exception as e:
                arxiv_down = is_arxiv
                errors.append(f"arXiv unavailable ({e}); skipped arXiv this round, will retry next check" if is_arxiv
                              else f"{name}: {e}")
            if is_arxiv:
                time.sleep(3)  # arXiv API asks for 3 s between calls
        offline = bool(jobs) and not ok  # nothing reachable: no network, or a background wake while the Mac sleeps
        if offline:
            errors = ["Offline: couldn't reach arXiv, GitHub or YouTube (no network, or the Mac was asleep); "
                      "will retry in a few minutes"]
        for src in ("YouTube", "GitHub"):  # one line per source, not one per channel / search term
            names = [e.split(": ", 1)[0][len(src) + 1:] for e in errors if e.startswith(src + " ")]
            if names:
                errors = [e for e in errors if not e.startswith(src + " ")] + [
                    f"{src}: couldn't load {', '.join(names)}; will retry next check"]
        with feed_lock:
            f = load_feed()
            seen = set(f["seen"])
            now = datetime.now().isoformat(timespec="minutes")
            for x in found:
                if x["id"] not in seen:
                    seen.add(x["id"])
                    f["items"].insert(0, {**x, "added": now, "score": None, "why": "", "vote": 0, "saved": False, "read": False})
            f["seen"] = sorted(seen)
            # ponytail: keeps the newest 1000 + saved; ids stay in "seen" so pruned items never come back
            f["items"] = [x for i, x in enumerate(f["items"]) if i < 1000 or x["saved"]]
            if not offline:  # an offline round doesn't count, so scout_loop retries within 10 minutes
                f["last"] = now
            f["errors"] = errors
            todo = [x for x in f["items"] if x["score"] is None]
            liked = [x["title"] for x in f["items"] if x["vote"] > 0 or x["saved"]][:15]
            disliked = [x["title"] for x in f["items"] if x["vote"] < 0][:15]
            save_feed(f)
        picks = 0
        for i in range(0, len(todo), 25):
            batch = todo[i:i + 25]
            scores = rank(batch, cfg, liked, disliked)
            if not scores:
                break  # Argo unreachable: leave them unscored, retry next round
            with feed_lock:
                f = load_feed()
                by_id = {x["id"]: x for x in f["items"]}
                for j, (score, why, labels) in scores.items():
                    if 0 <= j < len(batch) and batch[j]["id"] in by_id:
                        x = by_id[batch[j]["id"]]
                        x.update(score=score, why=why)
                        if labels and not x.get("labels") and score >= 5:  # never over the user's own labels
                            x["labels"] = labels
                        picks += score >= 8
                save_feed(f)
        if picks:
            say("scout", "Scout", f"{picks} new pick{'s' * (picks > 1)} for you")
    finally:
        scout_busy.release()


def relabel_scout():
    """One-time: labels for items scored (5+) before Scout suggested labels. Keeps their score and why; marks the feed
    done only once every batch got a reply, so an Argo outage just retries on the next launch."""
    f = load_feed()
    if f.get("relabeled") or not all_labels():
        return
    todo = [x for x in f["items"] if not x.get("labels") and (x["score"] or 0) >= 5]
    for i in range(0, len(todo), 25):
        batch = todo[i:i + 25]
        scores = rank(batch, {**SCOUT, **config()}, [], [])
        if not scores:
            return
        with feed_lock:
            f = load_feed()
            by_id = {x["id"]: x for x in f["items"]}
            for j, (_, _, labels) in scores.items():
                x = by_id.get(batch[j]["id"]) if 0 <= j < len(batch) else None
                if x and labels and not x.get("labels"):
                    x["labels"] = labels
            save_feed(f)
    with feed_lock:
        f = load_feed()
        f["relabeled"] = True
        save_feed(f)


def scout_loop():
    relabel_scout()
    while True:
        hours = {**SCOUT, **config()}["scout_hours"]
        if load_feed()["last"] < (datetime.now() - timedelta(hours=hours)).isoformat(timespec="minutes"):
            run_scout()  # also catches up right after launch if the Mac was off
        time.sleep(600)


# ---- Library: local projects, files, docs and links. Type (stored as "group") is what an item is, set automatically;
# labels are what it's about, shared with tasks and Scout. Argo writes a summary and suggests labels ----
LIB = DATA / "library.json"  # {"v": 2, "groups": [type names], "labels": {name: hue}, "items": [...]}
TYPES = ["Folders", "Papers", "Docs", "Slides", "Data", "Code", "Images", "Videos", "Websites", "Other"]
EXT_TYPE = {e: t for t, es in {
    "Docs": "doc docx rtf rtfd odt pages txt md markdown rst tex bib",
    "Slides": "ppt pptx key odp",
    "Data": "csv tsv xls xlsx numbers h5 hdf5 hdf nxs npy npz mat parquet zarr dat raw",
    "Code": "py ipynb js ts tsx jsx swift c cc cpp h hpp m r jl sh zsh sql html css json yaml yml toml",
    "Images": "png jpg jpeg gif tif tiff svg heic bmp webp",
    "Videos": "mp4 mov m4v avi mkv webm"}.items() for e in es.split()}
LINK_TYPE = {"arXiv": "Papers", "YouTube": "Videos", "GitHub": "Code", "Link": "Websites"}
FILES = DATA / "files"  # PDFs added to the Library by link are downloaded here
lib_lock = threading.Lock()
TEXT_EXT = set("txt md markdown rst tex bib py ipynb json csv tsv yaml yml toml ini cfg swift js ts tsx jsx html htm css "
               "c h cc cpp hpp m r jl sh zsh sql xml log".split())


def auto_type(x):
    """What a Library item is, from where it points: a link's site, folder vs file, the file's extension."""
    if x.get("src"):
        return LINK_TYPE.get(x["src"], "Websites")
    if x["folder"]:
        return "Folders"
    ext = Path(x["path"]).suffix.lower()[1:]
    if ext == "pdf":  # ponytail: paper vs other PDF by a few telltale words in its first page or two
        return "Papers" if re.search(r"\babstract\b|\bdoi\b|arxiv|et al\.", x["peek"], re.I) else "Docs"
    return EXT_TYPE.get(ext, "Other")


def load_lib():
    lib = json.loads(LIB.read_text()) if LIB.exists() else {"v": 2, "groups": list(TYPES), "labels": {}, "items": []}
    if lib.get("v", 1) < 2:  # groups -> automatic types; "Projects" goes (projects are labels now). Kept by the next save
        old = {"Projects", "Papers", "Docs", "Data", "Code", "Videos", "Websites"}
        lib["groups"] = TYPES + [g for g in lib["groups"] if g not in old and g not in TYPES]  # the user's own stay
        for x in lib["items"]:
            if x["group"] in old or not x["group"]:
                x["group"] = auto_type(x)
        lib["v"] = 2
    return lib


def save_lib(lib):
    tmp = LIB.with_suffix(".tmp")
    tmp.write_text(json.dumps(lib, indent=1, ensure_ascii=False))
    tmp.replace(LIB)


def peek(p):
    """A few KB that say what a path is (for search and for Argo): a folder's listing + README, or a file's text."""
    p = Path(p)
    try:
        if p.is_dir():
            kids = sorted((x for x in p.iterdir() if not x.name.startswith(".")), key=lambda x: x.name.lower())
            readme = next((x for x in kids if x.name.lower().startswith("readme") and x.is_file()), None)
            return ("Contains: " + ", ".join(x.name + "/" * x.is_dir() for x in kids[:60]) + " …" * (len(kids) > 60)
                    + ("\n\n" + peek(readme) if readme else ""))[:3000]
        ext = p.suffix.lower()[1:]
        if ext in TEXT_EXT:
            with open(p, errors="replace") as f:
                return f.read(3000)
        if ext in ("docx", "doc", "rtf", "rtfd", "odt", "webarchive"):
            out = subprocess.run(["textutil", "-convert", "txt", "-stdout", p], capture_output=True, timeout=30).stdout
            return out.decode("utf-8", "replace")[:3000]
        if ext == "pdf":  # PDFKit through JavaScript for Automation: no compiler or extra package needed
            out = subprocess.run(["osascript", "-l", "JavaScript", "-e", 'function run(a){ObjC.import("Quartz");'
                                  'const d=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(a[0]));'
                                  'return d.isNil()?"":d.string.js.slice(0,3000)}', p], capture_output=True, timeout=30).stdout
            if out.strip():
                return out.decode("utf-8", "replace").strip()[:3000]
        # the rest (and scanned PDFs without a text layer): Spotlight's title/authors only
        out = subprocess.run(["mdls", "-name", "kMDItemTitle", "-name", "kMDItemAuthors", "-name", "kMDItemDescription", p],
                             capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
        lines = re.sub(r"\s*kMDItem(\w+) = ", r"\n\1: ", " ".join(out.split())).splitlines()  # "Title: ...", "Authors: ..."
        return "\n".join(" ".join(re.sub(r'[()"]', "", l).split()) for l in lines if l.strip() and "(null)" not in l)[:3000]
    except Exception:
        return ""


def lib_item(path, group="", link=None):
    """A Library entry for a path on disk, or (link: a link_item) for a web page, video, paper or repo."""
    if link:
        what = {"path": link["url"], "name": link["title"] or link["url"], "folder": False, "src": link["src"],
                "peek": "\n".join(filter(None, [link["by"], link["date"], link["text"]]))}
    else:
        p = Path(path).expanduser().resolve()
        what = {"path": str(p), "name": p.name or str(p), "folder": p.is_dir(), "peek": peek(p)}
    x = {"id": uuid.uuid4().hex[:12], **what, "labels": [], "note": "", "summary": "",
         "added": datetime.now().isoformat(timespec="minutes"), "opened": "",
         "retry": True}  # cleared once Argo answers, so a quit or crash mid-call still gets retried
    x["group"] = group or auto_type(x)
    return x


def lib_link(url):
    """A link_item for a pasted link; a link straight to a PDF is downloaded into data/files and returned as a Path."""
    url = url if re.match(r"https?://", url) else "https://" + url
    name = Path(urlparse(url).path).name
    if name.lower().endswith(".pdf") and "arxiv.org" not in url:  # arXiv PDFs: its abstract page says more
        data = fetch(url, BROWSER)
        if not data.startswith(b"%PDF"):
            raise ValueError("that link didn't return a PDF (sign-in page?)")
        FILES.mkdir(parents=True, exist_ok=True)
        p = FILES / name
        while p.exists() and p.read_bytes() != data:  # same name, different paper
            p = p.with_name(p.stem + "-1.pdf")
        p.write_bytes(data)
        return p
    return link_item(url)


def lib_add(paths, split=False, group=""):
    """Add paths (each subfolder of them instead, with split). Returns (items added, paths skipped with why)."""
    want, skipped = [], []
    for raw in filter(str.strip, paths):
        r = raw.strip().strip("'\"")  # Finder's "Copy as Pathname" may come quoted
        if re.match(r"(?:https?://|www\.|youtu\.be/)", r):
            try:
                want.append(lib_link(r))
            except Exception as e:
                skipped.append(f"{r}: couldn't read that link ({e})")
            continue
        p = Path(r).expanduser()
        if not p.exists():
            skipped.append(f"{raw.strip()}: not found")
        elif split and p.is_dir():
            want += [x for x in sorted(p.iterdir()) if x.is_dir() and not x.name.startswith(".")]
        else:
            want.append(p)
    with lib_lock:
        lib = load_lib()
        have = {x["path"] for x in lib["items"]}
        new = []
        for p in want:
            link = p if isinstance(p, dict) else None
            if (link["url"] if link else str(p.resolve())) in have:
                skipped.append(f"{link['title'] if link else p.name}: already in the Library")
            else:
                new.append(lib_item(p, group, link))
                have.add(new[-1]["path"])
        lib["items"] = new + lib["items"]
        if group and group not in lib["groups"]:
            lib["groups"].append(group)
        save_lib(lib)
    # one at a time, so adding a folder of 200 projects doesn't fire 200 Argo calls at once
    threading.Thread(target=lambda: [lib_enrich(x["id"]) for x in new], daemon=True).start()
    return new, skipped


def feed_to_lib(x):
    """Put a Scout item in the Library (its labels too). False if that link is already there."""
    with lib_lock:
        lib = load_lib()
        if any(y["path"] == x["url"] for y in lib["items"]):
            return False
        item = {**lib_item("", link=x), "labels": list(x.get("labels") or [])}
        lib["items"].insert(0, item)
        save_lib(lib)
    threading.Thread(target=lib_enrich, args=(item["id"],), daemon=True).start()
    return True


def clean_lib_suggestion(text):
    """{"summary", "labels"} from the model's reply."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
    labels = [str(x).strip() for x in d.get("labels") or [] if 0 < len(str(x).strip()) <= 24][:3]
    return {"summary": flat(str(d.get("summary") or ""), 300), "labels": labels}


def lib_enrich(item_id):
    """Background: Argo's one-line summary and labels for a new item, never overwriting what the user set."""
    with lib_lock:
        lib = load_lib()
        x = next((x for x in lib["items"] if x["id"] == item_id), None)
    known = all_labels()
    if not x or not argo_on():
        return
    kind = f"{x['src']} link" if x.get("src") else "local folder" if x["folder"] else "local file"
    reply = argo(f"Below is a {kind} I keep in my library of projects, files and docs. "
                 'Reply with ONLY a JSON object {"summary": "...", "labels": [...]}.\n'
                 "- summary: one plain line (max 20 words) saying what it is.\n"
                 f"- labels: up to 3 short labels for the projects or topics it belongs to (1-2 words, Title Case). Existing labels: {json.dumps(known)}. Reuse one "
                 "whenever it fits; invent a new one only if none does.\n\n"
                 f"{'Link' if x.get('src') else 'Path'}: {x['path']}\nName: {x['name']}\n\n{x['peek']}", 600, tag="lib:" + item_id)
    with lib_lock:
        lib = load_lib()
        cur = next((y for y in lib["items"] if y["id"] == item_id), None)
        if not cur:
            return
        if reply is None:  # Argo down: retry_argo() tries again later
            cur["retry"] = True
            return save_lib(lib)
        cur.pop("retry", None)
        s = clean_lib_suggestion(reply)
        if s.get("summary") and not cur["summary"]:
            cur["summary"] = s["summary"]
        if s.get("labels") and not cur["labels"]:
            cur["labels"] = sorted(set(s["labels"]))
        save_lib(lib)
    lib_relate(item_id)  # with its summary and labels, it's easier to match


RELATE_ROUND = 20  # retry_argo: at most this many items get their links found per round


def clean_related(text, n):
    """[(candidate index, why)] from the model's JSON array reply: valid indexes below n, each once, at most 4."""
    out = {}
    for m in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            r = json.loads(m.group(0))
            i = int(r["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < n and i not in out:
            out[i] = flat(str(r.get("why") or ""), 80)
    return list(out.items())[:4]


def lib_relate(item_id):
    """Background: Argo links an item to the Library items, tasks and emails it belongs with. Keeps links you made and
    never brings back one you removed; links it made before are replaced."""
    with lib_lock:
        x = next((x for x in load_lib()["items"] if x["id"] == item_id), None)
    if not x or not argo_on():
        return
    me = "lib:" + item_id
    pool = [c for c in corpus() if c["ref"] != me]
    # ponytail: keyword overlap picks 30 candidates for Argo to judge; embeddings if it misses obvious ones
    cands = top_hits(pool, " ".join([x["name"], x["summary"], *x["labels"], x["peek"][:800]]),
                     lambda c: " ".join([c["title"], *c["labels"], c["text"][:1500]]), 30)
    if not cands:
        reply = "[]"
    else:
        lines = "\n".join(f"{i}. [{c['kind']}] {c['title']}" + (f" (labels: {', '.join(c['labels'])})" if c["labels"] else "")
                          + f": {flat(c['text'], 300)}" for i, c in enumerate(cands))
        reply = argo("Below is an item from my library of projects, files, papers and links, then numbered candidates "
                     "(other library items, my tasks and emails). Which candidates is it directly connected to: the same "
                     "project, one cites, uses or describes the other, an email or task about it, its data, code or talk? "
                     "Skip ones that only share a broad topic. Reply with ONLY a JSON array, at most 4, empty if none: "
                     '[{"i": <candidate number>, "why": "<how they connect, max 8 words>"}]\n\n'
                     f"Item: {x['name']}\n{x['summary']}\nLabels: {', '.join(x['labels']) or '-'}\n{flat(x['peek'], 1200)}"
                     f"\n\nCandidates:\n{lines}", 600, tag="lib:" + item_id)
    if reply is None:  # Argo down: no "related" yet, so retry_argo() tries again later
        return
    with lib_lock:
        lib = load_lib()
        cur = next((y for y in lib["items"] if y["id"] == item_id), None)
        if not cur:
            return
        keep = [r for r in cur.get("related", []) if r.get("by") == "me"]
        skip = {r["ref"] for r in keep} | set(cur.get("unlinked", []))
        cur["related"] = keep + [{"ref": cands[i]["ref"], "why": why, "by": "argo"}
                                 for i, why in clean_related(reply, len(cands)) if cands[i]["ref"] not in skip]
        save_lib(lib)


def lib_meta(lib, d):
    """Types ("group")/labels editor: {"op": "add_group"|"rename_group"|"delete_group"|"add_label"|"rename_label"|
    "delete_label"|"color_label", "name", "to", "hue"}. Renaming onto an existing name merges the two."""
    op, name, to = d.get("op", ""), str(d.get("name") or "").strip(), " ".join(str(d.get("to") or "").split())[:40]
    if op == "add_group" and name and name not in lib["groups"]:
        lib["groups"].append(name)
    elif op in ("rename_group", "delete_group"):
        new = to if op == "rename_group" else ""
        if op == "rename_group" and not new:
            return
        lib["groups"] = list(dict.fromkeys(new if g == name else g for g in lib["groups"] if g != name or new))
        for x in lib["items"]:
            if x["group"] == name:
                x["group"] = new
    elif op == "add_label" and name:
        lib["labels"].setdefault(name, None)  # None: colour from the name's hash, like Task Manager
    elif op in ("rename_label", "delete_label"):
        new = to if op == "rename_label" else ""
        if op == "rename_label" and not new:
            return
        hue = lib["labels"].pop(name, None)
        if new:
            lib["labels"][new] = lib["labels"].get(new, hue)
        relabel(lib["items"], name, new)
    elif op == "color_label" and name:
        lib["labels"][name] = max(0, min(359, int(d.get("hue") or 0)))


def relabel(items, name, new=""):
    """Rename (or with new="", remove) a label on every item that has it."""
    for x in items:
        if name in x.get("labels", []):
            x["labels"] = sorted({new if l == name else l for l in x["labels"]} - {""})


def all_labels():
    """One label vocabulary for tasks, Scout and the Library."""
    with lock:
        out = {l for t in load() for l in t.get("labels", [])}
    with feed_lock:
        out |= {l for x in load_feed()["items"] for l in x.get("labels", [])}
    with lib_lock:
        lib = load_lib()
    return sorted(out | set(lib["labels"]) | {l for x in lib["items"] for l in x["labels"]})


KIND = {"arXiv": "paper", "YouTube": "video", "GitHub": "repo"}


def corpus():
    """Everything the app knows, as {ref, kind, title, sub, text, labels}: Library items and tasks/emails (Scout keeps to its own window)."""
    with lib_lock:
        lib = load_lib()["items"]
    with lock:
        todos = load()
    return ([{"ref": "lib:" + x["id"], "kind": KIND.get(x["src"], "link") if x.get("src") else "folder" if x["folder"] else "file", "title": x["name"], "sub": x["path"],
              "text": "\n".join(filter(None, [x["path"], x["group"], x["summary"], x["note"], x["peek"]])), "labels": x["labels"]}
             for x in lib]
            + [{"ref": "task:" + t["id"], "kind": "email" if t.get("eml") else "task", "title": t["title"],
                "sub": " · ".join(filter(None, [t["sender"], t["date"] or t["added"][:10], t["due"] and "due " + t["due"].replace("T", " "),
                                                t["done"] and "done"])),
                "text": f"{t['body']}\n{t['note']}", "labels": t.get("labels", []), "id": t["id"], "eml": bool(t.get("eml"))}
               for t in todos])


HOME = Path.home()


def readable(p):
    """Home-folder paths the search agent may look at: no dot-folders (~/.ssh ...) and not ~/Library (mail, keychains, app data)."""
    p = Path(p).expanduser().resolve()
    if not p.exists() or not p.is_relative_to(HOME) or p == HOME:
        return None
    parts = p.relative_to(HOME).parts
    return None if parts[0] == "Library" or any(x.startswith(".") for x in parts) else p


def spotlight(q):
    out = subprocess.run(["mdfind", "-onlyin", str(HOME), q], capture_output=True, timeout=20).stdout.decode("utf-8", "replace")
    return [str(p) for p in map(readable, out.splitlines()[:200]) if p][:15]


def lib_ask(q, steps=6):
    """Agentic search: Argo searches the app's items and Spotlight, reads what looks promising, then answers.
    Returns {"answer", "refs", "steps"}, or None if Argo is unreachable."""
    items = corpus()
    by_ref = {x["ref"]: x for x in items}
    line = lambda x: f"{x['ref']} [{x['kind']}] {x['title']} | {x['sub']}" + (f" | labels {', '.join(x['labels'])}" if x["labels"] else "")
    log, trail, refs = [], [], []
    for i in range(steps):
        last = i == steps - 1
        reply = argo(
            f"Today is {datetime.now():%A %Y-%m-%d}. You are finding things for me on my Mac and in my notes app, to answer: {q}\n"
            "My app holds Library items (local folders, files, docs), tasks and saved emails. "
            f"Labels in use: {json.dumps(all_labels())}.\nEach turn reply with ONLY one JSON object, one of:\n"
            '{"search": "keywords"}  search the app\'s items (names, paths, notes, summaries, email text, labels)\n'
            '{"label": "Label"}  list everything with that label\n'
            '{"spotlight": "keywords"}  Spotlight search of file names and contents in my home folder\n'
            '{"read": "<ref or path>"}  read an item or a file (first few KB; a folder gives its file list)\n'
            '{"answer": "brief plain-text answer", "refs": ["<ref or path>", ...]}  when you know, or when searching '
            "stops helping. refs: the items or files the answer points to, best first.\n"
            "Search with different words if a search finds nothing. Never invent refs or paths."
            + ("\nThis is your last turn: answer now." if last else "")
            + "\n\nWhat you did so far:\n" + ("\n\n".join(log) or "(nothing yet)"), 700, timeout=60, tag="lib:ask")
        if reply is None:
            return None
        m = re.search(r"\{.*\}", reply, re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except ValueError:
            d = {}
        if "answer" in d or last or not d:
            refs = [str(r) for r in d.get("refs") or []] if d else []
            answer = str(d.get("answer") or "") if d else reply.strip()
            break
        if d.get("search"):
            hits = [x for x in top_hits(items, d["search"], lambda x: " ".join([x["title"], x["sub"], x["text"], *x["labels"]]), 10)
                    if any(w in " ".join([x["title"], x["sub"], x["text"], *x["labels"]]).lower()
                           for w in re.findall(r"\w{3,}", d["search"].lower()))]
            trail.append(f"Searched “{d['search']}”")
            log.append(f"search {d['search']!r} ->\n" + ("\n".join(map(line, hits)) or "nothing"))
        elif d.get("label"):
            hits = [x for x in items if str(d["label"]).lower() in (l.lower() for l in x["labels"])][:30]
            trail.append(f"Label “{d['label']}”")
            log.append(f"label {d['label']!r} ->\n" + ("\n".join(map(line, hits)) or "nothing"))
        elif d.get("spotlight"):
            paths = spotlight(str(d["spotlight"]))
            trail.append(f"Spotlight “{d['spotlight']}”")
            log.append(f"spotlight {d['spotlight']!r} ->\n" + ("\n".join(paths) or "nothing"))
        elif d.get("read"):
            r = str(d["read"])
            x, p = by_ref.get(r), None if r in by_ref else readable(r)
            trail.append("Read " + (x["title"] if x else Path(r).name))
            log.append(f"read {r} ->\n" + (f"{line(x)}\n{x['text'][:3000]}" if x else peek(p) if p else "not found (or not allowed)"))
        else:
            log.append(f"{json.dumps(d)} -> unknown action")
    out = []
    for r in dict.fromkeys(refs):
        if r in by_ref:
            out.append({k: v for k, v in by_ref[r].items() if k != "text"})
        elif readable(r):
            p = readable(r)
            out.append({"ref": str(p), "kind": "path", "title": p.name, "sub": str(p), "labels": []})
    return {"answer": answer.replace("**", "").strip(), "refs": out[:12], "steps": trail}


def pets(after=0):
    """Everything the Mac app's four pets show: badges, who's working, new bubbles."""
    global pets_seen
    pets_seen = time.time()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    with lock:
        todos = load()
    with feed_lock:
        items = load_feed()["items"]
    with busy_lock:
        tags = [k for k, n in busy.items() if n > 0 and k != "check"]
        said = [b for b in bubbles if b["id"] > after]
    ag = agents()
    return {"overdue": sum(1 for t in todos if not t["done"] and t["due"] and t["due"] <= now),
            "picks": sum(1 for x in items if not x["read"] and x["vote"] >= 0 and (x["score"] or 0) >= 7),
            "busy": {"task": any(not k.startswith(("lib", "scout")) for k in tags),
                     "scout": scout_busy.locked() or "scout" in tags, "library": any(k.startswith("lib") for k in tags),
                     "levi": any(a["state"] == "working" for a in ag)},
            "bubbles": said,
            "routines": [{"line": r["line"], "pet": r["pet"]} for r in routines()],
            "agents": ag}


class H(BaseHTTPRequestHandler):
    def reply(self, code, body=b"{}", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/todos":
            with lock:
                self.reply(200, json.dumps(load()).encode())
        elif self.path == "/api/settings":
            self.reply(200, json.dumps(get_settings()).encode())
        elif self.path.startswith("/api/shot/"):  # a screenshot task's image
            f = next(SHOTS.glob(re.sub(r"\W", "", self.path.rsplit("/", 1)[1]) + ".*"), None)
            self.reply(200, f.read_bytes(), IMAGE.get(f.suffix, "application/octet-stream")) if f else self.reply(404)
        elif self.path == "/api/status":
            with busy_lock:
                working = sorted(k for k, n in busy.items() if n > 0)
            self.reply(200, json.dumps({"configured": argo_on(), "argo": argo_ok, "checked": argo_at, "busy": working,
                                        "retry_minutes": config().get("retry_minutes", 10),
                                        "rev": DB.stat().st_mtime if DB.exists() else 0,
                                        "lib_rev": LIB.stat().st_mtime if LIB.exists() else 0}).encode())
        elif self.path.startswith("/api/pets"):  # /api/pets?after=<last bubble id seen>
            after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0] or 0)
            self.reply(200, json.dumps(pets(after)).encode())
        elif self.path == "/feed":
            self.reply(200, FEED_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/scout/settings":
            self.reply(200, json.dumps({k: {**SCOUT, **config()}[k] for k in SCOUT}).encode())
        elif self.path == "/settings":
            self.reply(200, SETTINGS_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/library":
            self.reply(200, LIB_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/levi":
            self.reply(200, LEVI_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/agents":
            self.reply(200, json.dumps(agents()).encode())
        elif self.path == "/api/library":
            with lib_lock:
                lib = load_lib()
            for x in lib["items"]:
                x["missing"] = not x.get("src") and not Path(x["path"]).exists()
            lib["editor"] = config().get("library_editor", "Visual Studio Code")
            self.reply(200, json.dumps(lib).encode())
        elif self.path == "/api/labels":  # the shared vocabulary + colours picked in the Library
            with lib_lock:
                hues = {k: v for k, v in load_lib()["labels"].items() if v is not None}
            self.reply(200, json.dumps({"all": all_labels(), "hues": hues}).encode())
        elif self.path == "/api/labeled":  # tasks and emails for the Library: label view, Related links, Map
            self.reply(200, json.dumps([{k: v for k, v in x.items() if k != "text"}
                                        for x in corpus() if not x["ref"].startswith("lib:")]).encode())
        elif self.path == "/api/feed":
            with feed_lock:
                f = load_feed()
            f.pop("seen")
            f["busy"] = scout_busy.locked()
            self.reply(200, json.dumps(f).encode())
        else:
            self.reply(404)

    def do_POST(self):
        if self.headers.get("Origin") not in (None, ORIGIN):  # block other websites
            return self.reply(403)
        u = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if u.path == "/api/agent":  # hooks/: ?tool=cc&pid=<claude pid> with the hook's JSON, or ?tool=codex with notify's
            q = parse_qs(u.query)
            try:
                host = q.get("host", [""])[0]
                agent_event(json.loads(raw or b"{}"), q.get("tool", ["cc"])[0], int(q.get("pid", ["0"])[0] or 0),
                            host if re.fullmatch(r"[\w.-]+@[\w.-]+", host) else "")
            except (ValueError, AttributeError):  # not JSON / not an object / bad pid
                return self.reply(400)
            return self.reply(200)
        if u.path == "/api/scout/channel":  # {"q": "@handle" | link | id} -> {"line": "UC... name"}
            try:
                return self.reply(200, json.dumps({"line": channel(json.loads(raw or b"{}").get("q") or "")}).encode())
            except Exception as e:
                msg = "Channel not found" if "404" in str(e) or isinstance(e, (ValueError, IndexError)) else str(e)
                return self.reply(400, json.dumps({"error": msg}).encode())
        if u.path == "/api/scout/settings":
            err = save_settings(json.loads(raw or b"{}"))
            if err:
                return self.reply(400, json.dumps({"error": err}).encode())
            threading.Thread(target=run_scout, daemon=True).start()  # check right away with the new searches
            return self.reply(200)
        if u.path == "/api/routines/run":  # {"line": ...}: the pet's "Run now"
            line = json.loads(raw or b"{}").get("line")
            r = next((r for r in routines() if r["line"] == line), None)
            if not r:
                return self.reply(404)
            threading.Thread(target=run_routine, args=(r,), daemon=True).start()
            return self.reply(200)
        if u.path == "/api/feed/refresh":
            threading.Thread(target=run_scout, daemon=True).start()
            return self.reply(200)
        if u.path == "/api/feed/task":  # Scout card -> "Read: ..." task; Argo writes a 2-line summary as its note
            fid = json.loads(raw or b"{}").get("id")
            with feed_lock:
                f = load_feed()
                x = next((x for x in f["items"] if x["id"] == fid), None)
                if not x:
                    return self.reply(404)
                if x.get("task"):
                    return self.reply(200)
                x["task"] = True
                save_feed(f)
            t = {**blank(), "title": "Read: " + x["title"], "sender": " · ".join(filter(None, [x["src"], x["by"]])),
                 "date": x["date"], "body": f"{x['url']}\n\n{x['text']}", "eml": False, "labels": sorted({"Reading", *x.get("labels", [])})}
            with lock:
                todos = load()
                todos.insert(0, t)
                save(todos)
            threading.Thread(target=summarize, args=(t,), daemon=True).start()
            return self.reply(200)
        if u.path == "/api/argo/check":
            check_argo()
            if argo_ok:
                threading.Thread(target=retry_argo, daemon=True).start()  # clicking the pill also clears the waiting queue
            return self.reply(200)
        if u.path == "/api/argo/retry":  # manual retry: ping first so a dead Argo fails fast, then redo the waiting work
            check_argo()
            if argo_ok:
                retry_argo()
            return self.reply(200)
        if u.path == "/api/settings":
            try:
                set_settings(json.loads(raw or b"{}"))
            except (ValueError, TypeError):
                return self.reply(400)
            return self.reply(200)
        if u.path == "/api/ask":  # {"q"}: across all tasks, or {"q", "id"}: about that one email
            d = json.loads(raw or b"{}")
            with lock:
                t = next((x for x in load() if x["id"] == d.get("id")), None)
            if d.get("id") and not t:
                return self.reply(404)
            answer = ((ask_mail(t, d.get("q", "")) if t else ask(d.get("q", ""))) or "").replace("**", "")  # plain-text box
            return self.reply(200, json.dumps({"answer": answer}).encode()) if answer else self.reply(503)
        if u.path.startswith("/api/reply/"):
            with lock:
                t = next((x for x in load() if x["id"] == u.path.rsplit("/", 1)[1]), None)
            if not t or not t.get("eml"):
                return self.reply(404)
            text = draft_reply(t, str(json.loads(raw or b"{}").get("how") or "").strip())
            return self.reply(200, json.dumps({"draft": text}).encode()) if text else self.reply(503)
        if u.path == "/api/feed/library":  # {"id": ...} -> that Scout item moves to the Library ("saved": hidden from
            # Scout but kept, so it still steers ranking and its id is never fetched again)
            with feed_lock:
                f = load_feed()
                x = next((x for x in f["items"] if x["id"] == json.loads(raw or b"{}").get("id")), None)
                if x:
                    x.update(saved=True, read=True)
                    save_feed(f)
            return self.reply(200, json.dumps({"added": feed_to_lib(x)}).encode()) if x else self.reply(404)
        if u.path == "/api/feed/add":  # {"url": ...} -> fetched, put in Scout and the Library, then scored in the background
            try:
                item = link_item(json.loads(raw or b"{}").get("url") or "")
            except urllib.error.HTTPError as e:
                return self.reply(400, json.dumps({"error": f"Couldn't read that link (the site answered {e.code})"}).encode())
            except urllib.error.URLError:
                return self.reply(400, json.dumps({"error": "Couldn't reach that site. Check the link?"}).encode())
            except Exception as e:
                return self.reply(400, json.dumps({"error": f"Couldn't read that link ({e})"}).encode())
            with feed_lock:
                f = load_feed()
                x = next((x for x in f["items"] if x["id"] == item["id"]), None)
                existed = x is not None
                if x:
                    x.update(saved=True, read=True)
                else:
                    x = {**item, "added": datetime.now().isoformat(timespec="minutes"), "score": None, "why": "",
                         "vote": 0, "saved": True, "read": True, "manual": True}
                    f["items"].insert(0, x)
                    f["seen"] = sorted(set(f["seen"]) | {x["id"]})
                save_feed(f)
            existed = not feed_to_lib(x)
            if x["score"] is None:
                threading.Thread(target=score_one, args=(x["id"],), daemon=True).start()
            return self.reply(200, json.dumps({"title": x["title"], "existing": existed}).encode())
        if u.path == "/api/library/add":  # {"paths": [...], "split": bool, "group": ""}
            d = json.loads(raw or b"{}")
            new, skipped = lib_add(d.get("paths") or [], bool(d.get("split")), str(d.get("group") or "").strip())
            return self.reply(200, json.dumps({"added": [x["name"] for x in new], "skipped": skipped}).encode())
        if u.path == "/api/library/ask":  # agentic search across everything
            res = lib_ask(json.loads(raw or b"{}").get("q", ""))
            return self.reply(200, json.dumps(res).encode()) if res else self.reply(503)
        if u.path == "/api/reveal":  # a file the search found, outside the Library
            p = readable(json.loads(raw or b"{}").get("path") or "")
            return self.reply(404) if not p or subprocess.run(["open", "-R", str(p)]).returncode else self.reply(200)
        if u.path == "/api/library/meta" or u.path.startswith("/api/library/item/"):  # .../item/<id>/<action>
            d, act = json.loads(raw or b"{}"), u.path.rsplit("/", 1)[1]
            if d.get("op") in ("rename_label", "delete_label") and str(d.get("name") or "").strip():  # labels are shared
                new = " ".join(str(d.get("to") or "").split())[:40] if d["op"] == "rename_label" else ""
                if new or d["op"] == "delete_label":
                    with lock:
                        todos = load()
                        relabel(todos, d["name"].strip(), new)
                        save(todos)
                    with feed_lock:
                        f = load_feed()
                        relabel(f["items"], d["name"].strip(), new)
                        save_feed(f)
            with lib_lock:
                lib = load_lib()
                if u.path == "/api/library/meta":
                    lib_meta(lib, d)
                    save_lib(lib)
                    return self.reply(200)
                x = next((x for x in lib["items"] if x["id"] == u.path.split("/")[-2]), None)
                if not x:
                    return self.reply(404)
                if act == "update":
                    x["name"] = str(d.get("name") or "").strip() or x["name"]
                    x["group"] = str(d["group"]).strip() if "group" in d else x["group"]
                    x["note"] = str(d.get("note", x["note"]))
                    if "labels" in d:
                        x["labels"] = sorted({str(l).strip()[:40] for l in d["labels"] if str(l).strip()})
                    if x["group"] and x["group"] not in lib["groups"]:
                        lib["groups"].append(x["group"])
                    if d.get("path") and x.get("src"):  # a link: just the new address
                        x["path"] = d["path"].strip()
                    elif d.get("path"):  # moved on disk: point it at the new place
                        x["path"] = str(Path(d["path"].strip()).expanduser().resolve())
                        x["folder"], x["peek"] = Path(x["path"]).is_dir(), peek(x["path"])
                elif act == "delete":  # only forgets it; the file stays where it is
                    lib["items"].remove(x)
                    for y in lib["items"]:
                        if "related" in y:
                            y["related"] = [r for r in y["related"] if r["ref"] != "lib:" + x["id"]]
                elif act == "link":  # {"ref": "lib:<id>" | "task:<id>"}: a link you made
                    ref = str(d.get("ref") or "")
                    if ref == "lib:" + x["id"] or not re.fullmatch(r"(lib|task):\w+", ref):
                        return self.reply(400)
                    x["related"] = [r for r in x.get("related", []) if r["ref"] != ref] + [{"ref": ref, "why": "", "by": "me"}]
                    x["unlinked"] = [r for r in x.get("unlinked", []) if r != ref]
                elif act == "unlink":  # {"ref"}: drops the link from either end, and Argo won't add it back
                    ref = str(d.get("ref") or "")
                    x["related"] = [r for r in x.get("related", []) if r["ref"] != ref]
                    x["unlinked"] = sorted(set(x.get("unlinked", [])) | {ref})
                    y = next((y for y in lib["items"] if "lib:" + y["id"] == ref), None)
                    if y:
                        y["related"] = [r for r in y.get("related", []) if r["ref"] != "lib:" + x["id"]]
                        y["unlinked"] = sorted(set(y.get("unlinked", [])) | {"lib:" + x["id"]})
                elif act == "relate":  # "Find links" again with Argo, in the background
                    threading.Thread(target=lib_relate, args=(x["id"],), daemon=True).start()
                elif act in ("open", "reveal", "edit", "terminal", "copy"):
                    cmd = {"open": ["open", x["path"]], "reveal": ["open", "-R", x["path"]],
                           "edit": ["open", "-a", config().get("library_editor", "Visual Studio Code"), x["path"]],
                           "terminal": ["open", "-a", "Terminal", x["path"] if x["folder"] else str(Path(x["path"]).parent)]}
                    if act == "copy":
                        subprocess.run(["pbcopy"], input=x["path"].encode())
                    elif subprocess.run(cmd[act], capture_output=True).returncode:
                        return self.reply(400, json.dumps({"error": f"Couldn't {act} {x['name']}"}).encode())
                    x["opened"] = datetime.now().isoformat(timespec="minutes")
                else:
                    return self.reply(404)
                save_lib(lib)
            return self.reply(200)
        if u.path == "/api/feed/update":  # {"id": ..., "vote"/"saved"/"read"/"hidden": ...}, or {"all_read": true}
            patch = json.loads(raw or b"{}")
            with feed_lock:
                f = load_feed()
                for x in f["items"]:
                    if patch.get("all_read"):
                        x["read"] = True
                    elif x["id"] == patch.get("id"):
                        x.update({k: patch[k] for k in ("vote", "saved", "read", "hidden") if k in patch})
                        if "labels" in patch:
                            x["labels"] = sorted({str(l).strip()[:40] for l in patch["labels"] if str(l).strip()})
                save_feed(f)
            return self.reply(200)
        parts = u.path.strip("/").split("/")  # api/drop | api/todo/<id>/<action>
        with lock:
            todos = load()
            if u.path == "/api/drop":
                t = make_todo(raw, parse_qs(u.query).get("name", [""])[0])
                todos.insert(0, t)
                save(todos)
                return self.reply(200, json.dumps({"title": t["title"], "id": t["id"]}).encode())
            elif len(parts) == 4 and parts[1] == "todo":
                t = next((x for x in todos if x["id"] == parts[2]), None)
                if not t:
                    return self.reply(404)
                if parts[3] == "update":
                    patch = json.loads(raw or b"{}")
                    for k in ("title", "due", "done", "note", "actions"):
                        if k in patch:
                            t[k] = patch[k]
                    if patch.get("done"):
                        t["done_at"] = datetime.now().isoformat(timespec="minutes")  # for "what did I finish" routines
                    if "labels" in patch:
                        t["labels"] = sorted({str(x).strip() for x in patch["labels"] if str(x).strip()})
                    if "due" in patch:
                        t["notified"] = False
                        t.pop("due_auto", None)  # the user set it
                elif parts[3] == "split":  # one suggested action item -> its own task, sharing the email
                    title = json.loads(raw or b"{}").get("title", "").strip()
                    t["actions"] = [a for a in t.get("actions", []) if a != title]
                    if title:
                        todos.insert(0, {**t, **blank(), "title": title, "actions": [], "due": t["due"], "retry": "",
                                         "mail": t.get("mail", t["id"])})
                elif parts[3] == "delete":
                    todos.remove(t)
                    mid = t.get("mail", t["id"])
                    if not any(x.get("mail", x["id"]) == mid for x in todos):  # split-off tasks may share the email
                        (MAILS / f"{mid}.eml").unlink(missing_ok=True)
                        for f in SHOTS.glob(f"{mid}.*"):
                            f.unlink()
                elif parts[3] == "open":
                    eml = MAILS / f"{t.get('mail', t['id'])}.eml"
                    if subprocess.run(["open", "-a", "Microsoft Outlook", eml]).returncode:
                        subprocess.run(["open", eml])  # Outlook missing: default mail app
            else:
                return self.reply(404)
            save(todos)
        self.reply(200)

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Task Manager</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#e2e2e7;--hover:#f0f0f4;
  --accent:#0a84ff;--red:#d70015;--redbg:#ff3b3014;--chipL:32%}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--card:#2a2a2d;--text:#f2f2f7;--muted:#98989f;--line:#3a3a3d;
  --hover:#323236;--red:#ff6961;--redbg:#ff453a1f;--chipL:76%}}
*{box-sizing:border-box}
body{font:13.5px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;background:var(--bg);color:var(--text)}
button,input,select,textarea{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:blur(12px);
  padding:14px 20px 10px;border-bottom:1px solid var(--line)}
.top{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
h1{font-size:20px;margin:0;font-weight:700}
#stats{color:var(--muted)}
.bar{display:flex;gap:8px}#shot{opacity:1;width:34px;height:auto;border:1px solid var(--line);background:var(--card);border-radius:8px}
#shot[hidden]{display:none}
.shotimg{display:block;max-width:100%;max-height:320px;margin-top:8px;border:1px solid var(--line);border-radius:10px;cursor:zoom-in}
.bar input,.bar select{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
.bar input:focus,.bar select:focus{border-color:var(--accent);box-shadow:0 0 0 3px #0a84ff33}
#new,#q{flex:1}.bar{flex-wrap:wrap;row-gap:3px}
#status{margin-top:6px;padding:6px 12px;border-radius:8px;font-size:13px;background:color-mix(in srgb,var(--accent) 12%,var(--card));
  border:1px solid color-mix(in srgb,var(--accent) 35%,transparent)}
#status[hidden]{display:none}#status.done{background:color-mix(in srgb,#30d158 14%,var(--card));border-color:color-mix(in srgb,#30d158 40%,transparent)}
.t.flash{animation:flash 2.4s ease-out}@keyframes flash{0%,30%{box-shadow:0 0 0 3px var(--accent)}100%{box-shadow:0 0 0 0 transparent}}.bar .brk{flex-basis:100%;height:0}
main{padding:6px 20px 40px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:18px 4px 6px;font-weight:600}
h2 span{font-weight:400;margin-left:4px}h2.late{color:var(--red)}
.empty{text-align:center;color:var(--muted);padding:60px 0;font-size:15px}
.t{display:flex;gap:10px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;margin:6px 0;transition:background .1s}
.t:hover{background:var(--hover)}
.t{flex-wrap:wrap;row-gap:0}.t>details{flex:0 0 calc(100% - 28px);margin-left:28px;min-width:0}  /* full-width row, lined up with the title */
.t.late{border-left:3px solid var(--red)}
.cb{appearance:none;flex:none;width:18px;height:18px;margin:2px 0 0;border:1.5px solid var(--muted);border-radius:50%;cursor:pointer;display:grid;place-content:center}
.cb:hover{border-color:var(--accent)}
.cb:checked{background:var(--accent);border-color:var(--accent)}
.cb:checked::after{content:"";width:5px;height:9px;border:solid #fff;border-width:0 2px 2px 0;transform:translateY(-1px) rotate(45deg)}
.main{flex:1;min-width:0}
.title{width:100%;border:0;background:none;font-weight:600;font-size:14px;padding:0;outline:none;text-overflow:ellipsis}
.title:focus{text-overflow:clip}
.done .title{text-decoration:line-through;color:var(--muted)}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin-top:3px;font-size:12px}
.info{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
#lbar{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.lb{border:1px solid transparent;border-radius:10px;padding:1px 9px;font-size:12px;cursor:pointer;font-weight:500;
  background:hsl(var(--h,210) 70% 50% / .14);color:hsl(var(--h,210) 65% var(--chipL))}
.lb b{font-weight:400;opacity:.6;margin-left:5px}.lb:hover{border-color:hsl(var(--h,210) 65% 50% / .5)}
.lb.on{background:hsl(var(--h,210) 70% 50%);color:#fff}.lb.on b{opacity:.85}
.lb:first-child{background:none;border-color:var(--line);color:var(--muted)}.lb:first-child.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.chips{display:contents}.chip{border-radius:10px;padding:1px 8px;cursor:pointer;background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 65% var(--chipL));font-weight:500}
.chip b{display:inline-block;vertical-align:bottom;max-width:0;overflow:hidden;font-weight:400;opacity:0}.chip:hover b{max-width:1em;margin-left:3px;opacity:.7}.chip b:hover{opacity:1}
.addl{border:0;background:none;width:0;padding:1px 0;color:var(--muted);outline:none;opacity:0}
.t:hover .addl,.addl:focus{opacity:1;width:58px;padding:1px 2px}.t .addl:focus,.c .addl:focus{width:180px;color:var(--text)}
details summary{display:inline-block;font-size:12px;color:var(--muted);cursor:pointer;margin-top:4px;list-style:none;user-select:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"›";display:inline-block;margin-right:5px;transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.55 -apple-system,sans-serif;max-height:280px;overflow:auto;
  background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0 0}
.quoted{display:block;margin-top:10px;padding-left:10px;border-left:2px solid var(--line);color:var(--muted)}
.more{border:0;background:none;color:var(--accent);cursor:pointer;padding:0;margin-top:8px;font-size:12px}
.lbl{display:block;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:12px 0 4px}
pre a{color:var(--accent)}
textarea{width:100%;background:var(--bg);border:1px solid transparent;border-radius:8px;padding:8px 10px;resize:none;outline:none;overflow:hidden;min-height:38px}
textarea:focus,.askm:focus{border-color:var(--accent)}
.side{flex:none;display:flex;align-items:center;gap:2px}
.due{border:0;border-radius:6px;padding:3px 8px;background:none;color:var(--muted);cursor:pointer;font-size:12px;white-space:nowrap}
.due.set{background:var(--bg);color:var(--text)}.late .due.set{background:var(--redbg);color:var(--red);font-weight:600}
.due:hover{background:var(--line)}.due.unset{opacity:0}.t:hover .due.unset{opacity:1}
.due b{font-weight:400;margin-left:4px;opacity:0}.due:hover b{opacity:.6}.due b:hover{opacity:1}
.side input{background:var(--card);border:1px solid var(--accent);border-radius:6px;padding:2px 6px;font-size:12px}
[hidden]{display:none!important}  /* .icon/.pill set display, which would otherwise beat the hidden attribute */
.icon{border:0;background:none;width:28px;height:28px;border-radius:6px;color:var(--muted);cursor:pointer;display:grid;place-content:center;opacity:.55}
.t:hover .icon{opacity:1}.icon:hover{background:var(--line);color:var(--text)}.icon.x:hover{color:var(--red)}
.icon svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
#donebox{margin-top:18px}#donebox>summary{font-size:12px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin:0 4px 6px}
.done{opacity:.65}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:var(--card);padding:6px 12px;cursor:pointer}.seg .on{background:var(--accent);color:#fff}
.calhd{display:flex;align-items:center;gap:8px;margin:14px 0 8px}.calhd h3{margin:0;font-size:16px;flex:1}
.calhd button{border:1px solid var(--line);background:var(--card);border-radius:6px;padding:4px 10px;cursor:pointer}
.calhd small{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.dow{background:var(--bg);padding:4px 8px;font-size:11px;color:var(--muted);text-transform:uppercase}
.day{background:var(--card);min-height:96px;padding:4px;cursor:pointer;overflow:hidden}
.day.out{background:var(--bg)}.day.out .dn{opacity:.5}.day.sel{box-shadow:inset 0 0 0 2px var(--accent)}.day.over{background:var(--hover)}
.dn{font-size:12px;color:var(--muted);margin:0 2px 2px}.day.today .dn{background:var(--accent);color:#fff;border-radius:9px;padding:0 6px;display:inline-block}
.ev{font-size:11.5px;border-radius:4px;padding:1px 5px;margin:2px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:grab;
  background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 65% var(--chipL))}
.ev.late{background:var(--redbg);color:var(--red);font-weight:600}.ev.done{text-decoration:line-through;opacity:.55}
.more{font-size:11px;color:var(--muted);padding:0 5px}
.askm{width:100%;box-sizing:border-box;background:var(--bg);border:1px solid transparent;border-radius:8px;padding:7px 10px;outline:none}
details .askm{border-color:color-mix(in srgb,var(--accent) 35%,transparent)}
.rbox{display:flex;gap:6px;margin-top:6px}.rbox[hidden]{display:none}.rbox .askm{margin:0;flex:1}
.rgo,.rno{border:0;border-radius:8px;padding:0 12px;background:var(--accent);color:#fff;cursor:pointer;white-space:nowrap}
.rno{background:var(--line);color:var(--text)}
.askans{white-space:pre-wrap;margin-top:6px;padding:6px 10px;border-left:3px solid var(--accent);font-size:13px}
.ai{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin-top:5px;font-size:12px;color:var(--muted)}
.act{border:1px dashed var(--line);border-radius:10px;padding:1px 8px;cursor:pointer;color:var(--text)}
.act:hover{border-color:var(--accent);color:var(--accent)}.act b{font-weight:400;margin-left:4px;opacity:.5}.act b:hover{opacity:1;color:var(--red)}
.icon.busy{opacity:1;animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.25}}
#ans{position:relative;background:var(--card);border:1px solid var(--accent);border-radius:10px;padding:10px 32px 10px 12px;margin:12px 0 0;white-space:pre-wrap}
#ans>button{position:absolute;top:6px;right:6px;border:0;background:none;cursor:pointer;color:var(--muted)}
#toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:10;background:var(--text);color:var(--bg);
  border-radius:8px;padding:8px 14px;font-size:12.5px;opacity:0;transition:opacity .2s;pointer-events:none}#toast.on{opacity:.92}
.pill[hidden]{display:none}.pill+.pill{margin-left:6px}.pill{margin-left:auto;align-self:center;display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card);
  border-radius:12px;padding:2px 10px;font-size:12px;color:var(--muted);cursor:pointer}.pill:hover{border-color:var(--accent)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.pill.ok .dot{background:#34c759}.pill.down{color:var(--red)}.pill.down .dot{background:var(--red)}
.pill.busy{color:var(--accent)}.pill.busy .dot{background:none;border:2px solid var(--accent);border-right-color:transparent;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.ev.thinking{outline:1.5px solid var(--accent);animation:pulse 1s infinite}
.argoing{display:none;color:var(--accent)}.t.thinking .argoing{display:inline}
.t.thinking{border-color:var(--accent);background:linear-gradient(90deg,var(--card) 30%,color-mix(in srgb,var(--accent) 12%,var(--card)) 50%,var(--card) 70%) 0 0/300% 100%;
  animation:shimmer 1.8s linear infinite}@keyframes shimmer{from{background-position:100% 0}to{background-position:0 0}}
#drop{display:none;position:fixed;inset:8px;z-index:9;pointer-events:none;align-items:center;justify-content:center;
  border:3px dashed var(--accent);border-radius:14px;background:#0a84ff1a;color:var(--accent);font-size:20px;font-weight:600}
#drop.over{display:flex}
</style>
<div id=drop>Drop email to add a task</div><div id=toast></div>
<header>
  <div class=top><h1>Task Manager</h1><span id=stats></span><button id=argo class=pill></button>
    <button id=rt class=pill hidden title="Retry the work Argo missed, now">Retry now</button></div>
  <div class=bar>
    <input id=new placeholder="Add a task (&quot;review Bob's draft by Fri 3pm&quot;), or drag in an email or image">
    <button id=shot class=icon hidden title="Screenshot to task: drag over an area (Space picks a window, Esc cancels). Already have one copied? Paste it here with ⌘V."><svg viewBox="0 0 24 24"><path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.5"/></svg></button>
    <div class=brk></div><input id=q type=search placeholder="Search, or ask a question ending in ?  ( / )">
    <span class=seg><button id=vl>List</button><button id=vc>Calendar</button></span>
  </div>
  <div id=status hidden></div>
  <div id=lbar></div>
</header>
<datalist id=labels></datalist>
<main><div id=ans hidden></div><div id=cal hidden></div><div id=list></div><details id=donebox><summary id=donehdr></summary><div id=donelist></div></details></main>
<script>
const api=(p,b)=>fetch(p,{method:'POST',body:typeof b==='string'||b instanceof Blob?b:JSON.stringify(b)}).then(load);
const nowStr=()=>new Date(Date.now()-new Date().getTimezoneOffset()*6e4).toISOString().slice(0,16);
const DAY=864e5, dayDiff=d=>Math.round((new Date(d).setHours(0,0,0,0)-new Date().setHours(0,0,0,0))/DAY);
const MAIL='<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6 8.5-6"/></svg>';
const REPLY='<svg viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v4"/></svg>';
function toast(m){const e=document.getElementById('toast');e.textContent=m;e.classList.add('on');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('on'),4000)}
const TRASH='<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M5.5 7l1 12a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>';
function fmtDue(s){
  const d=new Date(s),n=dayDiff(s),time=d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
  if(n===0)return 'Today '+time; if(n===1)return 'Tomorrow '+time; if(n===-1)return 'Yesterday '+time;
  if(n>1&&n<7)return d.toLocaleDateString([],{weekday:'short'})+' '+time;
  return d.toLocaleDateString([],{month:'short',day:'numeric',year:d.getFullYear()!==new Date().getFullYear()?'numeric':undefined})+' '+time;
}
function group(t){
  if(!t.due)return 'No due date'; if(t.due<=nowStr())return 'Overdue';
  const n=dayDiff(t.due); return n===0?'Today':n===1?'Tomorrow':n<7?'Next 7 days':'Later';
}
const GROUPS=['Overdue','Today','Tomorrow','Next 7 days','Later','No due date'];
function senderName(s){return (s||'').replace(/<[^>]*>/,'').replace(/"/g,'').trim()||s||''}
function shortDate(s){const d=new Date(s);return isNaN(d)?s:d.toLocaleDateString([],{month:'short',day:'numeric',year:d.getFullYear()!==new Date().getFullYear()?'numeric':undefined})}
function item(t){
  const d=document.createElement('div'),late=t.due&&!t.done&&t.due<=nowStr();
  d.className='t'+(t.done?' done':'')+(late?' late':'')+(isBusy(t.id)?' thinking':''); d.dataset.id=t.id;
  d.innerHTML=`<input type=checkbox class=cb title="Mark done">
    <div class=main><input class=title type=text spellcheck=false>
      <div class=meta><span class=argoing>✨ Argo is working on this…</span><span class=info></span><span class=chips></span><input class=addl list=labels placeholder="+ label"></div>
      <div class=rbox hidden><input class=askm placeholder="Reply instructions (optional, e.g. &quot;decline politely&quot;), then Enter"><button class=rgo>Draft in Outlook</button><button class=rno>Cancel</button></div>
</div>
    <div class=side><button class=due></button><input type=datetime-local hidden>
      <button class="icon r" title="Draft a reply with Argo">${REPLY}</button><button class="icon o" title="Open in Outlook">${MAIL}</button><button class="icon x" title="Delete">${TRASH}</button></div>
      <details><summary>Email & notes</summary><pre></pre>
        <div class=askw><span class=lbl>✨ Ask Argo</span><input class=askm placeholder="e.g. What's the deadline? (press Enter)"><div class=askans hidden></div></div>
        <span class=lbl>Notes</span><textarea rows=1 placeholder="Your notes (Argo also uses these when drafting a reply)…"></textarea></details>`;
  const $=s=>d.querySelector(s), up=p=>api(`/api/todo/${t.id}/update`,p);
  const cb=$('.cb'); cb.checked=t.done; cb.onchange=()=>up({done:cb.checked});
  const ti=$('.title'); ti.value=t.title; ti.title=t.title; ti.onchange=()=>up({title:ti.value});
  ti.onkeydown=e=>{if(e.key==='Enter')ti.blur()};
  // friendly due label; the raw date input only appears while editing
  const db=$('.due'),du=$('.side input');
  if(t.due){db.className='due set';db.textContent=fmtDue(t.due);db.title=t.due.replace('T',' ')+(t.due_auto?'\nNo deadline in the email: set to 3 days after it was sent':'');
    const c=document.createElement('b');c.textContent='×';c.title='Clear due date';
    c.onclick=e=>{e.stopPropagation();up({due:''})};db.append(c)}
  else{db.className='due unset';db.textContent='Set due'}
  du.value=t.due; du.onchange=()=>{if(du.value)up({due:du.value})};
  db.onclick=()=>{db.hidden=true;du.hidden=false;du.focus();try{du.showPicker()}catch{}};
  du.onblur=()=>{if(du.value===t.due){du.hidden=true;db.hidden=false}};
  const r=$('.r'); r.hidden=!t.eml;
  const rb=$('.rbox'),ri=$('.rbox input');
  r.onclick=()=>{if(r.disabled)return;rb.hidden=!rb.hidden;if(!rb.hidden)ri.focus()};
  ri.onkeydown=e=>{if(e.key==='Enter')draft();if(e.key==='Escape'){rb.hidden=true;ri.blur()}};
  $('.rgo').onclick=()=>draft(); $('.rno').onclick=()=>rb.hidden=true;
  const draft=async()=>{if(r.disabled)return;const how=ri.value.trim();rb.hidden=true;ri.blur();
    r.disabled=true;r.classList.add('busy');toast('Drafting a reply…');
    const res=await fetch(`/api/reply/${t.id}`,{method:'POST',body:JSON.stringify({how})}).catch(()=>({}));r.disabled=false;r.classList.remove('busy');
    load();toast(res.ok?'Draft opened in Outlook (also copied to the clipboard)':'Argo is unavailable right now (down, or not on VPN). Try again later.')};
  if(t.actions?.length){
    const a=document.createElement('div');a.className='ai';a.append('Action items:');
    for(const s of t.actions){
      const b=document.createElement('span');b.className='act';b.textContent='+ '+s;b.title='Add as its own task';
      b.onclick=()=>api(`/api/todo/${t.id}/split`,{title:s});
      const x=document.createElement('b');x.textContent='×';x.title='Dismiss';
      x.onclick=e=>{e.stopPropagation();up({actions:t.actions.filter(y=>y!==s)})};
      b.append(x);a.append(b);
    }
    $('.meta').after(a);
  }
  const am=$('details .askm'),aa=$('.askans'); $('.askw').hidden=!t.body;
  am.onkeydown=async e=>{const v=am.value.trim();if(e.key!=='Enter'||!v)return;
    aa.hidden=false;aa.textContent='Asking Argo…';
    const res=await fetch('/api/ask',{method:'POST',body:JSON.stringify({q:v,id:t.id})}).catch(()=>({}));
    aa.textContent=res.ok?(await res.json()).answer:'Argo is unavailable right now (down, or not on VPN). Try again later.'};
  const o=$('.o'); o.hidden=!t.eml; o.onclick=()=>api(`/api/todo/${t.id}/open`);
  $('.x').onclick=()=>confirm(`Delete "${t.title}"?`)&&api(`/api/todo/${t.id}/delete`);
  const info=$('.info'); info.textContent=[senderName(t.sender),t.date&&shortDate(t.date)].filter(Boolean).join(' · ');
  info.title=[t.sender,t.date].filter(Boolean).join('\n');
  const labels=t.labels||[];
  for(const l of labels){
    const c=document.createElement('span');c.className='chip';c.style.setProperty('--h',hue(l));
    c.textContent=l;c.title='Show only this label';c.onclick=()=>{lf=l;render()};
    const x=document.createElement('b');x.textContent='×';x.title='Remove label';
    x.onclick=e=>{e.stopPropagation();up({labels:labels.filter(y=>y!==l)})};
    c.append(x);$('.chips').append(c);
  }
  const al=$('.addl'); al.onchange=()=>{const v=al.value.trim();if(v)up({labels:[...labels,v]})};
  const hasBody=t.body&&t.body!==t.title, pre=$('pre'); if(hasBody)showMail(pre,t.body); else pre.remove();
  const n=$('textarea'); n.value=t.note; n.onchange=()=>up({note:n.value});
  const grow=()=>{n.style.height='auto';n.style.height=n.scrollHeight+2+'px'}; n.oninput=grow;
  $('details').ontoggle=grow;
  if(t.shot){const a=document.createElement('a'),i=document.createElement('img');a.href=i.src=`/api/shot/${t.mail||t.id}`;a.target='_blank';
    i.className='shotimg';i.title='Open full size';a.append(i);$('details summary').after(a)}
  $('summary').textContent=(t.shot?'Screenshot & notes':hasBody?'Email & notes':'Notes')+(t.note?' ✎':'');
  return d;
}
// Outlook Safe Links / urldefense wrap every URL; show the real one, keep the wrapped href for the security scan
function pretty(u){
  try{const x=new URL(u);if(x.hostname.endsWith('safelinks.protection.outlook.com'))u=x.searchParams.get('url')||u}catch{}
  const d=u.match(/urldefense\.\w+\/v3\/__(.+?)__;/);if(d)u=d[1];
  return u.length>90?u.slice(0,87)+'…':u;
}
// display only (Argo still reads the full text): drop Outlook's external-sender banners, fold the quoted thread
function showMail(el,text){
  text=text.replace(/^(This Message Is From an External Sender|This message came from outside your organization\.?)\s*$/gim,'').replace(/\n{3,}/g,'\n\n').trim();
  const q=text.search(/^(From: .+|On .+wrote:|-{2,} ?Original Message ?-{2,})$/m);
  if(q<=0)return linkify(el,text);
  linkify(el,text.slice(0,q).trim());
  const old=document.createElement('span');old.className='quoted';old.hidden=true;linkify(old,text.slice(q));
  const b=document.createElement('button');b.className='more';b.textContent='Show earlier messages';
  b.onclick=()=>{old.hidden=!old.hidden;b.textContent=old.hidden?'Show earlier messages':'Hide earlier messages'};
  el.append(document.createElement('br'),b,old);
}
function linkify(el,text){
  const re=/(?:https?:\/\/|mailto:)[^\s<>"]*[^\s<>".,;)]/g;let i=0,m;
  while((m=re.exec(text))){
    el.append(text.slice(i,m.index));
    const a=document.createElement('a');a.href=m[0];a.target='_blank';a.title=m[0];a.textContent=pretty(m[0]);
    el.append(a);i=re.lastIndex;
  }
  el.append(text.slice(i));
}
let LB={all:[],hues:{}};  // shared labels: names from all three windows, colours picked in the Library
const hue=s=>LB.hues[s]??[...s].reduce((h,c)=>(h*31+c.charCodeAt(0))%360,7);
let all=[], view=localStorage.view||'list', cm=new Date(), sel='', dragId=null;
cm.setDate(1);
const ymd=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
function renderCal(shown){
  const el=(tag,cls,txt)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e};
  const by={};for(const t of shown.filter(t=>t.due).sort((a,b)=>a.due.localeCompare(b.due)))(by[t.due.slice(0,10)]||=[]).push(t);
  const y=cm.getFullYear(),m=cm.getMonth(),lead=new Date(y,m,1).getDay(),days=new Date(y,m+1,0).getDate();
  const go=n=>{cm=n?new Date(y,m+n,1):new Date(new Date().getFullYear(),new Date().getMonth(),1);render()};
  const hd=el('div','calhd'),title=el('h3',0,new Date(y,m,1).toLocaleDateString([],{month:'long',year:'numeric'}));
  const undated=shown.filter(t=>!t.due&&!t.done).length;
  hd.append(title,...(undated?[el('small',0,`${undated} without a due date`)]:[]));
  for(const [l,n] of [['‹',-1],['Today',0],['›',1]]){const b=el('button',0,l);b.onclick=()=>go(n);hd.append(b)}
  const grid=el('div','grid');
  for(const d of ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'])grid.append(el('div','dow',d));
  const today=ymd(new Date());
  for(let i=0;i<Math.ceil((lead+days)/7)*7;i++){
    const d=new Date(y,m,1-lead+i),k=ymd(d),c=el('div','day'+(d.getMonth()!==m?' out':'')+(k===today?' today':'')+(k===sel?' sel':''));
    c.append(el('div','dn',d.getDate()));
    const ts=by[k]||[];
    for(const t of ts.slice(0,3)){
      const e=el('div','ev'+(t.done?' done':'')+(!t.done&&t.due<=nowStr()?' late':''),t.title);
      e.style.setProperty('--h',(t.labels||[])[0]?hue(t.labels[0]):210);e.title=t.title+'\n'+fmtDue(t.due);
      e.dataset.id=t.id;if(isBusy(t.id))e.classList.add('thinking');
      e.draggable=true;e.ondragstart=ev=>{dragId=t.id;ev.dataTransfer.setData('application/x-todo',t.id)};e.ondragend=()=>dragId=null;
      c.append(e);
    }
    if(ts.length>3)c.append(el('div','more',`+${ts.length-3} more`));
    c.onclick=()=>{sel=sel===k?'':k;render()};
    c.ondragover=e=>{if(dragId){e.preventDefault();c.classList.add('over')}};
    c.ondragleave=()=>c.classList.remove('over');
    c.ondrop=e=>{if(!dragId)return;e.preventDefault();e.stopPropagation();c.classList.remove('over');
      const t=all.find(x=>x.id===dragId);dragId=null;if(t&&t.due.slice(0,10)!==k)api(`/api/todo/${t.id}/update`,{due:k+t.due.slice(10)})};
    grid.append(c);
  }
  const out=[hd,grid];
  if(sel){const ts=by[sel]||[];out.push(el('h2',0,new Date(sel+'T00:00').toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'})),
    ...(ts.length?ts.map(item):[el('div','empty','Nothing due this day.')]))}
  cal.replaceChildren(...out);
}
let lf='';  // label filter ('' = all), set by the label row under the search bar or by clicking a label
function render(){
  const names=[...new Set(all.flatMap(t=>t.labels||[]))].sort();
  labels.replaceChildren(...[...new Set([...names,...LB.all])].sort().map(n=>new Option(n)));
  if(!names.includes(lf))lf='';
  const lchip=(name,text,n)=>{const c=document.createElement('button');c.className='lb'+(lf===name?' on':'');
    if(name)c.style.setProperty('--h',hue(name));c.textContent=text;if(n){const b=document.createElement('b');b.textContent=n;c.append(b)}
    c.onclick=()=>{lf=lf===name?'':name;render()};return c};
  const undone=all.filter(t=>!t.done);
  lbar.replaceChildren(lchip('','All labels'),...names.map(n=>lchip(n,n,undone.filter(t=>(t.labels||[]).includes(n)).length||'')));
  const words=q.value.trim().endsWith('?')?[]:q.value.toLowerCase().split(/\s+/).filter(Boolean);
  const shown=all.filter(t=>(!lf||(t.labels||[]).includes(lf))&&words.every(w=>
    [t.title,t.sender,t.body,t.note,...(t.labels||[])].join(' ').toLowerCase().includes(w)));
  const open=shown.filter(t=>!t.done).sort((a,b)=>(a.due||'z').localeCompare(b.due||'z'));
  const done=shown.filter(t=>t.done);
  const out=[];
  for(const g of GROUPS){
    const ts=open.filter(t=>group(t)===g); if(!ts.length)continue;
    const h=document.createElement('h2');h.className=g==='Overdue'?'late':'';h.textContent=g;
    const c=document.createElement('span');c.textContent=ts.length;h.append(c);
    out.push(h,...ts.map(item));
  }
  if(!open.length){
    const e=document.createElement('div');e.className='empty';
    e.textContent=all.some(t=>!t.done)?'No tasks match your search.':'All clear. Drag an email from Outlook here to add a task.';
    out.push(e);
  }
  list.replaceChildren(...out); donelist.replaceChildren(...done.map(item));
  const isCal=view==='cal'; cal.hidden=!isCal; list.hidden=isCal; vl.className=isCal?'':'on'; vc.className=isCal?'on':'';
  if(isCal)renderCal(shown);
  donebox.hidden=isCal||!done.length; donehdr.textContent=`Done · ${done.length}`;
  const openAll=all.filter(t=>!t.done), late=openAll.filter(t=>t.due&&t.due<=nowStr()).length;
  stats.textContent=`${openAll.length} open`+(late?` · ${late} overdue`:'');
  document.title=`Task Manager (${openAll.length})`;
}
let st={busy:[]}, rev=0;
async function load(){
  [all,st,LB]=await Promise.all(['/api/todos','/api/status','/api/labels'].map(u=>fetch(u).then(r=>r.json())));rev=st.rev;render();pill()}
function pill(){  // Argo status in the header: off / reachable / unavailable / working
  const work=st.busy.filter(x=>x!=='check'), checking=st.busy.includes('check'), waiting=all.filter(t=>t.retry&&!t.done).length;
  const ago=st.checked?Math.round(Date.now()/6e4-st.checked/60):null;
  argo.className='pill'+(!st.configured?'':work.length||checking?' busy':st.argo?' ok':st.argo===false?' down':'');
  const dot=document.createElement('span');dot.className='dot';
  argo.replaceChildren(dot,!st.configured?'Argo off':work.length?`Argo working${work.length>1?` on ${work.length}`:''}…`
    :checking?'Checking Argo…':st.argo===false?'Argo unavailable'+(waiting?` · ${waiting} waiting`:''):'Argo');
  rt.hidden=!st.configured||!waiting;
  argo.title=!st.configured?'LLM features are off. Set "argo_user" in data/config.json to turn them on.'
    :(st.argo===false?'Argo is down, or you are off VPN. Waiting work retries '+(st.retry_minutes?'every '+st.retry_minutes+' min':'only when you press Retry now')+'.':st.argo?'Argo is reachable.':'Not checked yet.')
    +(ago!=null?` Last contact ${ago<1?'just now':ago<60?ago+' min ago':Math.round(ago/60)+' h ago'}.`:'')+' Click to check now.';
}
async function tick(){
  try{st=await (await fetch('/api/status')).json()}catch{return}
  pill();
  for(const e of document.querySelectorAll('[data-id]'))e.classList.toggle('thinking',isBusy(e.dataset.id));
  // new inputs (typed, dropped, pasted, screenshots): a status line while Argo reads them, then jump to the finished task
  const now=st.busy.filter(b=>b.startsWith('new:')).map(b=>b.slice(4)), ae=document.activeElement,
    idle=(ae===document.body||ae.id==='new'&&!ae.value)&&!document.querySelector('.t details[open]');  // just pressed Enter in the add box counts as idle
  for(const id of reading)if(!now.includes(id))arrived.push(id);
  reading=now;
  if(reading.length){const t=all.find(t=>t.id===reading[0]);
    showStatus(`✨ Argo is reading ${!t?'a new task':t.shot&&t.title==='Screenshot'?'your screenshot':`“${t.title}”`}…`+(reading.length>1?` (+${reading.length-1} more)`:''))}
  else if(!arrived.length&&!statusEl.classList.contains('done'))statusEl.hidden=true;
  if(arrived.length&&idle){await load();jump(arrived.splice(0))}
  // data changed on the server (LLM fill-ins, retries, Scout 📌): reload, unless the user is in the middle of something
  else if(st.rev!==rev&&idle)load();
}
const isBusy=id=>st.busy.includes(id)||st.busy.includes('new:'+id);
let reading=[], arrived=[];
const statusEl=document.getElementById('status');  // not window.status: that's a built-in string
function showStatus(text,done){statusEl.textContent=text;statusEl.classList.toggle('done',!!done);statusEl.hidden=false;
  clearTimeout(showStatus.t);if(done)showStatus.t=setTimeout(()=>{statusEl.hidden=true;statusEl.classList.remove('done')},4000)}
function jump(ids){  // scroll to the last finished task and flash it
  const t=all.find(t=>t.id===ids.at(-1)), e=document.querySelector(`.t[data-id="${ids.at(-1)}"]`);
  if(t)showStatus(`✓ Ready: “${t.title}”`+(ids.length>1?` and ${ids.length-1} more`:''),true);
  if(!e)return;  // filtered out, or in the calendar view
  e.scrollIntoView({block:'center',behavior:'smooth'});e.classList.remove('flash');void e.offsetWidth;e.classList.add('flash');
}
argo.onclick=()=>{fetch('/api/argo/check',{method:'POST'}).then(tick);setTimeout(tick,150)};
rt.onclick=()=>{rt.disabled=true;fetch('/api/argo/retry',{method:'POST'}).then(()=>{rt.disabled=false;tick();load()});setTimeout(tick,150)};

setInterval(tick,2000);
q.oninput=render;
vl.onclick=()=>{view='list';localStorage.view=view;render()};vc.onclick=()=>{view='cal';localStorage.view=view;render()};
document.getElementById('new').onkeydown=e=>{
  const v=e.target.value.trim();
  if(e.key==='Enter'&&v){e.target.value='';api('/api/drop?name=todo.txt',v)}
  if(e.key==='Escape')e.target.blur();
};
addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){e.preventDefault();q.focus()}});
function answer(text){const b=document.createElement('button');b.textContent='×';b.title='Close';b.onclick=()=>ans.hidden=true;
  ans.replaceChildren(text,b);ans.hidden=false}
q.onkeydown=async e=>{
  if(e.key==='Escape'){q.value='';ans.hidden=true;render();q.blur()}
  const v=q.value.trim();
  if(e.key==='Enter'&&v.endsWith('?')){
    answer('Asking Argo…');
    const r=await fetch('/api/ask',{method:'POST',body:JSON.stringify({q:v})}).catch(()=>({}));
    load();answer(r.ok?(await r.json()).answer:'Argo is unavailable right now (down, or not on VPN). Try again later.');
  }
};
addEventListener('dragover',e=>{e.preventDefault();if(!dragId)drop.classList.add('over')});
addEventListener('dragleave',e=>{if(!e.relatedTarget)drop.classList.remove('over')});  // only when leaving the window
addEventListener('drop',async e=>{
  e.preventDefault(); if(dragId)return; drop.classList.remove('over');
  const files=[...e.dataTransfer.files];
  for(const f of files) await fetch('/api/drop?name='+encodeURIComponent(f.name),{method:'POST',body:f});
  const txt=!files.length&&(e.dataTransfer.getData('text/plain')||e.dataTransfer.getData('text/uri-list'));
  if(txt) await fetch('/api/drop?name=drop.txt',{method:'POST',body:txt});
  load();
});
// screenshots: the camera button (Mac app: it hides the window and runs the area picker), or ⌘V an image copied with ⌃⇧⌘4
const shotBtn=document.getElementById('shot');shotBtn.hidden=!window.webkit?.messageHandlers?.shot;
shotBtn.onclick=()=>webkit.messageHandlers.shot.postMessage('');
addEventListener('paste',async e=>{
  const f=[...e.clipboardData.files].find(f=>f.type.startsWith('image/'));if(!f)return;e.preventDefault();
  toast('Reading the screenshot…');
  await fetch('/api/drop?name=paste.'+(f.type.split('/')[1]||'png'),{method:'POST',body:f});load();
});
setInterval(()=>{if(!document.querySelector('.t details[open]')&&document.activeElement===document.body)load()},60000);  // due-date groups move with the clock
load();
</script>"""

FEED_PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Scout</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#e2e2e7;--hover:#f0f0f4;--accent:#0a84ff;--gold:#b25000}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--card:#2a2a2d;--text:#f2f2f7;--muted:#98989f;--line:#3a3a3d;--hover:#323236;--gold:#ffb340}}
*{box-sizing:border-box}
body{font:13.5px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;background:var(--bg);color:var(--text)}
button,input{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:blur(12px);
  padding:14px 20px 10px;border-bottom:1px solid var(--line)}
.top{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}h1{font-size:20px;margin:0}#stats{color:var(--muted);flex:1}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.bar input[type=search]{flex:1;min-width:160px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
.bar input:focus{border-color:var(--accent)}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button,.btn{border:0;background:var(--card);padding:6px 12px;cursor:pointer}.seg .on{background:var(--accent);color:#fff}
.btn{border:1px solid var(--line);border-radius:8px}.btn:hover{background:var(--hover)}
label{color:var(--muted);font-size:12px;display:flex;gap:4px;align-items:center}
#errs{color:var(--gold);font-size:12px;margin-top:6px}
main{padding:6px 20px 40px}
.empty{text-align:center;color:var(--muted);padding:60px 0;font-size:15px}
.c{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:6px 0}
.c:hover{background:var(--hover)}.c.read{opacity:.6}
.score{flex:none;width:34px;height:34px;border-radius:9px;display:grid;place-content:center;font-weight:700;font-size:14px;
  background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 60% 40%)}
@media (prefers-color-scheme:dark){.score{color:hsl(var(--h) 70% 72%)}}
.body{flex:1;min-width:0}
.title{font-weight:600;font-size:14px;color:inherit;text-decoration:none}.title:hover{color:var(--accent);text-decoration:underline}
.meta{color:var(--muted);font-size:12px;margin-top:2px}.src{font-weight:600}
.why{margin-top:4px;font-style:italic}
details summary{font-size:12px;color:var(--muted);cursor:pointer;margin-top:4px;list-style:none}
details summary::-webkit-details-marker{display:none}details p{margin:6px 0 0;font-size:12.5px;line-height:1.5}
.acts{flex:none;display:flex;gap:2px;align-items:flex-start}
.acts button{border:0;background:none;border-radius:6px;width:30px;height:28px;cursor:pointer;opacity:.35;font-size:14px}
.c:hover .acts button{opacity:.8}.acts button:hover{background:var(--line);opacity:1}.acts .on{opacity:1!important}
dialog.sheet{width:min(760px,calc(100vw - 32px));max-height:calc(100vh - 48px);border:1px solid var(--line);border-radius:14px;
  padding:0;background:var(--card);color:var(--text);box-shadow:0 24px 70px #0005}
dialog.sheet::backdrop{background:#0006;backdrop-filter:blur(2px)}
dialog.sheet form{display:flex;flex-direction:column;max-height:calc(100vh - 50px)}
dialog#adddlg{width:min(580px,calc(100vw - 32px))}
.alog{font-size:12.5px;margin-top:8px;overflow-wrap:anywhere}.alog.bad{color:var(--gold)}
.ph{padding:18px 22px 4px}.ph h3{margin:0;font-size:17px}.ph p{margin:3px 0 0;color:var(--muted);font-size:12.5px}
.pb{padding:4px 22px 18px;overflow:auto}
.sec{margin-top:18px}.sec b{display:block;font-size:13px}.sec small{display:block;color:var(--muted);font-size:12px;margin:2px 0 7px}
.sec small.err{color:var(--gold);margin:5px 0 0;min-height:0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media (max-width:600px){.two{grid-template-columns:1fr}}
.sheet textarea{width:100%;font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 11px;
  outline:none;resize:vertical;line-height:1.45}
.sheet textarea:focus,.tags:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px #0a84ff26}
.tags{display:flex;flex-wrap:wrap;gap:6px;align-items:center;min-height:40px;background:var(--bg);border:1px solid var(--line);
  border-radius:8px;padding:6px;cursor:text}
.tag{display:inline-flex;align-items:center;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:2px 3px 2px 10px;font-size:12.5px;max-width:100%}
.tag a,.tag span{color:inherit;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tag a:hover{color:var(--accent)}
.tag button{flex:none;border:0;background:none;width:18px;height:18px;border-radius:9px;cursor:pointer;color:var(--muted);padding:0;font-size:13px;line-height:1}
.tag button:hover{background:var(--line);color:var(--text)}
.tags input{flex:1;min-width:150px;border:0;background:none;outline:none;padding:3px 4px;font:inherit;font-size:12.5px;color:inherit}
.chan .tag::before{content:"▶";font-size:8px;color:#e33;margin-right:5px}
.pf{display:flex;gap:8px;align-items:center;padding:12px 22px;border-top:1px solid var(--line)}
.pf .msg{flex:1;font-size:12px;color:var(--muted)}.pf .msg.bad{color:var(--gold)}
.pf label{font-size:12.5px;gap:6px}
.pf select{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:4px 6px}
.linkbtn{border:0;background:none;color:var(--muted);cursor:pointer;padding:6px 4px;font-size:12.5px}.linkbtn:hover{color:var(--text);text-decoration:underline}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn.primary:disabled{opacity:.45;cursor:default}
:root{--chipL:32%}@media (prefers-color-scheme:dark){:root{--chipL:76%}}
.chip{border-radius:10px;padding:1px 8px;margin-left:6px;cursor:pointer;background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 65% var(--chipL));font-weight:500}
.chip b{display:inline-block;vertical-align:bottom;max-width:0;overflow:hidden;font-weight:400;opacity:0}.chip:hover b{max-width:1em;margin-left:3px;opacity:.7}
.addl{border:1px dashed var(--line);border-radius:10px;background:none;width:62px;padding:0 7px;margin-left:6px;color:var(--muted);outline:none;font-size:12px}
.addl:hover{border-color:var(--muted)}.addl:focus{width:160px;color:var(--text);border-style:solid;border-color:var(--accent)}
</style>
<datalist id=labels></datalist>
<header>
  <div class=top><h1>Scout</h1><span id=stats></span>
    <button class=btn id=addbtn title="Save a paper, video, repo or page by its link">+ Add</button><button class=btn id=prefsbtn>Interests</button><button class=btn id=allread>Mark all read</button><button class=btn id=refresh>Check now</button></div>
  <div class=bar>
    <span class=seg id=srcs></span>
    <input id=q type=search placeholder="Search  ( / )">
    <label><input type=checkbox id=low> Show low scores</label>
  </div>
  <div id=errs></div>
</header>
<main id=list></main>
<dialog id=prefs class=sheet><form id=pform>
  <div class=ph><h3>Scout interests</h3><p>What Scout searches for, and what Argo ranks highest. Saving runs a fresh check.</p></div>
  <div class=pb>
    <div class=sec style="margin-top:10px"><b>What you care about</b>
      <small>Describe it in plain words. Every item is scored 0–10 against this.</small>
      <textarea id=p_profile rows=4></textarea></div>
    <div class="sec two">
      <div><b>Papers &amp; repos</b><small>Searched on arXiv and GitHub</small><div class=tags id=t_kw></div><small class=err></small></div>
      <div><b>Papers only</b><small>Broad terms for arXiv only (they'd flood GitHub)</small><div class=tags id=t_ax></div><small class=err></small></div>
    </div>
    <div class=sec><b>YouTube channels</b><small>New videos from these channels. Add one by @handle or channel link.</small>
      <div class="tags chan" id=t_yt></div><small class=err></small></div>
  </div>
  <div class=pf><label>Check every <select id=p_hours></select></label><span class=msg id=p_msg></span>
    <button type=button class=linkbtn id=p_reset>Reset to defaults</button>
    <button type=button class=btn id=p_cancel>Cancel</button><button class="btn primary" id=p_save>Save</button></div>
</form></dialog>
<dialog id=adddlg class=sheet><form id=aform>
  <div class=ph><h3>Save a link</h3><p>An arXiv paper, YouTube video, GitHub repo, or any web page. It goes straight to your Library.</p></div>
  <div class=pb><div class=sec style="margin-top:10px"><textarea id=a_urls rows=3
    placeholder="Paste a link and press Enter (several links: one per line)"></textarea><div id=a_log></div></div></div>
  <div class=pf><span class=msg></span><button type=button class=btn id=a_cancel>Close</button><button class="btn primary" id=a_save>Save</button></div>
</form></dialog>
<script>
const SRCS=['All','arXiv','YouTube','GitHub'];
let feed={items:[]}, src=SRCS.includes(localStorage.src)?localStorage.src:'All', LB={all:[],hues:{}};  // labels shared with Task Manager and the Library
const hue=s=>LB.hues[s]??[...s].reduce((h,c)=>(h*31+c.charCodeAt(0))%360,7);
const post=b=>fetch('/api/feed/update',{method:'POST',body:JSON.stringify(b)}).then(load);
function ago(s){if(!s)return 'never';const m=Math.round((Date.now()-new Date(s))/6e4);
  return m<1?'just now':m<60?m+' min ago':m<1440?Math.round(m/60)+' h ago':Math.round(m/1440)+' d ago'}
function card(x){
  const d=document.createElement('div');d.className='c'+(x.read?' read':'');
  d.innerHTML=`<div class=score></div><div class=body><a class=title target=_blank></a>
    <div class=meta><span class=src></span> · <span class=by></span><span class=chips></span><input class=addl list=labels placeholder="+ label" title="Labels are shared with Task Manager and the Library"></div><div class=why></div>
    <details><summary>Details</summary><p></p></details></div>
    <div class=acts><button title="More like this">👍</button><button title="Less like this">👎</button><button title="Move to the Library">📚</button><button title="Hide (doesn't affect ranking)">✕</button><button title="Add to Task Manager as a reading task">📌</button></div>`;
  const $=s=>d.querySelector(s);
  const sc=$('.score');sc.textContent=x.score==null?'·':Math.round(x.score);
  sc.title=x.score==null?'Not ranked yet (Argo unreachable?)':'Score '+x.score;
  sc.style.setProperty('--h',x.score==null?220:Math.round(x.score*12));
  const a=$('.title');a.href=x.url;a.textContent=x.title;a.onclick=()=>{if(!x.read)post({id:x.id,read:true})};
  $('.src').textContent=x.src;$('.by').textContent=[x.by,x.date,x.manual&&'added by you'].filter(Boolean).join(' · ');
  $('.why').textContent=x.why;
  const ls=x.labels||[];
  for(const l of ls){const c=document.createElement('span');c.className='chip';c.textContent=l;c.style.setProperty('--h',hue(l));
    c.title='Show only this label';c.onclick=()=>{q.value=l;render()};
    const r=document.createElement('b');r.textContent='×';r.title='Remove label';r.onclick=e=>{e.stopPropagation();post({id:x.id,labels:ls.filter(y=>y!==l)})};
    c.append(r);$('.chips').append(c)}
  const al=$('.addl');al.onchange=()=>{const v=al.value.trim();if(v)post({id:x.id,labels:[...ls,v]})};
  if(x.text)$('details p').textContent=x.text;else $('details').remove();
  const [up,down,save,hide,task]=d.querySelectorAll('.acts button');
  hide.onclick=()=>post({id:x.id,hidden:true,read:true});  // hidden, but not sent to Argo as a dislike
  task.classList.toggle('on',!!x.task);if(x.task)task.title='Added to Task Manager';
  task.onclick=()=>x.task||fetch('/api/feed/task',{method:'POST',body:JSON.stringify({id:x.id})}).then(load);
  up.classList.toggle('on',x.vote>0);down.classList.toggle('on',x.vote<0);
  up.onclick=()=>post({id:x.id,vote:x.vote>0?0:1});down.onclick=()=>post({id:x.id,vote:x.vote<0?0:-1,read:true});
  save.onclick=()=>fetch('/api/feed/library',{method:'POST',body:JSON.stringify({id:x.id})}).then(load);
  return d;
}
function render(){
  srcs.replaceChildren(...SRCS.map(s=>{const b=document.createElement('button');b.textContent=s;b.className=s===src?'on':'';
    b.onclick=()=>{src=s;localStorage.src=s;render()};return b}));
  const words=q.value.toLowerCase().split(/\s+/).filter(Boolean);
  const shown=feed.items.filter(x=>!x.saved&&!x.hidden&&(src==='All'||x.src===src)
    &&x.vote>=0&&(low.checked||x.score==null||x.score>=5)
    &&words.every(w=>[x.title,x.by,x.text,x.why,...(x.labels||[])].join(' ').toLowerCase().includes(w)))
    .sort((a,b)=>((b.score??4.5)-(a.score??4.5))||b.date.localeCompare(a.date));
  list.replaceChildren(...shown.slice(0,300).map(card));
  if(!shown.length){const e=document.createElement('div');e.className='empty';
    e.textContent=feed.last?'Nothing here yet.':'First check is running…';list.append(e)}
  const picks=feed.items.filter(x=>!x.read&&x.vote>=0&&x.score>=7).length;
  stats.textContent=(feed.busy?'Checking… · ':'')+`Last checked ${ago(feed.last)}`+(picks?` · ${picks} unread pick${picks>1?'s':''}`:'');
  errs.textContent=(feed.errors||[]).map(e=>'⚠︎ '+e).join('  ');
  document.title=picks?`Scout (${picks})`:'Scout';
}
async function load(){[feed,LB]=await Promise.all(['/api/feed','/api/labels'].map(u=>fetch(u).then(r=>r.json())));
  labels.replaceChildren(...LB.all.map(n=>new Option(n)));render()}
q.oninput=low.onchange=render;
refresh.onclick=()=>fetch('/api/feed/refresh',{method:'POST'}).then(()=>setTimeout(load,500));
allread.onclick=()=>post({all_read:true});
// Tag input: Enter (or comma) adds, × or Backspace removes, pasting several lines adds them all.
// For YouTube, each entry is looked up first and stored as "UC... name".
function tags(el,ph,lookup){
  let vals=[];const inp=document.createElement('input'),err=el.nextElementSibling;inp.placeholder=ph;
  const key=v=>lookup?v.split(' ')[0]:v.toLowerCase();
  function draw(){
    el.replaceChildren(...vals.map((v,i)=>{
      const t=document.createElement('span');t.className='tag';
      const id=v.split(' ')[0],l=document.createElement(lookup?'a':'span');
      l.textContent=lookup?v.split(' ').slice(1).join(' ')||id:v;
      if(lookup){l.href='https://www.youtube.com/channel/'+id;l.target='_blank';l.title=id}
      const x=document.createElement('button');x.type='button';x.textContent='×';x.title='Remove';
      x.onclick=()=>{vals.splice(i,1);draw();changed();inp.focus()};
      t.append(l,x);return t}),inp);
  }
  async function add(raw){
    err.textContent='';
    for(const it of raw.split(lookup?/\n/:/[\n,]/).map(s=>s.trim()).filter(Boolean)){
      let v=it;
      if(lookup){
        inp.disabled=true;inp.value='';inp.placeholder=`Looking up ${it}…`;
        const r=await fetch('/api/scout/channel',{method:'POST',body:JSON.stringify({q:it})}),j=await r.json();
        inp.disabled=false;inp.placeholder=ph;
        if(!r.ok){err.textContent=`${it}: ${j.error}`;inp.value=it;inp.focus();return}
        v=j.line;
      }
      if(vals.some(x=>key(x)===key(v)))err.textContent=`Already added: ${lookup?v.split(' ').slice(1).join(' '):v}`;else vals.push(v);
    }
    inp.value='';draw();changed();inp.focus();
  }
  inp.onkeydown=e=>{
    if(e.key==='Enter'||(e.key===','&&!lookup)){e.preventDefault();if(inp.value.trim())add(inp.value)}
    else if(e.key==='Backspace'&&!inp.value&&vals.length){vals.pop();draw();changed();inp.focus()}
  };
  inp.onpaste=e=>{const t=e.clipboardData.getData('text');if(t.includes('\n')){e.preventDefault();add(t)}};
  inp.onblur=()=>{if(!lookup&&inp.value.trim())add(inp.value)};
  el.onclick=e=>{if(e.target===el)inp.focus()};
  return {set(v){vals=[...v];err.textContent='';inp.value='';draw()},get:()=>vals};
}
const kw=tags(t_kw,'Add a term…'),ax=tags(t_ax,'Add a term…'),yt=tags(t_yt,'Add @handle or channel link, then Enter',true);
const state=()=>JSON.stringify({scout_profile:p_profile.value.trim(),scout_keywords:kw.get(),scout_arxiv_keywords:ax.get(),
  scout_youtube:yt.get(),scout_hours:+p_hours.value});
let saved='';
function changed(){p_save.disabled=state()===saved}
p_profile.oninput=p_hours.onchange=changed;
async function openPrefs(){
  const c=await (await fetch('/api/scout/settings')).json();
  p_profile.value=c.scout_profile;kw.set(c.scout_keywords);ax.set(c.scout_arxiv_keywords);yt.set(c.scout_youtube);
  p_hours.replaceChildren(...[...new Set([1,2,3,6,12,24,c.scout_hours])].sort((a,b)=>a-b)
    .map(h=>new Option(h===1?'hour':h===24?'day':`${h} hours`,h)));
  p_hours.value=c.scout_hours;p_msg.textContent='';p_msg.className='msg';saved=state();changed();prefs.showModal();
}
async function savePrefs(body){
  p_msg.className='msg';p_msg.textContent=body.reset?'Resetting…':'Saving…';
  const r=await fetch('/api/scout/settings',{method:'POST',body:JSON.stringify(body)});
  if(!r.ok){p_msg.className='msg bad';p_msg.textContent=(await r.json()).error;return}
  prefs.close();setTimeout(load,500);
}
const discard=()=>p_save.disabled||confirm('Discard your changes?');
prefsbtn.onclick=openPrefs;
addbtn.onclick=()=>{a_urls.value='';a_log.replaceChildren();a_save.disabled=false;adddlg.showModal();a_urls.focus()};
a_cancel.onclick=()=>adddlg.close();
a_urls.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();aform.requestSubmit()}};
aform.onsubmit=async e=>{
  e.preventDefault();
  const urls=a_urls.value.split(/\s+/).filter(Boolean),bad=[];if(!urls.length)return;
  a_save.disabled=true;a_log.replaceChildren();
  for(const u of urls){
    const row=document.createElement('div');row.className='alog';row.textContent=`Reading ${u}…`;a_log.append(row);
    const r=await fetch('/api/feed/add',{method:'POST',body:JSON.stringify({url:u})}),j=await r.json();
    if(r.ok)row.textContent=(j.existing?'📚 Already in the Library: ':'📚 Added to the Library: ')+j.title;
    else{row.textContent=`⚠︎ ${u}: ${j.error}`;row.classList.add('bad');bad.push(u)}
  }
  a_save.disabled=false;a_urls.value=bad.join('\n');
  src='All';localStorage.src=src;load();setTimeout(load,10000);  // Argo's score lands a few seconds later
  if(!bad.length)setTimeout(()=>adddlg.close(),1200);
};
p_cancel.onclick=()=>discard()&&prefs.close();
prefs.oncancel=e=>{if(!discard())e.preventDefault()};  // Esc
p_reset.onclick=()=>confirm('Reset interests, search terms and channels to the defaults?')&&savePrefs({reset:true});
pform.onsubmit=e=>{e.preventDefault();savePrefs(JSON.parse(state()))};
addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();q.focus()}});
q.onkeydown=e=>{if(e.key==='Escape'){q.value='';render();q.blur()}};
let tick=0;setInterval(()=>{if((feed.busy||++tick%6===0)&&!document.querySelector('details[open]')&&!document.activeElement.classList.contains('addl'))load()},5000);
load();
</script>"""

SETTINGS_PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Settings</title>
<style>
:root{color-scheme:light dark;--bg:#ececec;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#dcdce0;--accent:#0a84ff;--red:#d70015;--green:#248a3d}
@media (prefers-color-scheme:dark){:root{--bg:#1e1e1e;--card:#2a2a2d;--text:#f2f2f7;--muted:#98989f;--line:#3a3a3d;--red:#ff6961;--green:#30d158}}
*{box-sizing:border-box}
body{font:13px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:18px 20px;background:var(--bg);color:var(--text);-webkit-user-select:none;cursor:default}
h2{font-size:13px;font-weight:600;margin:16px 0 6px}h2:first-child{margin-top:0}
.group{background:var(--card);border:1px solid var(--line);border-radius:10px}
.r{display:flex;align-items:center;gap:12px;padding:8px 12px;min-height:38px}.r+.r{border-top:1px solid var(--line)}
.r>label{flex:1}.r small{display:block;color:var(--muted);font-size:11.5px}
input,select,textarea{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 7px;width:220px;-webkit-user-select:text}
textarea{width:100%;margin-top:6px;resize:vertical;font:12px/1.4 ui-monospace,Menlo,monospace}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:6px;padding:3px 12px}
.ok{color:var(--green)}.bad{color:var(--red)}#res{font-size:12px}
.foot{color:var(--muted);font-size:11.5px;margin-top:12px;display:flex;justify-content:space-between}
</style>
<h2>Argo</h2>
<div class=group>
  <div class=r><label for=argo_user>ANL username<small>Blank turns all LLM features off</small></label><input id=argo_user autocomplete=off spellcheck=false></div>
  <div class=r><label for=argo_model>Model</label><input id=argo_model spellcheck=false placeholder=claudesonnet55></div>
  <div class=r><label for=retry_minutes>Retry missed work<small>When Argo was down or off VPN</small></label><select id=retry_minutes></select></div>
  <div class=r><label>Connection<small id=res>&nbsp;</small></label><button id=test>Test Connection</button></div>
</div>
<h2>Notifications</h2>
<div class=group>
  <div class=r><label for=briefing_hour>Morning briefing<small>What's overdue and due today, once a day</small></label><select id=briefing_hour></select></div>
</div>
<h2>Routines</h2>
<div class=group>
  <div class=r style="display:block"><label for=routines>One per line: <b>days hour pet: what to do</b>. The pet answers in a speech bubble.
  <small>Days: daily, weekdays, or mon,wed,fri. Pet: task, scout or library. Right-click a pet to run one now.</small></label>
  <textarea id=routines rows=4 spellcheck=false placeholder="fri 17 task: summarize what I finished this week&#10;mon 9 scout: the 3 must-reads from last week&#10;weekdays 18 library: which projects haven't I opened in a while?"></textarea></div>
</div>
<h2>Library &amp; Scout</h2>
<div class=group>
  <div class=r><label for=library_editor>Editor app<small>Opens Library files and folders</small></label><input id=library_editor spellcheck=false placeholder="Visual Studio Code"></div>
  <div class=r><label for=github_token>GitHub token<small id=ghs>Optional, raises Scout's GitHub rate limit</small></label><input id=github_token type=password autocomplete=off></div>
</div>
<div class=foot><span>Changes save automatically. Scout's interests are set in the Scout window.</span><span id=msg></span></div>
<script>
const $=id=>document.getElementById(id), post=(u,b)=>fetch(u,{method:'POST',body:JSON.stringify(b)});
const fill=(sel,opts)=>{for(const [v,l] of opts)sel.add(new Option(l,v))};
fill($('retry_minutes'),[[0,'Never (use Retry now)'],[5,'Every 5 min'],[10,'Every 10 min'],[30,'Every 30 min'],[60,'Every hour']]);
fill($('briefing_hour'),[[24,'Off'],...[...Array(24).keys()].map(h=>[h,'Daily after '+h+':00'])]);
async function load(){
  const c=await (await fetch('/api/settings')).json();
  for(const k in c){const e=$(k);if(e&&e.type!=='password'&&document.activeElement!==e)e.value=c[k]}
  $('github_token').placeholder=c.github_token_set?'Saved (type to replace)':'';
  $('ghs').innerHTML=c.github_token_set?'Saved. <a href=# id=rm>Remove</a>':"Optional, raises Scout's GitHub rate limit";
  if($('rm'))$('rm').onclick=e=>{e.preventDefault();save({clear_github_token:true})};
}
let pending=Promise.resolve();
async function save(b){  // macOS style: every change is saved as soon as it's made
  const p=post('/api/settings',b);pending=p;const r=await p, m=$('msg');
  m.className=r.ok?'ok':'bad';m.textContent=r.ok?'Saved':'Could not save';setTimeout(()=>m.textContent='',1500);
  if(b.github_token)$('github_token').value='';
  load();
}
for(const e of document.querySelectorAll('input,select,textarea'))e.onchange=()=>save({[e.id]:e.value});
$('test').onclick=async()=>{
  const res=$('res');res.className='';res.textContent='Checking…';
  await pending;  // a just-edited username saves (on blur) before the check
  await post('/api/argo/check',{});
  const st=await (await fetch('/api/status')).json();
  res.className=st.argo?'ok':'bad';
  res.textContent=!st.configured?'No username set':st.argo?'Argo is reachable':'Unreachable: Argo is down, or you are off VPN';
};
window.onfocus=load;load();
</script>"""


LEVI_PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Coding Sessions</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#e2e2e7;--hover:#f0f0f4;
  --accent:#0a84ff;--green:#1f9d55;--purple:#8e44ef;--red:#d70015}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--card:#2a2a2d;--text:#f2f2f7;--muted:#98989f;--line:#3a3a3d;
  --hover:#323236;--green:#34c759;--purple:#bf8cff;--red:#ff6961}}
*{box-sizing:border-box}
body{font:13.5px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;background:var(--bg);color:var(--text)}
button,input{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:blur(12px);
  padding:14px 20px 10px;border-bottom:1px solid var(--line)}
.top{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}h1{font-size:20px;margin:0}#stats{color:var(--muted)}
.bar{display:flex;gap:8px;align-items:center}
.bar input{flex:1;min-width:160px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
.bar input:focus{border-color:var(--accent);box-shadow:0 0 0 3px #0a84ff33}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:var(--card);padding:6px 12px;cursor:pointer}.seg .on{background:var(--accent);color:#fff}
main{padding:4px 20px 40px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:20px 0 4px;font-weight:600}
h3{font-size:13px;margin:12px 0 2px;font-weight:600;display:flex;gap:8px;align-items:baseline}
h3 span{font-weight:400;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl}
a.c{display:flex;gap:10px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;
  margin:5px 0;color:inherit;text-decoration:none}
a.c[href]:hover{background:var(--hover)}a.c:not([href]){cursor:default}
.dot{flex:none;width:9px;height:9px;border-radius:50%;background:var(--line)}
.working .dot{background:var(--green);animation:pulse 1.4s infinite}.waiting .dot{background:var(--purple)}
@keyframes pulse{50%{opacity:.35}}
.t{flex:1;min-width:0;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool{flex:none;font-size:11.5px;color:var(--muted);border:1px solid var(--line);border-radius:10px;padding:0 7px}
.st{flex:none;width:96px;text-align:right;color:var(--muted);font-size:12px}.working .st{color:var(--green)}.waiting .st{color:var(--purple)}
.cache{flex:none;width:110px;text-align:right;font-size:12px;font-variant-numeric:tabular-nums;color:var(--muted)}
.cache.warm{color:var(--green)}.cache.low{color:var(--red);font-weight:600}
details summary{cursor:pointer;color:var(--muted);font-size:12.5px;margin:12px 0 4px;user-select:none}
.earlier a.c{opacity:.75}.earlier .t{font-weight:500}
.empty{text-align:center;color:var(--muted);padding:60px 0;font-size:15px}
</style>
<header><div class=top><h1>Coding Sessions</h1><span id=stats></span></div>
<div class=bar><input id=q placeholder="Search sessions" autocomplete=off>
<div class=seg><button data-f=active class=on>Active</button><button data-f=all>All</button></div></div></header>
<main id=list></main>
<script>
let rows = [], filter = "active", openEarlier = new Set()
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c])
const ago = m => m < 1 ? "just now" : m < 60 ? m + "m" : Math.floor(m / 60) + "h"
// active: doing something, needs you, still cached, or used in the last hour
const active = a => a.state != "idle" || (a.until || 0) * 1000 > Date.now() || a.mins < 60
function cache(a) {
  if (!a.until) return ""
  const left = a.until * 1000 - Date.now()
  if (left <= 0) return `<span class=cache>cache expired</span>`
  const m = Math.floor(left / 60000), s = Math.floor(left / 1000) % 60
  return `<span class="cache ${left <= 300000 ? "low" : "warm"}">cache ${m}:${String(s).padStart(2, "0")}</span>`
}
const card = a => `<a class="c ${a.state}" ${a.open ? `href="${esc(a.open)}" target=_blank` : ""} title="${esc(a.dir)}">
  <i class=dot></i><span class=t>${esc(a.where)}</span><span class=tool>${esc(a.tool.replace(/ @.*/, ""))}</span>
  <span class=st>${a.state == "idle" ? "idle " + ago(a.mins) : a.state}</span>${cache(a) || "<span class=cache></span>"}</a>`
function byFolder(list) {
  const g = {}
  for (const a of list) (g[a.dir || "?"] ??= []).push(a)
  return Object.entries(g).map(([dir, l]) =>
    `<h3>${esc(dir.split("/").filter(Boolean).pop() || dir)}<span><bdi>${esc(dir)}</bdi></span></h3>${l.map(card).join("")}`).join("")
}
function render() {
  const q = document.getElementById("q").value.toLowerCase()
  const shown = rows.filter(a => !q || (a.where + " " + a.dir + " " + a.tool).toLowerCase().includes(q))
  const hosts = {}
  for (const a of shown) (hosts[a.host || ""] ??= []).push(a)
  const names = Object.keys(hosts).sort((x, y) => (x != "") - (y != "") || x.localeCompare(y))
  document.getElementById("list").innerHTML = names.map(h => {
    const now = hosts[h].filter(active), old = hosts[h].filter(a => !active(a))
    const earlier = !old.length ? "" : filter == "all" || q ? byFolder(old) :
      `<details class=earlier data-h="${esc(h)}" ${openEarlier.has(h) ? "open" : ""}><summary>Earlier · ${old.length}</summary>${byFolder(old)}</details>`
    return `<h2>${esc(h || "This Mac")}</h2>${byFolder(now)}${earlier}`
  }).join("") || `<div class=empty>${q ? "No sessions match" : "No sessions in the last 12 hours"}</div>`
  const n = s => rows.filter(a => a.state == s).length
  document.getElementById("stats").textContent = `${n("working")} working · ${n("waiting")} waiting · ${rows.filter(active).length} active · ${rows.length} today`
}
document.getElementById("list").addEventListener("toggle", e => {
  const h = e.target.dataset.h; if (h == null) return
  e.target.open ? openEarlier.add(h) : openEarlier.delete(h)
}, true)
document.getElementById("q").oninput = render
document.querySelectorAll(".seg button").forEach(b => b.onclick = () => {
  filter = b.dataset.f; document.querySelectorAll(".seg button").forEach(x => x.classList.toggle("on", x == b)); render()
})
async function load() { try { rows = await (await fetch("/api/agents")).json(); render() } catch (e) {} }
load(); setInterval(load, 5000)
setInterval(() => document.querySelector(".cache") && render(), 1000)  // live countdowns
</script>"""


LIB_PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Library</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#e2e2e7;--hover:#f0f0f4;
  --accent:#0a84ff;--red:#d70015;--chipL:32%}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--card:#2a2a2d;--text:#f2f2f7;--muted:#98989f;--line:#3a3a3d;
  --hover:#323236;--red:#ff6961;--chipL:76%}}
*{box-sizing:border-box}
body{font:13.5px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;background:var(--bg);color:var(--text)}
button,input,select,textarea{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:blur(12px);
  padding:14px 20px 10px;border-bottom:1px solid var(--line)}
.top{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}h1{font-size:20px;margin:0}#stats{color:var(--muted)}
.bar{display:flex;gap:8px;align-items:center}
.bar input{flex:1;min-width:160px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
.bar input:focus{border-color:var(--accent);box-shadow:0 0 0 3px #0a84ff33}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button,.btn{border:0;background:var(--card);padding:6px 12px;cursor:pointer}.seg .on{background:var(--accent);color:#fff}
.btn{border:1px solid var(--line);border-radius:8px}.btn:hover{background:var(--hover)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn.primary:disabled{opacity:.45;cursor:default}
.wrap{display:flex;align-items:flex-start}
aside{flex:none;width:200px;position:sticky;top:96px;max-height:calc(100vh - 96px);overflow:auto;padding:14px 8px 30px 14px}
aside h2{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:16px 8px 4px;font-weight:600}
h2.fold{cursor:pointer;user-select:none;display:flex;align-items:center;gap:4px}h2.fold:hover{color:var(--text)}
h2.fold::after{content:"›";font-size:13px;transform:rotate(90deg);transition:transform .15s;opacity:0}aside:hover h2.fold::after{opacity:1}
h2.fold.shut::after{transform:none;opacity:1}
.nav{display:flex;align-items:center;gap:6px;width:100%;border:0;background:none;border-radius:7px;padding:5px 8px;cursor:pointer;text-align:left}
.nav:hover,.nav.over{background:var(--line)}.nav.on{background:var(--accent);color:#fff}.nav.on .n{color:#fff}
.nav span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.nav .n{flex:none;color:var(--muted);font-size:12px}
.nav i{flex:none;width:9px;height:9px;border-radius:50%;background:hsl(var(--h) 70% 50%)}.nav.miss{color:var(--red)}
.linkbtn{border:0;background:none;color:var(--muted);cursor:pointer;padding:6px 8px;font-size:12.5px}.linkbtn:hover{color:var(--text);text-decoration:underline}
main{flex:1;min-width:0;padding:6px 20px 40px 6px}
.empty{text-align:center;color:var(--muted);padding:60px 0;font-size:15px;line-height:1.7}
.c{display:flex;gap:12px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:6px 0}
.c:hover{background:var(--hover)}.c.missing{border-left:3px solid var(--red)}
.ico{flex:none;width:34px;height:34px;border-radius:9px;display:grid;place-content:center;cursor:pointer;font-weight:700;font-size:10.5px;
  background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 65% var(--chipL))}
.ico svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linejoin:round}
.body{flex:1;min-width:0}
.title{width:100%;border:0;background:none;font-weight:600;font-size:14px;padding:0;outline:none;text-overflow:ellipsis}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin-top:3px;font-size:12px}
.path a,.why a{color:var(--accent);text-decoration:none}.path a:hover,.why a:hover{text-decoration:underline}
.path{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;max-width:340px;direction:rtl;text-align:left}
.grp{border:0;border-radius:6px;background:var(--bg);color:var(--text);padding:1px 4px;font-size:12px;cursor:pointer;max-width:130px}
.grp.none{color:var(--muted)}
.chips{display:contents}.chip{border-radius:10px;padding:1px 8px;cursor:pointer;background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 65% var(--chipL));font-weight:500}
.chip b{display:inline-block;vertical-align:bottom;max-width:0;overflow:hidden;font-weight:400;opacity:0}.chip:hover b{max-width:1em;margin-left:3px;opacity:.7}.chip b:hover{opacity:1}
.addl{border:0;background:none;width:0;padding:1px 0;color:var(--muted);outline:none;opacity:0}
.c:hover .addl,.addl:focus{opacity:1;width:58px;padding:1px 2px}.t .addl:focus,.c .addl:focus{width:180px;color:var(--text)}
.why{margin-top:4px;font-style:italic;color:var(--muted)}.warn{color:var(--red);margin-top:4px;font-size:12px}
details summary{display:inline-block;font-size:12px;color:var(--muted);cursor:pointer;margin-top:4px;list-style:none;user-select:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"›";display:inline-block;margin-right:5px;transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
textarea,.pathin{width:100%;background:var(--bg);border:1px solid transparent;border-radius:8px;padding:8px 10px;outline:none}
textarea{resize:vertical;margin-top:6px}textarea:focus,.pathin:focus{border-color:var(--accent)}.pathin{margin-top:6px;font-size:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,Menlo,monospace;max-height:260px;overflow:auto;
  background:var(--bg);border-radius:8px;padding:10px 12px;margin:6px 0}
.side{flex:none;display:flex;align-items:center;gap:2px}
.icon{border:0;background:none;width:28px;height:28px;border-radius:6px;color:var(--muted);cursor:pointer;display:grid;place-content:center;opacity:.55}
.icon[hidden]{display:none}.c:hover .icon{opacity:1}.icon:hover{background:var(--line);color:var(--text)}.icon.x:hover{color:var(--red)}.icon:disabled{opacity:.2!important;cursor:default}
.icon svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.argoing{display:none;color:var(--accent)}.c.thinking .argoing{display:inline}
.c.thinking{border-color:var(--accent);background:linear-gradient(90deg,var(--card) 30%,color-mix(in srgb,var(--accent) 12%,var(--card)) 50%,var(--card) 70%) 0 0/300% 100%;
  animation:shimmer 1.8s linear infinite}@keyframes shimmer{from{background-position:100% 0}to{background-position:0 0}}
#ans{position:relative;background:var(--card);border:1px solid var(--accent);border-radius:10px;padding:10px 32px 10px 12px;margin:12px 0 0;white-space:pre-wrap}
#ans>button{position:absolute;top:6px;right:6px;border:0;background:none;cursor:pointer;color:var(--muted)}
#toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:10;background:var(--text);color:var(--bg);
  border-radius:8px;padding:8px 14px;font-size:12.5px;opacity:0;transition:opacity .2s;pointer-events:none}#toast.on{opacity:.92}
.pill[hidden]{display:none}.pill+.pill{margin-left:6px}.pill{margin-left:auto;align-self:center;display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card);
  border-radius:12px;padding:2px 10px;font-size:12px;color:var(--muted);cursor:pointer}.pill:hover{border-color:var(--accent)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.pill.ok .dot{background:#34c759}.pill.down{color:var(--red)}.pill.down .dot{background:var(--red)}
.pill.busy{color:var(--accent)}.pill.busy .dot{background:none;border:2px solid var(--accent);border-right-color:transparent;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#drop{display:none;position:fixed;inset:8px;z-index:9;pointer-events:none;align-items:center;justify-content:center;
  border:3px dashed var(--accent);border-radius:14px;background:#0a84ff1a;color:var(--accent);font-size:20px;font-weight:600}
#drop.over{display:flex}
dialog.sheet{width:min(720px,calc(100vw - 32px));max-height:calc(100vh - 48px);border:1px solid var(--line);border-radius:14px;
  padding:0;background:var(--card);color:var(--text);box-shadow:0 24px 70px #0005}
dialog.sheet::backdrop{background:#0006;backdrop-filter:blur(2px)}
dialog.sheet form{display:flex;flex-direction:column;max-height:calc(100vh - 50px)}
dialog#adddlg{width:min(580px,calc(100vw - 32px))}
.ph{padding:18px 22px 4px}.ph h3{margin:0;font-size:17px}.ph p{margin:3px 0 0;color:var(--muted);font-size:12.5px}
.pb{padding:4px 22px 18px;overflow:auto}
.sec{margin-top:14px}.sec b{display:block;font-size:13px;margin-bottom:6px}
.sheet textarea{background:var(--bg);border:1px solid var(--line);font-size:12.5px;font-family:ui-monospace,Menlo,monospace}
.sheet textarea:focus{box-shadow:0 0 0 3px #0a84ff26}
.opt{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:12.5px;color:var(--muted)}.opt label{display:flex;gap:6px;align-items:center}
.opt select{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:3px 6px}
.alog{font-size:12.5px;margin-top:8px;overflow-wrap:anywhere}.alog.bad{color:var(--red)}
.zone{display:flex;align-items:center;gap:14px;margin-top:14px;padding:14px 16px;border:1.5px dashed var(--line);border-radius:12px;background:var(--bg)}
.zone[hidden]{display:none}.zone small{display:block;color:var(--muted);font-size:12px;margin-top:6px}
.zi{flex:none;width:40px;height:40px;border-radius:10px;display:grid;place-content:center;background:#0a84ff1f;color:var(--accent)}
.zi svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linejoin:round}
.lbl{display:flex;align-items:baseline;gap:8px;margin:16px 0 0}.lbl b{font-size:13px}.lbl small{color:var(--muted);font-size:12px}
#adddlg textarea{margin-top:6px;line-height:1.5}
.opt select{appearance:none;-webkit-appearance:none;padding:3px 24px 3px 8px;cursor:pointer;
  background:var(--bg) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%238e8e93' stroke-width='1.5'/%3E%3C/svg%3E") no-repeat right 8px center}
.two{display:grid;grid-template-columns:1fr 1fr;gap:22px}@media (max-width:600px){.two{grid-template-columns:1fr}}
.row{display:flex;align-items:center;gap:6px;margin:3px 0}
.row input[type=text]{flex:1;min-width:0;background:var(--bg);border:1px solid transparent;border-radius:7px;padding:5px 8px;outline:none}
.row input[type=text]:focus{border-color:var(--accent)}.row input.new{border:1px dashed var(--line)}
.row small{color:var(--muted);font-size:11.5px;min-width:22px;text-align:right}
.row button{border:0;background:none;width:22px;height:22px;border-radius:11px;cursor:pointer;color:var(--muted)}.row button:hover{background:var(--line);color:var(--red)}
.hue{appearance:none;width:64px;height:10px;border-radius:5px;background:linear-gradient(90deg,hsl(0 70% 55%),hsl(60 70% 55%),hsl(120 70% 55%),hsl(180 70% 55%),hsl(240 70% 55%),hsl(300 70% 55%),hsl(359 70% 55%))}
.hue::-webkit-slider-thumb{appearance:none;width:14px;height:14px;border-radius:50%;background:hsl(var(--h) 70% 50%);border:2px solid #fff;box-shadow:0 0 0 1px #0004}
.pf{display:flex;gap:8px;align-items:center;padding:12px 22px;border-top:1px solid var(--line)}
.pf .msg{flex:1;font-size:12px;color:var(--muted)}
.mini{padding:8px 12px}.mini .ico{font-size:15px;font-weight:400}
.mini .title{display:block;color:inherit;text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
a.title:hover{color:var(--accent);text-decoration:underline}.mini.done .title{text-decoration:line-through;color:var(--muted)}
.kind{color:var(--muted);text-transform:capitalize;font-weight:600}.sub{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;max-width:420px}
main h3{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:18px 2px 4px;font-weight:600}
#ans .steps{color:var(--muted);font-size:12px;margin-top:6px}#ans .refs{white-space:normal;margin-top:6px}
.rel{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin-top:5px;font-size:12px}.rel:empty{display:none}
.rl{color:var(--muted);font-weight:600;cursor:pointer}.rl:hover{color:var(--accent)}
.rchip{display:inline-flex;align-items:center;gap:5px;max-width:260px;border:1px solid var(--line);border-radius:10px;padding:0 8px;cursor:pointer;background:var(--bg)}
.rchip:hover{border-color:var(--accent)}.rchip i{flex:none;width:7px;height:7px;border-radius:50%;background:hsl(var(--h) 70% 50%)}
.rchip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rchip.argo{border-style:dashed}
.rchip b{display:none;font-weight:400;opacity:.6}.rchip:hover b{display:inline}.rchip b:hover{opacity:1;color:var(--red)}
.rel .addl{width:0}.c:hover .rel .addl{width:70px}.rel .find{display:none;padding:0 4px;font-size:12px}.c:hover .rel .find{display:inline}
#focusbar{display:flex;align-items:center;gap:8px;margin:10px 0 4px;padding:8px 12px;border-radius:10px;background:color-mix(in srgb,var(--accent) 10%,var(--card));font-size:12.5px}
#focusbar[hidden]{display:none}#focusbar span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#mapbox{position:relative;margin-top:8px}#mapbox[hidden]{display:none}
#mapc{display:block;width:100%;height:calc(100vh - 172px);min-height:320px;background:var(--card);border:1px solid var(--line);border-radius:12px;cursor:grab;touch-action:none}
#focusbar:not([hidden])~#mapbox #mapc{height:calc(100vh - 226px)}#mapc.drag{cursor:grabbing}#mapc.on{cursor:pointer}
#tip{position:absolute;z-index:3;max-width:320px;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:7px 10px;font-size:12px;box-shadow:0 6px 24px #0003}#tip[hidden]{display:none}#tip b{display:block;font-size:12.5px}#tip small{color:var(--muted);display:block;margin-top:2px}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;color:var(--muted);font-size:11.5px;margin:6px 4px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px;vertical-align:-1px;background:var(--muted)}
.legend .ln{width:16px;height:0;border-radius:0;border-top:2px solid var(--accent);background:none;vertical-align:3px}
.legend .ll{width:16px;height:0;border-radius:0;border-top:1px solid var(--muted);background:none;vertical-align:3px}
.seg[hidden]{display:none}
</style>
<div id=drop>Drop files, folders or links to add them</div><div id=toast></div>
<header>
  <div class=top><h1>Library</h1><span id=stats></span><button id=argo class=pill></button>
    <button class=btn id=addbtn title="Add folders, files or docs">+ Add</button></div>
  <div class=bar>
    <input id=q type=search placeholder="Search, or ask Argo in plain words and press Enter: “slides from my last beamtime talk”  ( / )">
    <span class=seg id=sorts></span><span class=seg id=views></span>
  </div>
</header>
<datalist id=labels></datalist><datalist id=refs></datalist>
<div class=wrap><aside id=nav></aside><main><div id=ans hidden></div><div id=focusbar hidden></div><div id=list></div>
  <div id=mapbox hidden><canvas id=mapc></canvas><div id=tip hidden></div>
    <div class=legend><span><i style="width:13px;height:13px;vertical-align:-3px"></i>Label</span><span><i></i>Item (coloured by kind)</span>
      <span><i class=ll></i>has the label</span><span><i class=ln></i>Related</span>
      <span>Click a label to filter, an item to see what it connects to; double-click opens it. Scroll to zoom, drag to move.</span></div></div>
</main></div>
<dialog id=adddlg class=sheet><form id=aform>
  <div class=ph><h3>Add to Library</h3><p>Folders, files, docs and links. Each gets its type automatically; Argo summarizes it and suggests labels.</p></div>
  <div class=pb>
    <div class=zone id=a_zone><span class=zi></span>
      <div><button type=button class="btn primary" id=a_browse>Choose in Finder…</button>
        <small>or drop files, folders or links anywhere in this window. ⌘-click picks several.</small></div></div>
    <div class=lbl><b>Or paste paths and links</b><small>one per line · ⌥⌘C in Finder copies a path</small></div>
    <textarea id=a_paths rows=3 placeholder="~/Research/Projects/ptychography&#10;https://arxiv.org/abs/…  ·  YouTube  ·  GitHub  ·  any page or PDF"></textarea>
    <div class=opt><label><input type=checkbox id=a_split> Add each subfolder as its own item</label>
      <label>Type <select id=a_grp></select></label></div><div id=a_log></div></div>
  <div class=pf><span class=msg></span><button type=button class=btn id=a_close>Close</button><button class="btn primary" id=a_save>Add</button></div>
</form></dialog>
<dialog id=mdlg class=sheet><form id=mform>
  <div class=ph><h3>Types &amp; labels</h3><p><b>Type</b> is what an item is: set automatically, change it any time. <b>Labels</b> are what it's about
    (projects, topics), shared with Task Manager and Scout. Renaming onto an existing name merges the two.</p></div>
  <div class="pb two"><div class=sec><b>Types</b><div id=m_groups></div><div class=row><input id=m_ng class=new type=text placeholder="+ New type"></div></div>
    <div class=sec><b>Labels</b><div id=m_labels></div><div class=row><input id=m_nl class=new type=text placeholder="+ New label"></div></div></div>
  <div class=pf><span class=msg>Tip: drag an item onto a type or label in the sidebar.</span><button class="btn primary">Done</button></div>
</form></dialog>
<script>
const SVG=p=>`<svg viewBox="0 0 24 24">${p}</svg>`;
const I={folder:SVG('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  open:SVG('<path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>'),
  reveal:SVG('<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>'),
  edit:SVG('<path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"/>'),
  term:SVG('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M12.5 15H17"/>'),
  copy:SVG('<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>'),
  x:SVG('<path d="M6 6l12 12M18 6 6 18"/>')};
const SORTS={recent:'Recent',name:'Name',added:'Added'};
let lib={groups:[],labels:{},items:[]}, st={busy:[]}, rev=0, trev=0, dragId=null, tasks=[], others=[];  // tasks & emails; others: the labeled ones
let byRef=new Map(), back=new Map();  // "lib:<id>"/"task:<id>" -> item; ref -> links other items made to it
let sel=localStorage.libsel||'all', ty=localStorage.libty||'', sort=localStorage.libsort||'recent', view=localStorage.libview||'list';
if(sel.startsWith('g:')){ty=sel.slice(2);sel='all'}  // from before groups became types
const hue=s=>[...s].reduce((h,c)=>(h*31+c.charCodeAt(0))%360,7);  // same colours as Task Manager labels
const lhue=l=>lib.labels[l]??hue(l);
const el=(tag,cls,txt)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e};
function toast(m){const e=document.getElementById('toast');e.textContent=m;e.classList.add('on');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('on'),4000)}
async function api(p,b){
  const r=await fetch(p,{method:'POST',body:JSON.stringify(b||{})}).catch(()=>({}));
  if(!r.ok)toast((await r.json?.().catch(()=>({})))?.error||'Something went wrong');
  await load();return r;
}
const meta=b=>api('/api/library/meta',b);
const allLabels=()=>[...new Set([...Object.keys(lib.labels),...lib.items.flatMap(x=>x.labels),...others.flatMap(x=>x.labels)])].sort((a,b)=>a.localeCompare(b));
const KIND={arXiv:'paper',YouTube:'video',GitHub:'repo',Link:'link'};
const refOf=x=>x.ref||'lib:'+x.id;
const extOf=x=>x.folder||x.src?'':(x.name.match(/\.(\w{1,5})$/)||['',''])[1].toUpperCase(),kindOf=x=>x.src&&(KIND[x.src]||'link');
const hueOf=x=>x.ref?KHUE[x.kind]??212:x.folder?212:kindOf(x)?KHUE[kindOf(x)]:hue(extOf(x)||'doc');
const titleOf=x=>x.ref?x.title:x.name;
function links(x){  // what x is related to, both the links it holds and the ones others hold to it; gone items dropped
  const out=new Map();for(const r of x.related||[])out.set(r.ref,r);
  for(const r of back.get(refOf(x))||[])if(!out.has(r.ref))out.set(r.ref,r);
  return [...out.values()].filter(r=>byRef.has(r.ref));
}
function index(){
  byRef=new Map([...lib.items.map(x=>['lib:'+x.id,x]),...tasks.map(x=>[x.ref,x])]);back=new Map();
  for(const y of lib.items)for(const r of y.related||[]){if(!back.has(r.ref))back.set(r.ref,[]);back.get(r.ref).push({...r,ref:'lib:'+y.id,back:true})}
}
function openRef(ref){const x=byRef.get(ref);if(!x)return;  // double-click on the map
  if(!x.ref)return api(`/api/library/item/${x.id}/open`);if(x.eml)return api(`/api/todo/${x.id}/open`);toast(x.title+' (a task: open it in Task Manager)')}
// Survey Corps kit: sealed dispatch, supply wagon, scroll, crest shield, crossed blades, scouting compass, the Wall
const WALL=SVG('<path d="M3 20V8h3V5h3v3h2V5h2v3h2V5h3v3h3v12zM10 20v-3.5a2 2 0 0 1 4 0V20"/>');
const KICON={email:SVG('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/><circle cx="12" cy="14" r="2.2" fill="currentColor"/>'),
  task:SVG('<path d="M4.5 12.5 3 6c3-1.5 6-2 9-2s6 .5 9 2l-1.5 6.5M8.6 4.5l.4 8M15.4 4.5l-.4 8M3 12.5h18v2.6H3zM21 13.8h2.5M7 16.2v4.8M4.6 18.6h4.8M17 16.2v4.8M14.6 18.6h4.8"/><circle cx="7" cy="18.6" r="2.4"/><circle cx="17" cy="18.6" r="2.4"/>'),
  paper:SVG('<path d="M17 7h2a2 2 0 0 0 0-4H8a2 2 0 0 0-2 2v12M17 5v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2h11v2M9.5 9h4.5M9.5 12.5h4.5"/>'),
  video:SVG('<path d="M12 3l8 3v6c0 4.5-3.5 8-8 9-4.5-1-8-4.5-8-9V6z"/><path d="M10 8.5v7l5.5-3.5z" fill="currentColor"/>'),
  repo:SVG('<path d="M3 21l3.5-3.5M21 21l-3.5-3.5M5 15.5l3.5 3.5M19 15.5 15.5 19"/><path d="M7.5 16.5 20 4M16.5 16.5 4 4" stroke-width="3" stroke-linecap="butt"/>'),
  link:SVG('<circle cx="12" cy="12" r="9"/><path d="M12 5.5l2.2 6.5-2.2 6.5-2.2-6.5z"/><path d="M12 5.5l2.2 6.5H9.8z" fill="currentColor"/>'),
  path:WALL},KHUE={email:212,task:140,paper:28,video:0,repo:270,link:180,path:212};
function mini(x){  // a task, email or found file, in the Library's label view and search answers
  const d=el('div','c mini'+(x.sub.endsWith('· done')?' done':''));
  const ico=el('div','ico');ico.innerHTML=KICON[x.kind]||'•';ico.style.setProperty('--h',KHUE[x.kind]??212);
  const b=el('div','body'),t=el(x.url?'a':'div','title',x.title);t.title=x.title;if(x.url){t.href=x.url;t.target='_blank'}
  const m=el('div','meta');m.append(el('span','kind',x.kind==='path'?'On disk':x.kind));
  const sub=el('span',x.kind==='path'?'path':'sub',x.kind==='path'?'\u200e'+x.sub.replace(/^\/Users\/[^/]+/,'~')+'\u200e':x.sub);sub.title=x.sub;
  const up=labels=>api(`/api/todo/${x.id}/update`,{labels});
  for(const l of x.labels){const c=el('span','chip',l);c.style.setProperty('--h',lhue(l));c.title='Show everything with this label';c.onclick=()=>go('l:'+l);
    const r=el('b',0,'×');r.title='Remove label';r.onclick=e=>{e.stopPropagation();up(x.labels.filter(y=>y!==l))};c.append(r);m.append(c)}
  if(x.kind!=='path'){const al=el('input','addl');al.setAttribute('list','labels');al.placeholder='+ label';al.onchange=()=>al.value.trim()&&up([...x.labels,al.value.trim()]);m.append(al)}
  m.append(sub);b.append(t,m);
  const side=el('div','side'),btn=(ic,title,f)=>{const e=el('button','icon');e.innerHTML=ic;e.title=title;e.onclick=f;side.append(e)};
  if(x.eml)btn(I.open,'Open in Outlook',()=>api(`/api/todo/${x.id}/open`));
  if(x.url)btn(I.open,'Open link',()=>t.click());
  if(x.kind==='path'){btn(I.reveal,'Show in Finder',()=>api('/api/reveal',{path:x.sub}));
    btn(SVG('<path d="M12 5v14M5 12h14"/>'),'Add to Library',()=>fetch('/api/library/add',{method:'POST',body:JSON.stringify({paths:[x.sub]})}).then(r=>r.json()).then(dropped))}
  d.append(ico,b,side);return d;
}
// Two filters that combine: sel (all items / a label / missing) and ty (a type, '' = any)
const OTYPE={email:'Emails',task:'Tasks',paper:'Papers',video:'Videos',repo:'Code',link:'Websites'};  // tasks, and what Argo's search finds, as types
const typeOf=x=>x.ref?OTYPE[x.kind]:x.group||'No type';
const sels=()=>sel.startsWith('l:')?sel.slice(2).split('\t'):[];  // clicking labels toggles them; an item must have all of them
let near=new Set();  // sel "r:<ref>": that item and what it's related to
const inScope=x=>sel==='all'||(sel==='missing'?x.missing:sel.startsWith('r:')?near.has(refOf(x)):sels().every(l=>x.labels.includes(l)));
const matches=x=>inScope(x)&&(!ty||typeOf(x)===ty);
function go(s){sel=s;localStorage.libsel=s;render()}
function goType(t){ty=ty===t?'':t;localStorage.libty=ty;render()}
function navBtn(key,text,n,h){
  const isT=key.startsWith('t:'),b=el('button','nav'+((isT?'t:'+ty===key:key.startsWith('l:')?sels().includes(key.slice(2)):sel===key)?' on':'')+(key==='missing'?' miss':''));
  if(h!=null){const i=el('i');i.style.setProperty('--h',h);b.append(i)}
  b.append(el('span',0,text),el('span','n',n));b.title=isT?(ty===text?'Show every type':'Only '+text):key.startsWith('l:')?text+' (click more labels to combine)':text;
  b.onclick=e=>{if(isT)return goType(key.slice(2));
    if(key.startsWith('l:')){const l=key.slice(2),cur=sels(),nx=cur.includes(l)?cur.filter(y=>y!==l):[...cur,l];return go(nx.length?'l:'+nx.join('\t'):'all')}
    go(key)};
  if(isT&&lib.groups.includes(key.slice(2))||key.startsWith('l:')){  // drop an item here: change its type / add the label
    b.ondragover=e=>{if(dragId){e.preventDefault();b.classList.add('over')}};b.ondragleave=()=>b.classList.remove('over');
    b.ondrop=e=>{e.preventDefault();b.classList.remove('over');const x=lib.items.find(y=>y.id===dragId);if(!x)return;
      api(`/api/library/item/${x.id}/update`,isT?{group:key.slice(2)}:{labels:[...x.labels,key.slice(2)]})};
  }
  return b;
}
let fold=(()=>{try{return JSON.parse(localStorage.libfold||'{}')}catch{return {}}})();
function section(out,name,btns){  // a collapsible sidebar section; a folded one still shows what's selected
  const h=el('h2','fold'+(fold[name]?' shut':''),name);h.title=fold[name]?'Show':'Hide';
  h.onclick=()=>{fold[name]=!fold[name];localStorage.libfold=JSON.stringify(fold);renderNav()};
  out.push(h,...btns.filter(b=>!fold[name]||b.classList.contains('on')));
}
function renderNav(){
  const it=lib.items,out=[navBtn('all','All items',it.length)];
  const scope=[...it,...sel.startsWith('l:')?others:sel.startsWith('r:')?tasks:[]].filter(inScope),tn=t=>scope.filter(x=>typeOf(x)===t).length;
  section(out,'Types',[...lib.groups,'Emails','Tasks','No type'].filter(t=>tn(t)||t===ty).map(t=>navBtn('t:'+t,t,tn(t)||'')));
  const ls=allLabels();
  const n=l=>[...it,...others].filter(x=>x.labels.includes(l)).length||'';  // a label view shows tasks too
  if(ls.length)section(out,'Labels',ls.map(l=>navBtn('l:'+l,l,n(l),lhue(l))));
  const miss=it.filter(x=>x.missing).length;if(miss)section(out,'Needs attention',[navBtn('missing','Missing on disk',miss)]);
  const e=el('button','linkbtn','Edit types & labels…');e.onclick=openManage;out.push(el('div'),e);
  nav.replaceChildren(...out);
}
function card(x){
  const d=el('div','c'+(x.missing?' missing':'')+(st.busy.includes(x.id)?' thinking':''));d.dataset.id=x.id;
  d.innerHTML=`<div class=ico></div><div class=body><input class=title spellcheck=false>
    <div class=meta><span class=argoing>✨ Argo is reading this…</span><select class=grp title="Type (set automatically; change it any time)"></select><span class=chips></span>
      <input class=addl list=labels placeholder="+ label"><span class=path></span></div>
    <div class=why></div><div class=rel></div><details><summary>Notes &amp; contents</summary><textarea rows=2 placeholder="Notes…"></textarea>
      <input class=pathin spellcheck=false title="Path on disk (fix it here if you moved the item)"><pre></pre></details></div>
    <div class=side><button class=icon data-a=open title="Open">${I.open}</button><button class=icon data-a=reveal title="Show in Finder">${I.reveal}</button>
      <button class=icon data-a=edit title="Open in ${lib.editor}">${I.edit}</button><button class=icon data-a=terminal title="Open in Terminal">${I.term}</button>
      <button class=icon data-a=copy title="Copy path">${I.copy}</button><button class="icon x" title="Remove from Library (the file stays on disk)">${I.x}</button></div>`;
  const $=s=>d.querySelector(s),up=p=>api(`/api/library/item/${x.id}/update`,p);
  const ext=extOf(x),k=kindOf(x);
  const ico=$('.ico');ico.innerHTML=x.folder?WALL:k?KICON[k]:'';if(!x.folder&&!k)ico.textContent=ext||'DOC';
  ico.style.setProperty('--h',hueOf(x));ico.title=x.folder?'Open folder':k?'Open link':'Open';
  if(k)d.classList.add('mini');
  ico.onclick=()=>act('open');
  const ti=$('.title');ti.value=x.name;ti.title=x.name;ti.onchange=()=>up({name:ti.value});ti.onkeydown=e=>{if(e.key==='Enter')ti.blur()};
  const g=$('.grp');g.replaceChildren(new Option('No type',''),...lib.groups.map(n=>new Option(n)));g.value=x.group;
  g.classList.toggle('none',!x.group);g.onchange=()=>up({group:g.value});
  for(const l of x.labels){
    const c=el('span','chip',l);c.style.setProperty('--h',lhue(l));c.title='Show only this label';c.onclick=()=>go('l:'+l);
    const b=el('b',0,'×');b.title='Remove label';b.onclick=e=>{e.stopPropagation();up({labels:x.labels.filter(y=>y!==l)})};
    c.append(b);$('.chips').append(c);
  }
  const al=$('.addl');al.onchange=()=>{const v=al.value.trim();if(v)up({labels:[...x.labels,v]})};
  const p=$('.path');p.textContent='\u200e'+x.path.replace(/^\/Users\/[^/]+/,'~')+'\u200e';  // LRM: keeps ~/ in front under rtl (which ellipsizes the start)
  p.title=x.path;
  if(k&&/^https?:\/\//.test(x.path)){const a=el('a',0,x.path);a.href=x.path;a.target='_blank';a.onclick=e=>e.stopPropagation();p.replaceChildren(a)}
  const why=$('.why');if(x.missing){why.className='warn';why.textContent='⚠︎ Not found at this path. Moved or renamed? Fix the path under Notes & contents.'}
  else if(x.summary){let i=0,m;for(const re=/https?:\/\/[^\s<>"]*[^\s<>".,;)]/g;(m=re.exec(x.summary));i=re.lastIndex){
    why.append(x.summary.slice(i,m.index));const a=el('a',0,m[0]);a.href=m[0];a.target='_blank';why.append(a)}
    why.append(x.summary.slice(i))}else why.remove();
  related($('.rel'),x);
  const n=$('textarea');n.value=x.note;n.onchange=()=>up({note:n.value});
  const pi=$('.pathin');pi.value=x.path;pi.onchange=()=>pi.value.trim()&&up({path:pi.value});
  if(x.peek)$('pre').textContent=x.peek;else $('pre').remove();
  $('summary').textContent='Notes & contents'+(x.note?' ✎':'');
  function act(a){return api(`/api/library/item/${x.id}/${a}`).then(r=>r.ok&&a==='copy'&&toast(k?'Link copied':'Path copied'))}
  for(const b of d.querySelectorAll('[data-a]')){b.onclick=()=>act(b.dataset.a);b.disabled=x.missing&&b.dataset.a!=='copy'}
  if(k){for(const a of ['reveal','edit','terminal'])$(`[data-a=${a}]`).hidden=true;$('[data-a=open]').title='Open in browser';
    $('[data-a=copy]').title='Copy link';pi.title='Link'}
  else if(!x.folder&&!/^(txt|md|py|ipynb|json|tex|bib|ya?ml|toml|js|ts|swift|sh|csv|c|h|cpp|m|r|jl|html|css|xml)$/i.test(ext))$('[data-a=edit]').hidden=true;
  $('.x').onclick=()=>confirm(`Remove "${x.name}" from the Library? The ${x.folder?'folder':'file'} itself stays on disk.`)&&api(`/api/library/item/${x.id}/delete`);
  d.draggable=true;d.ondragstart=e=>{if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName))return e.preventDefault();dragId=x.id;e.dataTransfer.setData('application/x-lib',x.id)};
  d.ondragend=()=>dragId=null;
  return d;
}
function related(box,x){  // a card's "Related" row: click one to see what it connects to
  const ls=links(x),me=refOf(x);
  const h=el('span','rl','Related');h.title='Show this and everything it connects to';h.onclick=()=>go('r:'+me);
  if(ls.length)box.append(h);
  for(const r of ls.slice(0,8)){const y=byRef.get(r.ref),c=el('span','rchip'+(r.by==='argo'?' argo':''));
    const i=el('i');i.style.setProperty('--h',hueOf(y));c.append(i,el('span',0,titleOf(y)));
    c.title=(r.why?r.why+'\n':'')+(r.by==='argo'?'Found by Argo':'Linked by you')+' · click to see what it connects to';c.onclick=()=>go('r:'+r.ref);
    const b=el('b',0,'×');b.title='Not related';b.onclick=e=>{e.stopPropagation();api(`/api/library/item/${x.id}/unlink`,{ref:r.ref})};c.append(b);box.append(c)}
  if(ls.length>8)box.append(el('span','rl',`+${ls.length-8} more`));
  const al=el('input','addl');al.setAttribute('list','refs');al.placeholder='+ link';al.title='Link it to another item, task or email (type its name)';
  al.onchange=()=>{const v=al.value.trim(),y=[...byRef.values()].find(y=>titleOf(y)===v&&refOf(y)!==me);
    if(y)api(`/api/library/item/${x.id}/link`,{ref:refOf(y)});else if(v)toast('No item, task or email has that name')};
  box.append(al);
  if(st.configured){const f=el('button','linkbtn find','✨ Find links');f.title='Ask Argo again what this connects to (keeps the links you made)';
    f.onclick=()=>api(`/api/library/item/${x.id}/relate`).then(()=>toast('Argo is looking for what this connects to…'));box.append(f)}
}
function render(){
  if(sels().some(l=>!allLabels().includes(l)))sel='all';  // the label may have been renamed or deleted
  if(sel.startsWith('r:')&&!byRef.has(sel.slice(2)))sel='all';  // its item was removed
  const center=sel.startsWith('r:')&&byRef.get(sel.slice(2));
  near=new Set(center?[sel.slice(2),...links(center).map(r=>r.ref)]:[]);
  focusbar.hidden=!center;
  if(center){const b=el('button','linkbtn','Show all');b.onclick=()=>go('all');
    focusbar.replaceChildren(el('span',0,`“${titleOf(center)}” and the ${near.size-1} thing${near.size===2?'':'s'} it connects to`),b)}
  if(ty&&!lib.groups.includes(ty)&&!['Emails','Tasks','No type'].includes(ty))ty='';
  renderNav();
  labels.replaceChildren(...allLabels().map(l=>new Option(l)));
  sorts.replaceChildren(...Object.entries(SORTS).map(([k,v])=>{const b=el('button',k===sort?'on':'',v);b.onclick=()=>{sort=k;localStorage.libsort=k;render()};return b}));
  views.replaceChildren(...[['list','List'],['map','Map']].map(([k,v])=>{const b=el('button',k===view?'on':'',v);
    b.title=k==='map'?'How your items, tasks and emails connect, through labels and Related links':'';b.onclick=()=>{view=k;localStorage.libview=k;render()};return b}));
  refs.replaceChildren(...[...byRef.values()].map(y=>new Option(titleOf(y))));
  sorts.hidden=view==='map';
  const qv=q.value.trim(),words=qv.endsWith('?')||(!ans.hidden&&wordy(qv))?[]:qv.toLowerCase().split(/\s+/).filter(Boolean);
  const key={recent:x=>-Date.parse((x.opened>x.added?x.opened:x.added)||0),name:x=>x.name.toLowerCase(),added:x=>-Date.parse(x.added)}[sort];
  const shown=lib.items.filter(x=>matches(x)&&words.every(w=>[x.name,x.path,x.group,x.note,x.summary,x.peek,...x.labels].join(' ').toLowerCase().includes(w)))
    .sort((a,b)=>{const p=key(a),r=key(b);return p<r?-1:p>r?1:0});
  const lab=sels().length||center,tw=x=>words.every(w=>[x.title,x.sub,...x.labels].join(' ').toLowerCase().includes(w));
  if(center)shown.sort((a,b)=>(b===center)-(a===center));
  const more=lab?tasks.filter(x=>matches(x)&&tw(x)).sort((a,b)=>(b===center)-(a===center)):[];
  list.hidden=view==='map';mapbox.hidden=view!=='map';
  if(view==='map')drawMap([...shown,...lab?more:tasks.filter(x=>matches(x)&&tw(x)&&(x.labels.length||back.has(x.ref)))]);else stopMap();
  const files=shown;
  list.replaceChildren(...(more.length&&files.length?[el('h3',0,'In the Library')]:[]),...files.slice(0,400).map(card),
    ...(more.length?[el('h3',0,'Tasks & emails'),...more.map(mini)]:[]));
  if(!shown.length&&!more.length){const e=el('div','empty');
    e.innerHTML=lib.items.length?(st.configured?'Nothing matches. Press <b>Enter</b> to have Argo look for it.':'Nothing matches.'):'Your Library is empty.<br>Click <b>+ Add</b> to choose folders, files or docs, or drag them in from Finder.';list.append(e)}
  stats.textContent=`${lib.items.length} item${lib.items.length===1?'':'s'}`+(shown.length+more.length!==lib.items.length?` · ${shown.length+more.length} shown`:'');
  if(mdlg.open&&!mdlg.contains(document.activeElement))drawManage();
}
async function load(){[lib,st,tasks]=await Promise.all(['/api/library','/api/status','/api/labeled'].map(u=>fetch(u).then(r=>r.json())));
  others=tasks.filter(x=>x.labels.length);index();rev=st.lib_rev;trev=st.rev;render();pill()}
function pill(){  // Argo status, as in Task Manager
  const work=st.busy.filter(x=>x!=='check'),waiting=lib.items.filter(x=>x.retry).length;
  argo.className='pill'+(!st.configured?'':work.length||st.busy.includes('check')?' busy':st.argo?' ok':st.argo===false?' down':'');
  argo.replaceChildren(el('span','dot'),!st.configured?'Argo off':work.length?'Argo working…':st.argo===false?'Argo unavailable'+(waiting?` · ${waiting} waiting`:''):'Argo');
  argo.title=!st.configured?'LLM features are off. Set "argo_user" in data/config.json to turn them on.'
    :st.argo===false?'Argo is down, or you are off VPN. New items get summarized when it is back.':'Argo is reachable. Click to check now.';
}
async function tick(){
  try{st=await (await fetch('/api/status')).json()}catch{return}
  pill();
  for(const e of document.querySelectorAll('.c[data-id]'))e.classList.toggle('thinking',st.busy.includes(e.dataset.id));
  if((st.lib_rev!==rev||st.rev!==trev)&&document.activeElement===document.body&&!document.querySelector('.c details[open]')&&!dragId)load();
}
argo.onclick=()=>{fetch('/api/argo/check',{method:'POST'}).then(tick);setTimeout(tick,150)};
setInterval(tick,2000);
// ---- map: labels and items as a force-directed graph; lines are "has this label" and Related links ----
// ponytail: plain O(n²) forces, fine for a few hundred nodes; Barnes-Hut if Libraries get into the thousands
const M={nodes:[],edges:[],sig:'',scope:'',alpha:0,k:1,tx:0,ty:0,fit:true,hover:null,drag:null,pan:null,raf:0};
const pos=new Map();  // node id -> {x,y,vx,vy}, kept across redraws so the map doesn't reshuffle
function drawMap(items){
  const nodes=[],edges=[],at=new Map(),cnt={};
  for(const x of items){at.set(refOf(x),nodes.length);nodes.push({id:refOf(x),item:x,r:x.ref?4:5.5,h:hueOf(x)})}
  for(const x of items)for(const l of x.labels)cnt[l]=(cnt[l]||0)+1;
  for(const l in cnt){at.set('L:'+l,nodes.length);nodes.push({id:'L:'+l,label:l,n:cnt[l],r:7+Math.sqrt(cnt[l])*2.4,h:lhue(l)})}
  for(const x of items){const a=at.get(refOf(x));for(const l of x.labels)edges.push({a,b:at.get('L:'+l)});
    for(const r of x.related||[]){const b=at.get(r.ref);if(b!=null&&b!==a)edges.push({a,b,rel:true})}}
  for(const n of nodes)n.nb=new Set([n]);
  for(const e of edges){nodes[e.a].nb.add(nodes[e.b]);nodes[e.b].nb.add(nodes[e.a])}
  for(const n of nodes){  // new nodes start next to something they're tied to
    if(!pos.has(n.id)){const m=[...n.nb].find(m=>pos.has(m.id)),o=m?pos.get(m.id):{x:0,y:0},a=Math.random()*6.3,d=m?20:60+Math.random()*160;
      pos.set(n.id,{x:o.x+Math.cos(a)*d,y:o.y+Math.sin(a)*d,vx:0,vy:0})}
    n.p=pos.get(n.id)}
  const sig=nodes.map(n=>n.id).join()+'|'+edges.length,scope=sel+'\t'+ty+'\t'+q.value;
  if(scope!==M.scope){M.fit=true;M.alpha=Math.max(M.alpha,.5)}
  if(sig!==M.sig)M.alpha=Math.max(M.alpha,M.sig?.4:1);
  Object.assign(M,{nodes,edges,sig,scope});
  M.hover=M.hover&&nodes.find(n=>n.id===M.hover.id);if(!M.hover)tip.hidden=true;
  run();
}
function stopMap(){cancelAnimationFrame(M.raf);M.raf=0;tip.hidden=true}
function run(){if(!M.raf)M.raf=requestAnimationFrame(frame)}
function frame(){
  M.raf=0;
  if(M.alpha>.005){for(let i=0;i<2;i++)step();if(M.fit)fit()}
  paint();
  if(M.alpha>.005||M.drag)run();
}
function step(){
  const N=M.nodes,a=M.alpha;
  for(let i=0;i<N.length;i++){const p=N[i].p,ci=N[i].label?3:1;
    for(let j=i+1;j<N.length;j++){const q=N[j].p,cj=N[j].label?3:1,dx=q.x-p.x,dy=q.y-p.y,d2=Math.max(dx*dx+dy*dy,25);
      if(d2>160000&&ci*cj<9)continue;
      const f=160*ci*cj*a/d2;p.vx-=dx*f;p.vy-=dy*f;q.vx+=dx*f;q.vy+=dy*f}}
  for(const e of M.edges){const p=M.nodes[e.a].p,q=M.nodes[e.b].p,dx=q.x-p.x,dy=q.y-p.y,d=Math.hypot(dx,dy)||1,
    f=(d-(e.rel?45:60))/d*a*(e.rel?.12:.06);p.vx+=dx*f;p.vy+=dy*f;q.vx-=dx*f;q.vy-=dy*f}
  for(const n of N){const p=n.p;if(n===M.drag){p.vx=p.vy=0;continue}
    p.vx-=p.x*.01*a;p.vy-=p.y*.01*a;p.vx*=.55;p.vy*=.55;p.x+=p.vx;p.y+=p.vy}
  M.alpha*=.99;
}
function fit(){  // zoom and centre so everything shows, until you pan or zoom yourself
  if(!M.nodes.length)return;
  const W=mapc.clientWidth,H=mapc.clientHeight;let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  for(const n of M.nodes){x0=Math.min(x0,n.p.x-n.r);x1=Math.max(x1,n.p.x+n.r);y0=Math.min(y0,n.p.y-n.r);y1=Math.max(y1,n.p.y+n.r+16)}
  M.k=Math.min(2.5,(W-80)/Math.max(x1-x0,1),(H-60)/Math.max(y1-y0,1));M.tx=-(x0+x1)/2*M.k;M.ty=-(y0+y1)/2*M.k;
}
function paint(){
  const c=mapc,dpr=devicePixelRatio||1,W=c.clientWidth,H=c.clientHeight;
  if(c.width!==Math.round(W*dpr)||c.height!==Math.round(H*dpr)){c.width=Math.round(W*dpr);c.height=Math.round(H*dpr)}
  const g=c.getContext('2d'),cs=getComputedStyle(document.body),v=k=>cs.getPropertyValue(k).trim(),hot=M.hover?.nb,k=M.k;
  g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,W,H);g.translate(W/2+M.tx,H/2+M.ty);g.scale(k,k);
  for(const e of M.edges){const A=M.nodes[e.a],B=M.nodes[e.b],on=!hot||A===M.hover||B===M.hover;
    g.globalAlpha=on?(e.rel?.9:hot?.7:.28):.05;g.strokeStyle=e.rel?v('--accent'):`hsl(${B.h} 55% 55%)`;g.lineWidth=(e.rel?1.8:1)/k;
    g.beginPath();g.moveTo(A.p.x,A.p.y);g.lineTo(B.p.x,B.p.y);g.stroke()}
  g.textAlign='center';g.textBaseline='top';
  for(const n of M.nodes){g.globalAlpha=!hot||hot.has(n)?1:.15;
    g.beginPath();g.arc(n.p.x,n.p.y,n.r,0,7);
    if(n.label){g.fillStyle=`hsl(${n.h} 70% 50% / .22)`;g.fill();g.strokeStyle=`hsl(${n.h} 70% 50%)`;g.lineWidth=1.5/k;g.stroke()}
    else{g.fillStyle=`hsl(${n.h} 70% 50%)`;g.fill();if(n===M.hover){g.strokeStyle=v('--text');g.lineWidth=2/k;g.stroke()}}
    if(sel==='r:'+n.id){g.beginPath();g.arc(n.p.x,n.p.y,n.r+4/k,0,7);g.strokeStyle=v('--accent');g.lineWidth=2/k;g.stroke()}}
  const few=M.nodes.length<=30;  // small maps (one label, one item's connections): every title
  for(const n of M.nodes){const on=!hot||hot.has(n);if(!n.label&&!(hot&&on)&&!few&&k<1.6)continue;
    g.globalAlpha=on?1:.15;g.fillStyle=n.label?v('--text'):v('--muted');g.font=`${n.label?600:400} ${(n.label?12:10.5)/k}px -apple-system,sans-serif`;
    const t=n.label||titleOf(n.item);g.fillText(t.length>34?t.slice(0,33)+'…':t,n.p.x,n.p.y+n.r+3/k)}
  g.globalAlpha=1;
  if(!M.nodes.length){g.setTransform(dpr,0,0,dpr,0,0);g.fillStyle=v('--muted');g.font='15px -apple-system,sans-serif';g.textAlign='center';
    g.fillText(lib.items.length?'Nothing matches.':'Your Library is empty.',W/2,H/2)}
}
const world=e=>{const b=mapc.getBoundingClientRect();return {x:(e.clientX-b.left-b.width/2-M.tx)/M.k,y:(e.clientY-b.top-b.height/2-M.ty)/M.k}};
function hit(e){const w=world(e);let best=null,bd=1e9;
  for(const n of M.nodes){const d=Math.hypot(n.p.x-w.x,n.p.y-w.y);if(d<n.r+5/M.k&&d<bd){best=n;bd=d}}return best}
function showTip(n,e){
  if(!n){tip.hidden=true;return}
  const x=n.item,b=mapbox.getBoundingClientRect();
  if(n.label)tip.replaceChildren(el('b',0,n.label),el('small',0,`Label · ${n.n} here · click to ${sels().includes(n.label)?'remove the filter':'filter by it'}`));
  else{const ls=links(x);tip.replaceChildren(el('b',0,titleOf(x)),el('small',0,[x.ref?x.kind:typeOf(x),...x.labels].join(' · ')));
    const s=x.ref?x.sub:x.summary;if(s)tip.append(el('small',0,s));
    if(ls.length)tip.append(el('small',0,'Related: '+ls.slice(0,4).map(r=>titleOf(byRef.get(r.ref))+(r.why?` (${r.why})`:'')).join('; ')+(ls.length>4?' …':'')))}
  tip.hidden=false;
  const L=Math.min(e.clientX-b.left+14,b.width-tip.offsetWidth-4),T=e.clientY-b.top+14;
  tip.style.left=Math.max(4,L)+'px';tip.style.top=(T+tip.offsetHeight>b.height?e.clientY-b.top-tip.offsetHeight-10:T)+'px';
}
mapc.onpointerdown=e=>{mapc.setPointerCapture(e.pointerId);const n=hit(e);M.moved=0;
  if(n){M.drag=n;M.fit=false}else{M.pan={x:e.clientX,y:e.clientY};mapc.classList.add('drag')}};
mapc.onpointermove=e=>{
  if(M.drag){M.moved++;if(M.moved<3)return;Object.assign(M.drag.p,world(e));M.alpha=Math.max(M.alpha,.15);tip.hidden=true;run();return}
  if(M.pan){M.tx+=e.clientX-M.pan.x;M.ty+=e.clientY-M.pan.y;M.pan={x:e.clientX,y:e.clientY};M.moved++;M.fit=false;run();return}
  const n=hit(e);if(n!==M.hover){M.hover=n;mapc.classList.toggle('on',!!n);run()}showTip(n,e)};
mapc.onpointerup=()=>{const n=M.drag,click=M.moved<3;M.drag=M.pan=null;mapc.classList.remove('drag');if(!click||!n)return;
  M.hover=null;tip.hidden=true;
  if(n.label){const cur=sels(),nx=cur.includes(n.label)?cur.filter(y=>y!==n.label):[...cur,n.label];go(nx.length?'l:'+nx.join('\t'):'all')}
  else go('r:'+n.id)};
mapc.ondblclick=e=>{const n=hit(e);if(n&&!n.label)openRef(n.id)};
mapc.onpointerleave=()=>{if(M.hover){M.hover=null;run()}tip.hidden=true};
mapc.addEventListener('wheel',e=>{e.preventDefault();const w=world(e),k=Math.max(.15,Math.min(6,M.k*Math.exp(-e.deltaY*.0015)));
  M.tx-=w.x*(k-M.k);M.ty-=w.y*(k-M.k);M.k=k;M.fit=false;run()},{passive:false});
addEventListener('resize',()=>view==='map'&&run());
matchMedia('(prefers-color-scheme:dark)').addEventListener('change',()=>view==='map'&&run());
// ---- groups & labels editor: every change is saved right away ----
function mrow(name,count,ren,del,h){
  const r=el('div','row');
  if(h!=null){const s=el('input','hue');s.type='range';s.min=0;s.max=359;s.value=h;s.style.setProperty('--h',h);s.title='Colour';
    s.oninput=()=>s.style.setProperty('--h',s.value);s.onchange=()=>meta({op:'color_label',name,hue:+s.value});r.append(s)}
  const i=el('input');i.type='text';i.value=name;i.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();i.blur()}};
  i.onchange=()=>{const v=i.value.trim();if(v&&v!==name)ren(v);else i.value=name};
  const x=el('button',0,'×');x.type='button';x.title='Delete';x.onclick=del;
  r.append(i,el('small',0,count||''),x);return r;
}
for(const [i,op] of [[m_ng,'add_group'],[m_nl,'add_label']])
  i.onkeydown=async e=>{if(e.key!=='Enter')return;e.preventDefault();const name=i.value.trim();i.value='';if(name){await meta({op,name});drawManage()}};
function drawManage(){
  const n=f=>[...lib.items,...others].filter(f).length;
  m_groups.replaceChildren(...lib.groups.map(g=>mrow(g,n(x=>x.group===g),to=>meta({op:'rename_group',name:g,to}),
    ()=>confirm(`Delete the type "${g}"? Its items stay, with no type.`)&&meta({op:'delete_group',name:g}))));
  m_labels.replaceChildren(...allLabels().map(l=>mrow(l,n(x=>x.labels.includes(l)),to=>meta({op:'rename_label',name:l,to}),
    ()=>confirm(`Delete the label "${l}" everywhere (Library, tasks and Scout)?`)&&meta({op:'delete_label',name:l}),lhue(l))));
}
function openManage(){drawManage();mdlg.showModal()}
mform.onsubmit=e=>{e.preventDefault();mdlg.close()};
mdlg.addEventListener('focusout',()=>setTimeout(()=>{if(mdlg.open&&!mdlg.contains(document.activeElement))drawManage()}));
// ---- add ----
addbtn.onclick=()=>{a_paths.value='';a_log.replaceChildren();a_save.disabled=false;
  a_grp.replaceChildren(new Option('Automatic',''),...lib.groups.map(g=>new Option(g)));adddlg.showModal();a_paths.focus()};
a_close.onclick=()=>adddlg.close();
const picker=window.webkit?.messageHandlers?.choose;  // only in the Mac app
a_zone.hidden=!picker;if(!picker)document.querySelector('.lbl b').textContent='Paste paths and links';a_zone.querySelector('.zi').innerHTML=I.folder;a_browse.onclick=()=>picker.postMessage(0);
function chosen(paths){  // called by the Mac app with the picked paths
  const have=a_paths.value.split('\n').filter(s=>s.trim());
  a_paths.value=[...new Set([...have,...paths])].join('\n');a_save.focus();
}
function added(j){
  const rows=[...j.added.length?[el('div','alog',`✓ Added ${j.added.length}: ${j.added.slice(0,8).join(', ')}${j.added.length>8?' …':''}`)]:[],
    ...j.skipped.map(s=>el('div','alog bad','⚠︎ '+s))];
  return rows;
}
aform.onsubmit=async e=>{
  e.preventDefault();const paths=a_paths.value.split('\n').filter(s=>s.trim());if(!paths.length)return;
  a_save.disabled=true;a_log.replaceChildren(el('div','alog','Reading…'));
  const r=await fetch('/api/library/add',{method:'POST',body:JSON.stringify({paths,split:a_split.checked,group:a_grp.value})});
  const j=await r.json();a_log.replaceChildren(...added(j));a_save.disabled=false;load();
  if(!j.skipped.length){a_paths.value='';setTimeout(()=>adddlg.close(),1200)}
};
function dropped(j){drop.classList.remove('over');load();  // called by the Mac app after a Finder drop
  toast([j.added.length&&`Added ${j.added.length} to the Library`,j.skipped.length&&j.skipped.join('; ')].filter(Boolean).join(' · ')||'Nothing added')}
addEventListener('dragover',e=>{e.preventDefault();if(!dragId)drop.classList.add('over')});
addEventListener('dragleave',e=>{if(!e.relatedTarget)drop.classList.remove('over')});
addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('over');  // only reached in a browser: it hides file paths
  const u=e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain');
  if(!dragId&&/^https?:\/\//.test(u))fetch('/api/library/add',{method:'POST',body:JSON.stringify({paths:u.split(/\s+/).filter(Boolean)})}).then(r=>r.json()).then(dropped);
  else if(!dragId)toast('Browsers hide file paths: use + Add and paste them (⌥⌘C in Finder), or use the Mac app')});
// ---- search & ask ----
function answer(text,j){const b=el('button',0,'×');b.title='Close';b.onclick=()=>ans.hidden=true;ans.replaceChildren(text,b);ans.hidden=false;
  if(!j)return;
  if(j.steps.length)ans.append(el('div','steps',j.steps.join(' → ')));
  const refs=el('div','refs');refs.append(...j.refs.map(r=>{const x=r.ref.startsWith('lib:')&&lib.items.find(y=>'lib:'+y.id===r.ref);return x?card(x):mini(r)}));
  if(j.refs.length)ans.append(refs);
  render();  // under the answer: the whole Library again, not the plain-text filter's "nothing matches"
}
q.oninput=render;
let asking=null;  // AbortController of the running Argo search
const wordy=v=>v.endsWith('?')||v.split(/\s+/).length>2||/^(what|where|which|who|when|why|how|find|show|list|any|is|are|do|did|can)\b/i.test(v);
q.onkeydown=async e=>{
  if(e.key==='Escape'){asking?.abort();q.value='';ans.hidden=true;render();q.blur()}
  const v=q.value.trim();
  // Enter: a question or request goes to Argo; a keyword or two that the filter already matches stays a plain search
  if(e.key==='Enter'&&v&&(wordy(v)||!list.querySelector('.c'))){
    if(!st.configured)return v.endsWith('?')&&answer('Argo is off. Set your Argo username in Settings to ask questions.');
    asking?.abort();asking=new AbortController();
    answer('✨ Argo is searching your Library, tasks and Spotlight…  (Esc cancels)');
    const r=await fetch('/api/library/ask',{method:'POST',body:JSON.stringify({q:v}),signal:asking.signal}).catch(()=>null);
    if(!r)return;  // cancelled
    if(!r.ok)return answer('Argo is unavailable right now (down, or not on VPN). The list below is a plain text search.');
    const j=await r.json();answer(j.answer||'No answer.',j);
  }
};
addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){e.preventDefault();q.focus()}});
load();
</script>"""

if __name__ == "__main__":
    for d in (MAILS, INBOX):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=scout_loop, daemon=True).start()
    threading.Thread(target=agents_loop, daemon=True).start()
    for target in config().get("remotes", []):
        threading.Thread(target=tunnel, args=(target,), daemon=True).start()
    print(f"Survey Corps on {ORIGIN}  (data in {DATA}, Finder fallback inbox: {INBOX})")
    if "--no-browser" not in sys.argv:  # the Mac app passes this and shows its own window
        subprocess.run(["open", "-a", "Safari", ORIGIN])  # Chromium browsers drop Outlook drags
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
