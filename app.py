#!/usr/bin/env python3
"""Survey Corps: local todo list fed by emails dragged from Outlook. Run: python3 app.py"""
from collections import Counter
import html, json, re, subprocess, sys, threading, time, urllib.parse, urllib.request, uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
DATA = Path(__file__).resolve().parent / "data"
MAILS = DATA / "mails"   # original .eml copies, opened in Outlook via "Open mail"
INBOX = DATA / "inbox"   # fallback: drag emails from Outlook into this Finder folder
DB = DATA / "todos.json"
CONFIG = DATA / "config.json"  # optional: {"argo_user": "<ANL username>", "argo_model": "claudesonnet5"}
ARGO = "https://apps.inside.anl.gov/argoapi/v1/chat/completions"
BRIEFED = DATA / "briefed"  # date of the last morning briefing
lock = threading.Lock()
retry_busy = threading.Lock()
argo_ok, argo_at = None, 0.0  # last Argo call: outcome (None = not tried yet) and when; shown in the UI
busy, busy_lock = Counter(), threading.Lock()  # Argo calls in flight, by tag: task id, "ask", "briefing", "scout", "check"


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


def argo_on():
    return bool(config().get("argo_user"))


def argo(prompt, max_tokens, timeout=120, tag=""):
    """Ask the lab's Argo LLM. Returns the reply text, or None if unconfigured, down or unreachable (e.g. no VPN)."""
    global argo_ok, argo_at
    cfg = config()
    if not cfg.get("argo_user"):
        return None
    req = urllib.request.Request(ARGO, json.dumps({
        "model": cfg.get("argo_model", "claudesonnet5"), "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]}).encode(),
        {"Content-Type": "application/json", "Authorization": "Bearer " + cfg["argo_user"]})
    with busy_lock:
        busy[tag] += 1
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = json.load(r)["choices"][0]["message"]["content"]
    except Exception:  # down, 5xx, timeout, off VPN, malformed reply: all the same to callers
        argo_ok = False
        return None
    finally:
        argo_at = time.time()
        with busy_lock:
            busy[tag] -= 1
    argo_ok = True
    return text


def check_argo():
    argo("Reply with OK.", 5, timeout=20, tag="check")


def suggest(t, known):
    """Labels, due date and action items for a dropped email (or a cleaned title for a typed task).
    None if Argo didn't answer; {} if it answered nonsense."""
    now = datetime.now()
    labels = ("- labels: up to 3 short labels (1-2 words, Title Case). Reuse an existing label whenever one fits; "
              "invent a new one only if none does.\n")
    if t["eml"]:
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
    reply = argo(prompt, 800, tag=t["id"])
    return None if reply is None else clean_suggestion(reply, now)


def enrich(t):
    """Background: fill in labels/due on a freshly dropped email, unless the user already set them."""
    with lock:
        known = sorted({l for x in load() for l in x.get("labels", [])})
    s = suggest(t, known)
    with lock:
        todos = load()
        cur = next((x for x in todos if x["id"] == t["id"]), None)
        if not cur or not argo_on():
            return
        if s is None:  # Argo down: retry_argo() tries again later
            cur["retry"] = "enrich"
            return save(todos)
        cur.pop("retry", None)
        if not s:
            return save(todos)
        if s["labels"] and not cur.get("labels"):
            cur["labels"] = sorted(set(s["labels"]))
        if s["due"] and not cur["due"]:
            cur["due"], cur["notified"] = s["due"], False
        if s["title"] and not t["eml"] and cur["title"] == t["title"]:  # typed task the user hasn't renamed
            cur["title"], cur["body"] = s["title"], "" if cur["body"] == t["title"] else cur["body"]
        if s["actions"] and not cur.get("actions"):
            cur["actions"] = s["actions"]
        save(todos)


def blank():
    return {"id": uuid.uuid4().hex[:12], "done": False, "due": "", "notified": False, "note": "",
            "added": datetime.now().isoformat(timespec="minutes")}


def make_todo(raw, name):
    t = blank()
    tid = t["id"]
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
            todo = [t for t in load() if t.get("retry") and not t["done"]]
        for t in todo:
            (summarize if t["retry"] == "summary" else enrich)(t)
            if not argo_ok:
                break
        if argo_on() and time.time() - argo_at > 600:  # idle: ping it so the status in the UI stays current
            check_argo()
    finally:
        retry_busy.release()


def draft_reply(t):
    """Argo drafts a reply; it opens as a new Outlook message (never sent) and is copied to the clipboard."""
    msg = message_from_bytes((MAILS / f"{t.get('mail', t['id'])}.eml").read_bytes(), policy=policy.default)
    text = argo("Draft my reply to the email below. Write only the reply body: plain text, no subject line, no signature, "
                "match the sender's tone and language, keep it short."
                + (f" My notes on how to answer: {t['note']}" if t["note"] else "")
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


def ask(q):
    """Answer a question from saved emails/tasks: keyword-pick the likeliest ones, then let Argo read them."""
    words = [w for w in re.findall(r"\w+", q.lower()) if w not in STOP and len(w) > 2]
    with lock:
        todos = load()
    hay = lambda t: " ".join([t["title"], t["sender"], t["date"], t["body"], t["note"], *t.get("labels", [])]).lower()
    # ponytail: keyword-overlap ranking over <= a few thousand tasks; embeddings if it misses obvious matches
    hits = sorted(todos, key=lambda t: -sum(hay(t).count(w) for w in words))[:12]
    ctx = "\n\n".join(f"[{t['title']}] from {t['sender'] or '-'} | {t['date'] or t['added']} | due {t['due'] or 'none'} | "
                       f"{'done' if t['done'] else 'open'}\n{t['body'][:1500]}" + (f"\nMy notes: {t['note']}" if t["note"] else "")
                       for t in hits)
    return argo(f"Today is {datetime.now():%A %Y-%m-%d}. Answer my question using only the saved emails/tasks below. "
                "Be brief and plain text; name the item (by its [subject]) the answer comes from. "
                f"If they don't contain the answer, say so.\n\nQuestion: {q}\n\n{ctx}", 2000, timeout=90, tag="ask")


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
        notify(f"Good morning: {head}", (tip or "Start with: " + "; ".join(t["title"] for t in (late + due)[:3])).strip())
    threading.Thread(target=send, daemon=True).start()


def notify(title, msg):
    subprocess.run(["osascript", "-e", "on run argv",
                    "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "default"',
                    "-e", "end run", title, msg])


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
        if time.time() - last_retry > 600:  # every 10 min, and right after launch
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
                    y["score"], y["why"] = scores[0]
            save_feed(f)


def clean_scores(text):
    """{index: (score 0-10, why)} from the model's JSON array reply. Each {...} is parsed on its own,
    so a reply cut off mid-array still keeps the entries before the cut; malformed ones are dropped."""
    out = {}
    for m in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            r = json.loads(m.group(0))
            out[int(r["i"])] = (max(0.0, min(10.0, float(r["score"]))), flat(str(r.get("why", "")), 160))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def rank(items, cfg, liked, disliked):
    lines = "\n".join(f"{i}. [{x['src']}] {x['title']} ({x['by']}): {x['text'][:400]}" for i, x in enumerate(items))
    prompt = (f"My interests: {cfg['scout_profile']}\n"
              + (f"Recently I liked: {'; '.join(liked)}\n" if liked else "")
              + (f"Recently I was NOT interested in: {'; '.join(disliked)}\n" if disliked else "")
              + "Rate how much I'd want to see each item below, 0 (irrelevant) to 10 (must read). Reply with ONLY a JSON "
              'array: [{"i": <number>, "score": <0-10>, "why": "<why it matters to me, max 15 words>"}]\n\n' + lines)
    return clean_scores(argo(prompt, 80 * len(items) + 200, tag="scout"))


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
        found, errors, arxiv_down = [], [], False
        for name, job in jobs:
            is_arxiv = name.startswith("arXiv")
            if is_arxiv and arxiv_down:
                continue  # rate-limited/down: more calls only extend the block
            try:
                found += job()
            except Exception as e:
                arxiv_down = is_arxiv
                errors.append(f"arXiv unavailable ({e}); skipped arXiv this round, will retry next check" if is_arxiv
                              else f"{name}: {e}")
            if is_arxiv:
                time.sleep(3)  # arXiv API asks for 3 s between calls
        yt = [e.split(": ", 1)[0][len("YouTube "):] for e in errors if e.startswith("YouTube ")]
        if yt:  # one line, not one per channel
            errors = [e for e in errors if not e.startswith("YouTube ")]
            errors.append(f"YouTube: couldn't load {', '.join(yt)}; will retry next check")
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
            f["last"], f["errors"] = now, errors
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
                for j, (score, why) in scores.items():
                    if 0 <= j < len(batch) and batch[j]["id"] in by_id:
                        by_id[batch[j]["id"]].update(score=score, why=why)
                        picks += score >= 8
                save_feed(f)
        if picks:
            notify("Scout", f"{picks} new pick{'s' * (picks > 1)} for you")
    finally:
        scout_busy.release()


def scout_loop():
    while True:
        hours = {**SCOUT, **config()}["scout_hours"]
        if load_feed()["last"] < (datetime.now() - timedelta(hours=hours)).isoformat(timespec="minutes"):
            run_scout()  # also catches up right after launch if the Mac was off
        time.sleep(600)


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
        elif self.path == "/api/status":
            with busy_lock:
                working = sorted(k for k, n in busy.items() if n > 0)
            self.reply(200, json.dumps({"configured": argo_on(), "argo": argo_ok, "checked": argo_at, "busy": working,
                                        "rev": DB.stat().st_mtime if DB.exists() else 0}).encode())
        elif self.path == "/feed":
            self.reply(200, FEED_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/scout/settings":
            self.reply(200, json.dumps({k: {**SCOUT, **config()}[k] for k in SCOUT}).encode())
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
                 "date": x["date"], "body": f"{x['url']}\n\n{x['text']}", "eml": False, "labels": ["Reading"]}
            with lock:
                todos = load()
                todos.insert(0, t)
                save(todos)
            threading.Thread(target=summarize, args=(t,), daemon=True).start()
            return self.reply(200)
        if u.path == "/api/argo/check":
            check_argo()
            return self.reply(200)
        if u.path == "/api/ask":
            answer = (ask(json.loads(raw or b"{}").get("q", "")) or "").replace("**", "")  # plain-text box
            return self.reply(200, json.dumps({"answer": answer}).encode()) if answer else self.reply(503)
        if u.path.startswith("/api/reply/"):
            with lock:
                t = next((x for x in load() if x["id"] == u.path.rsplit("/", 1)[1]), None)
            if not t or not t.get("eml"):
                return self.reply(404)
            text = draft_reply(t)
            return self.reply(200, json.dumps({"draft": text}).encode()) if text else self.reply(503)
        if u.path == "/api/feed/add":  # {"url": ...} -> fetched, saved to the Saved list, then scored in the background
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
                    x["saved"] = True
                else:
                    x = {**item, "added": datetime.now().isoformat(timespec="minutes"), "score": None, "why": "",
                         "vote": 0, "saved": True, "read": False, "manual": True}
                    f["items"].insert(0, x)
                    f["seen"] = sorted(set(f["seen"]) | {x["id"]})
                save_feed(f)
            if x["score"] is None:
                threading.Thread(target=score_one, args=(x["id"],), daemon=True).start()
            return self.reply(200, json.dumps({"title": x["title"], "existing": existed}).encode())
        if u.path == "/api/feed/update":  # {"id": ..., "vote"/"saved"/"read": ...}, or {"all_read": true}
            patch = json.loads(raw or b"{}")
            with feed_lock:
                f = load_feed()
                for x in f["items"]:
                    if patch.get("all_read"):
                        x["read"] = True
                    elif x["id"] == patch.get("id"):
                        x.update({k: patch[k] for k in ("vote", "saved", "read") if k in patch})
                save_feed(f)
            return self.reply(200)
        parts = u.path.strip("/").split("/")  # api/drop | api/todo/<id>/<action>
        with lock:
            todos = load()
            if u.path == "/api/drop":
                todos.insert(0, make_todo(raw, parse_qs(u.query).get("name", [""])[0]))
            elif len(parts) == 4 and parts[1] == "todo":
                t = next((x for x in todos if x["id"] == parts[2]), None)
                if not t:
                    return self.reply(404)
                if parts[3] == "update":
                    patch = json.loads(raw or b"{}")
                    for k in ("title", "due", "done", "note", "actions"):
                        if k in patch:
                            t[k] = patch[k]
                    if "labels" in patch:
                        t["labels"] = sorted({str(x).strip() for x in patch["labels"] if str(x).strip()})
                    if "due" in patch:
                        t["notified"] = False
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
.bar{display:flex;gap:8px}
.bar input,.bar select{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
.bar input:focus,.bar select:focus{border-color:var(--accent);box-shadow:0 0 0 3px #0a84ff33}
#new{flex:2}#q{flex:1.3}
main{padding:6px 20px 40px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:18px 4px 6px;font-weight:600}
h2 span{font-weight:400;margin-left:4px}h2.late{color:var(--red)}
.empty{text-align:center;color:var(--muted);padding:60px 0;font-size:15px}
.t{display:flex;gap:10px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;margin:6px 0;transition:background .1s}
.t:hover{background:var(--hover)}
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
.chips{display:contents}.chip{border-radius:10px;padding:1px 8px;cursor:pointer;background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 65% var(--chipL));font-weight:500}
.chip b{display:inline-block;vertical-align:bottom;max-width:0;overflow:hidden;font-weight:400;opacity:0}.chip:hover b{max-width:1em;margin-left:3px;opacity:.7}.chip b:hover{opacity:1}
.addl{border:0;background:none;width:0;padding:1px 0;color:var(--muted);outline:none;opacity:0}
.t:hover .addl,.addl:focus{opacity:1;width:58px;padding:1px 2px}.addl:focus{width:180px;color:var(--text)}
details summary{display:inline-block;font-size:12px;color:var(--muted);cursor:pointer;margin-top:4px;list-style:none;user-select:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"›";display:inline-block;margin-right:5px;transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.5 -apple-system,sans-serif;max-height:340px;overflow:auto;
  background:var(--bg);border-radius:8px;padding:10px 12px;margin:8px 0 6px}
pre a{color:var(--accent)}
textarea{width:100%;background:var(--bg);border:1px solid transparent;border-radius:8px;padding:8px 10px;resize:vertical;outline:none}
textarea:focus{border-color:var(--accent)}
.side{flex:none;display:flex;align-items:center;gap:2px}
.due{border:0;border-radius:6px;padding:3px 8px;background:none;color:var(--muted);cursor:pointer;font-size:12px;white-space:nowrap}
.due.set{background:var(--bg);color:var(--text)}.late .due.set{background:var(--redbg);color:var(--red);font-weight:600}
.due:hover{background:var(--line)}.due.unset{opacity:0}.t:hover .due.unset{opacity:1}
.due b{font-weight:400;margin-left:4px;opacity:0}.due:hover b{opacity:.6}.due b:hover{opacity:1}
.side input{background:var(--card);border:1px solid var(--accent);border-radius:6px;padding:2px 6px;font-size:12px}
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
.ai{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin-top:5px;font-size:12px;color:var(--muted)}
.act{border:1px dashed var(--line);border-radius:10px;padding:1px 8px;cursor:pointer;color:var(--text)}
.act:hover{border-color:var(--accent);color:var(--accent)}.act b{font-weight:400;margin-left:4px;opacity:.5}.act b:hover{opacity:1;color:var(--red)}
.icon.busy{opacity:1;animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.25}}
#ans{position:relative;background:var(--card);border:1px solid var(--accent);border-radius:10px;padding:10px 32px 10px 12px;margin:12px 0 0;white-space:pre-wrap}
#ans button{position:absolute;top:6px;right:6px;border:0;background:none;cursor:pointer;color:var(--muted)}
#toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:10;background:var(--text);color:var(--bg);
  border-radius:8px;padding:8px 14px;font-size:12.5px;opacity:0;transition:opacity .2s;pointer-events:none}#toast.on{opacity:.92}
.pill{margin-left:auto;align-self:center;display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card);
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
  <div class=top><h1>Task Manager</h1><span id=stats></span><button id=argo class=pill></button></div>
  <div class=bar>
    <input id=new placeholder="Add a task (&quot;review Bob's draft by Fri 3pm&quot;) or drag in emails from Outlook">
    <input id=q type=search placeholder="Search, or ask a question ending in ?  ( / )">
    <select id=lf><option value="">All labels</option></select>
    <span class=seg><button id=vl>List</button><button id=vc>Calendar</button></span>
  </div>
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
  d.className='t'+(t.done?' done':'')+(late?' late':'')+(st.busy.includes(t.id)?' thinking':''); d.dataset.id=t.id;
  d.innerHTML=`<input type=checkbox class=cb title="Mark done">
    <div class=main><input class=title type=text spellcheck=false>
      <div class=meta><span class=argoing>✨ Argo is working on this…</span><span class=info></span><span class=chips></span><input class=addl list=labels placeholder="+ label"></div>
      <details><summary>Email & notes</summary><pre></pre><textarea rows=2 placeholder="Notes…"></textarea></details></div>
    <div class=side><button class=due></button><input type=datetime-local hidden>
      <button class="icon r" title="Draft a reply with Argo">${REPLY}</button><button class="icon o" title="Open in Outlook">${MAIL}</button><button class="icon x" title="Delete">${TRASH}</button></div>`;
  const $=s=>d.querySelector(s), up=p=>api(`/api/todo/${t.id}/update`,p);
  const cb=$('.cb'); cb.checked=t.done; cb.onchange=()=>up({done:cb.checked});
  const ti=$('.title'); ti.value=t.title; ti.title=t.title; ti.onchange=()=>up({title:ti.value});
  ti.onkeydown=e=>{if(e.key==='Enter')ti.blur()};
  // friendly due label; the raw date input only appears while editing
  const db=$('.due'),du=$('.side input');
  if(t.due){db.className='due set';db.textContent=fmtDue(t.due);db.title=t.due.replace('T',' ');
    const c=document.createElement('b');c.textContent='×';c.title='Clear due date';
    c.onclick=e=>{e.stopPropagation();up({due:''})};db.append(c)}
  else{db.className='due unset';db.textContent='Set due'}
  du.value=t.due; du.onchange=()=>{if(du.value)up({due:du.value})};
  db.onclick=()=>{db.hidden=true;du.hidden=false;du.focus();try{du.showPicker()}catch{}};
  du.onblur=()=>{if(du.value===t.due){du.hidden=true;db.hidden=false}};
  const r=$('.r'); r.hidden=!t.eml;
  r.onclick=async()=>{if(r.disabled)return;r.disabled=true;r.classList.add('busy');toast('Drafting a reply…');
    const res=await fetch(`/api/reply/${t.id}`,{method:'POST'}).catch(()=>({}));r.disabled=false;r.classList.remove('busy');
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
  const o=$('.o'); o.hidden=!t.eml; o.onclick=()=>api(`/api/todo/${t.id}/open`);
  $('.x').onclick=()=>confirm(`Delete "${t.title}"?`)&&api(`/api/todo/${t.id}/delete`);
  const info=$('.info'); info.textContent=[senderName(t.sender),t.date&&shortDate(t.date)].filter(Boolean).join(' · ');
  info.title=[t.sender,t.date].filter(Boolean).join('\n');
  const labels=t.labels||[];
  for(const l of labels){
    const c=document.createElement('span');c.className='chip';c.style.setProperty('--h',hue(l));
    c.textContent=l;c.title='Show only this label';c.onclick=()=>{lf.value=l;render()};
    const x=document.createElement('b');x.textContent='×';x.title='Remove label';
    x.onclick=e=>{e.stopPropagation();up({labels:labels.filter(y=>y!==l)})};
    c.append(x);$('.chips').append(c);
  }
  const al=$('.addl'); al.onchange=()=>{const v=al.value.trim();if(v)up({labels:[...labels,v]})};
  const hasBody=t.body&&t.body!==t.title, pre=$('pre'); if(hasBody)linkify(pre,t.body); else pre.remove();
  const n=$('textarea'); n.value=t.note; n.onchange=()=>up({note:n.value});
  $('summary').textContent=(hasBody?'Email & notes':'Notes')+(t.note?' ✎':'');
  return d;
}
// Outlook Safe Links / urldefense wrap every URL; show the real one, keep the wrapped href for the security scan
function pretty(u){
  try{const x=new URL(u);if(x.hostname.endsWith('safelinks.protection.outlook.com'))u=x.searchParams.get('url')||u}catch{}
  const d=u.match(/urldefense\.\w+\/v3\/__(.+?)__;/);if(d)u=d[1];
  return u.length>90?u.slice(0,87)+'…':u;
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
const hue=s=>[...s].reduce((h,c)=>(h*31+c.charCodeAt(0))%360,7);
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
      e.dataset.id=t.id;if(st.busy.includes(t.id))e.classList.add('thinking');
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
function render(){
  const names=[...new Set(all.flatMap(t=>t.labels||[]))].sort();
  labels.replaceChildren(...names.map(n=>new Option(n)));
  const cur=lf.value;
  lf.replaceChildren(new Option('All labels',''),...names.map(n=>new Option(n)));
  lf.value=names.includes(cur)?cur:'';
  const words=q.value.trim().endsWith('?')?[]:q.value.toLowerCase().split(/\s+/).filter(Boolean);
  const shown=all.filter(t=>(!lf.value||(t.labels||[]).includes(lf.value))&&words.every(w=>
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
  [all,st]=await Promise.all(['/api/todos','/api/status'].map(u=>fetch(u).then(r=>r.json())));rev=st.rev;render();pill()}
function pill(){  // Argo status in the header: off / reachable / unavailable / working
  const work=st.busy.filter(x=>x!=='check'), checking=st.busy.includes('check'), waiting=all.filter(t=>t.retry&&!t.done).length;
  const ago=st.checked?Math.round(Date.now()/6e4-st.checked/60):null;
  argo.className='pill'+(!st.configured?'':work.length||checking?' busy':st.argo?' ok':st.argo===false?' down':'');
  const dot=document.createElement('span');dot.className='dot';
  argo.replaceChildren(dot,!st.configured?'Argo off':work.length?`Argo working${work.length>1?` on ${work.length}`:''}…`
    :checking?'Checking Argo…':st.argo===false?'Argo unavailable'+(waiting?` · ${waiting} waiting`:''):'Argo');
  argo.title=!st.configured?'LLM features are off. Set "argo_user" in data/config.json to turn them on.'
    :(st.argo===false?'Argo is down, or you are off VPN. Waiting work retries every 10 min.':st.argo?'Argo is reachable.':'Not checked yet.')
    +(ago!=null?` Last contact ${ago<1?'just now':ago<60?ago+' min ago':Math.round(ago/60)+' h ago'}.`:'')+' Click to check now.';
}
async function tick(){
  try{st=await (await fetch('/api/status')).json()}catch{return}
  pill();
  for(const e of document.querySelectorAll('[data-id]'))e.classList.toggle('thinking',st.busy.includes(e.dataset.id));
  // data changed on the server (LLM fill-ins, retries, Scout 📌): reload, unless the user is in the middle of something
  if(st.rev!==rev&&document.activeElement===document.body&&!document.querySelector('.t details[open]'))load();
}
argo.onclick=()=>{fetch('/api/argo/check',{method:'POST'}).then(tick);setTimeout(tick,150)};
setInterval(tick,2000);
q.oninput=lf.onchange=render;
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
</style>
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
  <div class=ph><h3>Save a link</h3><p>An arXiv paper, YouTube video, GitHub repo, or any web page. It goes straight to Saved.</p></div>
  <div class=pb><div class=sec style="margin-top:10px"><textarea id=a_urls rows=3
    placeholder="Paste a link and press Enter (several links: one per line)"></textarea><div id=a_log></div></div></div>
  <div class=pf><span class=msg></span><button type=button class=btn id=a_cancel>Close</button><button class="btn primary" id=a_save>Save</button></div>
</form></dialog>
<script>
const SRCS=['All','arXiv','YouTube','GitHub','Saved'];
let feed={items:[]}, src=localStorage.src||'All';
const post=b=>fetch('/api/feed/update',{method:'POST',body:JSON.stringify(b)}).then(load);
function ago(s){if(!s)return 'never';const m=Math.round((Date.now()-new Date(s))/6e4);
  return m<1?'just now':m<60?m+' min ago':m<1440?Math.round(m/60)+' h ago':Math.round(m/1440)+' d ago'}
function card(x){
  const d=document.createElement('div');d.className='c'+(x.read?' read':'');
  d.innerHTML=`<div class=score></div><div class=body><a class=title target=_blank></a>
    <div class=meta><span class=src></span> · <span class=by></span></div><div class=why></div>
    <details><summary>Details</summary><p></p></details></div>
    <div class=acts><button title="More like this">👍</button><button title="Less like this">👎</button><button title="Save">☆</button><button title="Add to Task Manager as a reading task">📌</button></div>`;
  const $=s=>d.querySelector(s);
  const sc=$('.score');sc.textContent=x.score==null?'·':Math.round(x.score);
  sc.title=x.score==null?'Not ranked yet (Argo unreachable?)':'Score '+x.score;
  sc.style.setProperty('--h',x.score==null?220:Math.round(x.score*12));
  const a=$('.title');a.href=x.url;a.textContent=x.title;a.onclick=()=>{if(!x.read)post({id:x.id,read:true})};
  $('.src').textContent=x.src;$('.by').textContent=[x.by,x.date,x.manual&&'added by you'].filter(Boolean).join(' · ');
  $('.why').textContent=x.why;
  if(x.text)$('details p').textContent=x.text;else $('details').remove();
  const [up,down,save,task]=d.querySelectorAll('.acts button');
  task.classList.toggle('on',!!x.task);if(x.task)task.title='Added to Task Manager';
  task.onclick=()=>x.task||fetch('/api/feed/task',{method:'POST',body:JSON.stringify({id:x.id})}).then(load);
  up.classList.toggle('on',x.vote>0);down.classList.toggle('on',x.vote<0);save.classList.toggle('on',x.saved);
  if(x.saved)save.textContent='★';
  up.onclick=()=>post({id:x.id,vote:x.vote>0?0:1});down.onclick=()=>post({id:x.id,vote:x.vote<0?0:-1,read:true});
  save.onclick=()=>post({id:x.id,saved:!x.saved});
  return d;
}
function render(){
  srcs.replaceChildren(...SRCS.map(s=>{const b=document.createElement('button');b.textContent=s;b.className=s===src?'on':'';
    b.onclick=()=>{src=s;localStorage.src=s;render()};return b}));
  const words=q.value.toLowerCase().split(/\s+/).filter(Boolean);
  const shown=feed.items.filter(x=>(src==='All'||(src==='Saved'?x.saved:x.src===src))
    &&(src==='Saved'||(x.vote>=0&&(low.checked||x.score==null||x.score>=5)))
    &&words.every(w=>[x.title,x.by,x.text,x.why].join(' ').toLowerCase().includes(w)))
    .sort((a,b)=>((b.score??4.5)-(a.score??4.5))||b.date.localeCompare(a.date));
  list.replaceChildren(...shown.slice(0,300).map(card));
  if(!shown.length){const e=document.createElement('div');e.className='empty';
    e.textContent=feed.last?'Nothing here yet.':'First check is running…';list.append(e)}
  const picks=feed.items.filter(x=>!x.read&&x.vote>=0&&x.score>=7).length;
  stats.textContent=(feed.busy?'Checking… · ':'')+`Last checked ${ago(feed.last)}`+(picks?` · ${picks} unread pick${picks>1?'s':''}`:'');
  errs.textContent=(feed.errors||[]).map(e=>'⚠︎ '+e).join('  ');
  document.title=picks?`Scout (${picks})`:'Scout';
}
async function load(){feed=await (await fetch('/api/feed')).json();render()}
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
    if(r.ok)row.textContent=(j.existing?'★ Already in Scout, now saved: ':'★ Saved: ')+j.title;
    else{row.textContent=`⚠︎ ${u}: ${j.error}`;row.classList.add('bad');bad.push(u)}
  }
  a_save.disabled=false;a_urls.value=bad.join('\n');
  src='Saved';localStorage.src=src;load();setTimeout(load,10000);  // Argo's score lands a few seconds later
  if(!bad.length)setTimeout(()=>adddlg.close(),1200);
};
p_cancel.onclick=()=>discard()&&prefs.close();
prefs.oncancel=e=>{if(!discard())e.preventDefault()};  // Esc
p_reset.onclick=()=>confirm('Reset interests, search terms and channels to the defaults?')&&savePrefs({reset:true});
pform.onsubmit=e=>{e.preventDefault();savePrefs(JSON.parse(state()))};
addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();q.focus()}});
q.onkeydown=e=>{if(e.key==='Escape'){q.value='';render();q.blur()}};
let tick=0;setInterval(()=>{if((feed.busy||++tick%6===0)&&!document.querySelector('details[open]'))load()},5000);
load();
</script>"""

if __name__ == "__main__":
    for d in (MAILS, INBOX):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=scout_loop, daemon=True).start()
    print(f"Survey Corps on {ORIGIN}  (data in {DATA}, Finder fallback inbox: {INBOX})")
    if "--no-browser" not in sys.argv:  # the Mac app passes this and shows its own window
        subprocess.run(["open", "-a", "Safari", ORIGIN])  # Chromium browsers drop Outlook drags
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
