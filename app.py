#!/usr/bin/env python3
"""Survey Corps: local todo list fed by emails dragged from Outlook. Run: python3 app.py"""
import html, json, re, subprocess, sys, threading, time, urllib.request, uuid
from datetime import datetime
from email import message_from_bytes, policy
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
lock = threading.Lock()


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
    """Pull {"labels", "due"} out of the model's reply, dropping anything malformed or already past."""
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
    return {"labels": [l for l in labels if len(l) <= 24], "due": due if ok else ""}


def config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def argo(prompt, max_tokens):
    """Ask the lab's Argo LLM. Returns the reply text, or None if unconfigured or unreachable (e.g. no VPN)."""
    cfg = config()
    if not cfg.get("argo_user"):
        return None
    req = urllib.request.Request(ARGO, json.dumps({
        "model": cfg.get("argo_model", "claudesonnet5"), "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]}).encode(),
        {"Content-Type": "application/json", "Authorization": "Bearer " + cfg["argo_user"]})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    except Exception:
        return None


def suggest(t, known):
    """Labels and a due date for a dropped email, or {} if Argo isn't available."""
    now = datetime.now()
    prompt = (f"Today is {now:%A %Y-%m-%d %H:%M} (local time). Existing labels: {json.dumps(known)}.\n"
              "Below is an email I saved as a to-do. Reply with ONLY a JSON object "
              '{"labels": [...], "due": "..."}.\n'
              "- labels: up to 3 short labels (1-2 words, Title Case). Reuse an existing label whenever one fits; "
              "invent a new one only if none does.\n"
              '- due: when I must act by, as local YYYY-MM-DDTHH:MM. Resolve relative dates ("by Friday") against the '
              'email\'s own Date header; if only a date is given use 17:00. Use "" if the email states no deadline '
              "(do not invent one).\n\n"
              f"Subject: {t['title']}\nFrom: {t['sender']}\nDate: {t['date']}\n\n{t['body'][:3000]}")
    reply = argo(prompt, 200)
    return clean_suggestion(reply, now) if reply else {}


def enrich(t):
    """Background: fill in labels/due on a freshly dropped email, unless the user already set them."""
    with lock:
        known = sorted({l for x in load() for l in x.get("labels", [])})
    s = suggest(t, known)
    if not s:
        return
    with lock:
        todos = load()
        cur = next((x for x in todos if x["id"] == t["id"]), None)
        if not cur:
            return
        if s["labels"] and not cur.get("labels"):
            cur["labels"] = sorted(set(s["labels"]))
        if s["due"] and not cur["due"]:
            cur["due"], cur["notified"] = s["due"], False
        save(todos)


def make_todo(raw, name):
    tid = uuid.uuid4().hex[:12]
    t = {"id": tid, "done": False, "due": "", "notified": False, "note": "",
         "added": datetime.now().isoformat(timespec="minutes")}
    msg = message_from_bytes(raw, policy=policy.default)
    if name.lower().endswith(".eml") or (msg["From"] and msg["Subject"] is not None):
        (MAILS / f"{tid}.eml").write_bytes(raw)
        text = email_text(msg)
        t.update(title=str(msg["Subject"] or "(no subject)"), sender=str(msg["From"] or ""),
                 date=str(msg["Date"] or ""), body=text.strip()[:3000], eml=True)
        threading.Thread(target=enrich, args=(t,), daemon=True).start()
    else:  # plain text (manual todo, or Outlook only handed over text)
        text = raw.decode("utf-8", "replace").strip()
        t.update(title=(text.splitlines() or ["(empty)"])[0][:200], sender="", date="",
                 body=text[:3000], eml=False)
    return t


def notify(title, msg):
    subprocess.run(["osascript", "-e", "on run argv",
                    "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "default"',
                    "-e", "end run", title, msg])


def watcher():
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
        time.sleep(20)


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
        else:
            self.reply(404)

    def do_POST(self):
        if self.headers.get("Origin") not in (None, ORIGIN):  # block other websites
            return self.reply(403)
        u = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
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
                    for k in ("title", "due", "done", "note"):
                        if k in patch:
                            t[k] = patch[k]
                    if "labels" in patch:
                        t["labels"] = sorted({str(x).strip() for x in patch["labels"] if str(x).strip()})
                    if "due" in patch:
                        t["notified"] = False
                elif parts[3] == "delete":
                    todos.remove(t)
                    (MAILS / f"{t['id']}.eml").unlink(missing_ok=True)
                elif parts[3] == "open":
                    eml = MAILS / f"{t['id']}.eml"
                    if subprocess.run(["open", "-a", "Microsoft Outlook", eml]).returncode:
                        subprocess.run(["open", eml])  # Outlook missing: default mail app
            else:
                return self.reply(404)
            save(todos)
        self.reply(200)

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Survey Corps</title>
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
#drop{display:none;position:fixed;inset:8px;z-index:9;pointer-events:none;align-items:center;justify-content:center;
  border:3px dashed var(--accent);border-radius:14px;background:#0a84ff1a;color:var(--accent);font-size:20px;font-weight:600}
#drop.over{display:flex}
</style>
<div id=drop>Drop email to add a task</div>
<header>
  <div class=top><h1>Survey Corps</h1><span id=stats></span></div>
  <div class=bar>
    <input id=new placeholder="Add a task and press Enter, or drag an email from Outlook anywhere">
    <input id=q type=search placeholder="Search  ( / )">
    <select id=lf><option value="">All labels</option></select>
    <span class=seg><button id=vl>List</button><button id=vc>Calendar</button></span>
  </div>
</header>
<datalist id=labels></datalist>
<main><div id=cal hidden></div><div id=list></div><details id=donebox><summary id=donehdr></summary><div id=donelist></div></details></main>
<script>
const api=(p,b)=>fetch(p,{method:'POST',body:typeof b==='string'||b instanceof Blob?b:JSON.stringify(b)}).then(load);
const nowStr=()=>new Date(Date.now()-new Date().getTimezoneOffset()*6e4).toISOString().slice(0,16);
const DAY=864e5, dayDiff=d=>Math.round((new Date(d).setHours(0,0,0,0)-new Date().setHours(0,0,0,0))/DAY);
const MAIL='<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6 8.5-6"/></svg>';
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
  d.className='t'+(t.done?' done':'')+(late?' late':'');
  d.innerHTML=`<input type=checkbox class=cb title="Mark done">
    <div class=main><input class=title type=text spellcheck=false>
      <div class=meta><span class=info></span><span class=chips></span><input class=addl list=labels placeholder="+ label"></div>
      <details><summary>Email & notes</summary><pre></pre><textarea rows=2 placeholder="Notes…"></textarea></details></div>
    <div class=side><button class=due></button><input type=datetime-local hidden>
      <button class="icon o" title="Open in Outlook">${MAIL}</button><button class="icon x" title="Delete">${TRASH}</button></div>`;
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
  const words=q.value.toLowerCase().split(/\s+/).filter(Boolean);
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
  document.title=`Survey Corps (${openAll.length})`;
}
async function load(){all=await (await fetch('/api/todos')).json();render()}
q.oninput=lf.onchange=render;
vl.onclick=()=>{view='list';localStorage.view=view;render()};vc.onclick=()=>{view='cal';localStorage.view=view;render()};
document.getElementById('new').onkeydown=e=>{
  const v=e.target.value.trim();
  if(e.key==='Enter'&&v){e.target.value='';api('/api/drop?name=todo.txt',v)}
  if(e.key==='Escape')e.target.blur();
};
addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){e.preventDefault();q.focus()}});
q.onkeydown=e=>{if(e.key==='Escape'){q.value='';render();q.blur()}};
addEventListener('dragover',e=>{e.preventDefault();if(!dragId)drop.classList.add('over')});
addEventListener('dragleave',e=>{if(!e.relatedTarget)drop.classList.remove('over')});  // only when leaving the window
addEventListener('drop',async e=>{
  e.preventDefault(); if(dragId)return; drop.classList.remove('over');
  const files=[...e.dataTransfer.files];
  for(const f of files) await fetch('/api/drop?name='+encodeURIComponent(f.name),{method:'POST',body:f});
  const txt=!files.length&&(e.dataTransfer.getData('text/plain')||e.dataTransfer.getData('text/uri-list'));
  if(txt) await fetch('/api/drop?name=drop.txt',{method:'POST',body:txt});
  load(); setTimeout(load,8000); setTimeout(load,20000);  // labels/due arrive from the LLM a few seconds later
});
setInterval(()=>{if(!document.querySelector('.t details[open]')&&document.activeElement===document.body)load()},30000);
load();
</script>"""

if __name__ == "__main__":
    for d in (MAILS, INBOX):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watcher, daemon=True).start()
    print(f"Survey Corps on {ORIGIN}  (data in {DATA}, Finder fallback inbox: {INBOX})")
    if "--no-browser" not in sys.argv:  # the Mac app passes this and shows its own window
        subprocess.run(["open", "-a", "Safari", ORIGIN])  # Chromium browsers drop Outlook drags
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
