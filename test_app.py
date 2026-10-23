from datetime import datetime
from pathlib import Path
from app import SCOUT, channel, clean_scores, clean_settings, clean_suggestion, parse_arxiv, parse_youtube_page
now = datetime(2026, 10, 17, 9, 0)
assert clean_suggestion('x {"labels": ["A", " B ", "", "C", "D"], "due": "2026-10-20T17:00"} y', now) == {"labels": ["A", "B", "C"], "due": "2026-10-20T17:00", "title": "", "actions": []}
assert clean_suggestion('{"title": " Review draft ", "actions": ["only one"]}', now) == {"labels": [], "due": "", "title": "Review draft", "actions": []}  # 1 action = no split
assert clean_suggestion('{"actions": ["Send slides", "", "Book room"]}', now)["actions"] == ["Send slides", "Book room"]
assert clean_suggestion('{"labels": [], "due": "2026-10-01T17:00"}', now)["due"] == ""  # past
assert clean_suggestion('{"due": "next friday"}', now)["due"] == ""                      # malformed
assert clean_suggestion("no json", now) == {"labels": [], "due": "", "title": "", "actions": []}
assert clean_scores('ok [{"i": 0, "score": 12, "why": "x"}, {"i": "1", "score": 3}, {"score": 5}, {"i": 2, "score": "?"}]') == {0: (10.0, "x", []), 1: (3.0, "", [])}
assert clean_scores('[{"i": 0, "score": 7, "why": "a", "labels": ["ptychography", "Made Up", "AI"]}]', ["Ptychography", "AI"]) == {0: (7.0, "a", ["AI", "Ptychography"])}
assert clean_scores(None) == {} and clean_scores("[oops") == {}
assert clean_scores('[{"i": 0, "score": 7, "why": "a"}, {"i": 1, "score": 4, "wh') == {0: (7.0, "a", [])}  # truncated reply
a = parse_arxiv(b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2610.01234v2</id>
<title>A  new\n ptychography</title><published>2026-10-15T00:00:00Z</published><summary> abc </summary>
<author><name>A</name></author><author><name>B</name></author><author><name>C</name></author><author><name>D</name></author></entry></feed>''')
assert a == [{"id": "arxiv:2610.01234", "src": "arXiv", "title": "A new ptychography", "url": "https://arxiv.org/abs/2610.01234",
              "date": "2026-10-15", "by": "A, B, C et al.", "text": "abc"}], a

# Argo down -> task marked for retry; Argo back -> retry fills it in and clears the mark
import tempfile
import app
app.DB = Path(tempfile.mkdtemp()) / "todos.json"
app.config = lambda: {"argo_user": "x"}
app.ARGO = "http://127.0.0.1:9/"  # nothing listens here: connection refused
t = {**app.blank(), "title": "review draft by fri", "body": "review draft by fri", "sender": "", "date": "", "eml": False}
app.save([t])
app.enrich(t)
assert app.load()[0]["retry"] == "enrich" and app.argo_ok is False and not +app.busy  # busy count released on failure
def back(prompt, max_tokens, **kw):
    app.argo_ok = True
    return '{"title": "Review draft", "labels": ["Review"], "due": ""}'
app.argo = back
app.retry_argo()
x = app.load()[0]
assert "retry" not in x and x["title"] == "Review draft" and x["labels"] == ["Review"] and x["body"] == "", x
c = clean_settings({"scout_profile": "  ", "scout_keywords": [" a  b ", "", 3], "scout_hours": "99"})
assert c == {"scout_keywords": ["a b", "3"], "scout_arxiv_keywords": [], "scout_youtube": [], "scout_profile": SCOUT["scout_profile"],
             "scout_hours": 48}, c
assert clean_settings({"scout_hours": "x"})["scout_hours"] == 3
assert channel("UCMLtBahI5DMrt0NPvDSoIRQ   MLST  ") == "UCMLtBahI5DMrt0NPvDSoIRQ MLST"  # ids are kept without a lookup
import json
from datetime import timedelta
lock = {"lockupViewModel": {"contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "contentId": "abc", "metadata": {"lockupMetadataViewModel": {
    "title": {"content": "Agents  rule"}, "metadata": {"contentMetadataViewModel": {"metadataRows": [{"metadataParts": [
        {"text": {"content": "1K"}, "accessibilityLabel": "1 thousand views"}, {"text": {"content": "2w ago"}, "accessibilityLabel": "2 weeks ago"}]}]}}}}}}
page = '<meta property="og:title" content="Chan &amp; Co"><script>var ytInitialData = ' + json.dumps({"a": [lock, {"x": 1}]}) + ';</script>'
v = parse_youtube_page(page)
assert [(x["id"], x["title"], x["by"]) for x in v] == [("yt:abc", "Agents rule", "Chan & Co")], v
assert v[0]["date"] == (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

# Library: Argo's suggestion is validated, groups/labels edits cascade to items, folders are split into projects
assert app.clean_lib_suggestion('x {"summary": " A  tool ", "group": "x", "labels": ["Py", "", "' + "x" * 30 + '"]}') == {
    "summary": "A tool", "labels": ["Py"]}
assert app.clean_lib_suggestion("nope") == {"summary": "", "labels": []}
x = {"folder": False, "path": "/a/b.PDF", "peek": "Abstract. We show"}
assert app.auto_type(x) == "Papers" and app.auto_type({**x, "peek": "Invoice"}) == "Docs" and app.auto_type({**x, "path": "/a/c.h5"}) == "Data"
assert app.auto_type({**x, "folder": True}) == "Folders" and app.auto_type({**x, "src": "YouTube"}) == "Videos"
lib = {"groups": ["A", "B"], "labels": {"X": 40}, "items": [{"group": "A", "labels": ["X", "Y"]}, {"group": "B", "labels": ["Y"]}]}
app.lib_meta(lib, {"op": "rename_group", "name": "A", "to": "B"})  # merge
assert lib["groups"] == ["B"] and [x["group"] for x in lib["items"]] == ["B", "B"]
app.lib_meta(lib, {"op": "rename_label", "name": "X", "to": "Y"})
assert lib["labels"] == {"Y": 40} and [x["labels"] for x in lib["items"]] == [["Y"], ["Y"]]
app.lib_meta(lib, {"op": "delete_group", "name": "B"}); app.lib_meta(lib, {"op": "delete_label", "name": "Y"})
assert lib["groups"] == [] and lib["items"] == [{"group": "", "labels": []}, {"group": "", "labels": []}] and lib["labels"] == {}
root = Path(tempfile.mkdtemp())
app.LIB = root / "library.json"
(root / "proj1").mkdir(); (root / "proj1" / "README.md").write_text("# Ptycho recon"); (root / "proj2").mkdir(); (root / ".hidden").mkdir()
app.lib_enrich = lambda i: None
new, skipped = app.lib_add([str(root), str(root / "nope"), ""], split=True)
assert sorted(x["name"] for x in new) == ["proj1", "proj2"] and skipped == [f"{root / 'nope'}: not found"], (new, skipped)
assert "README.md" in new[0]["peek"] and "# Ptycho recon" in new[0]["peek"]
assert app.lib_add([str(root / "proj1")])[1] == ["proj1: already in the Library"]
# Settings: saved to config.json, blank text = default, blank token keeps the old one, token never read back
import tempfile
app.CONFIG = Path(tempfile.mkdtemp()) / "config.json"
app.config = lambda: __import__("json").loads(app.CONFIG.read_text()) if app.CONFIG.exists() else {}  # undo earlier patch
app.set_settings({"argo_user": " bob ", "retry_minutes": "30", "github_token": "tok", "bogus": 1, "argo_model": "claudesonnet5"})
c = app.get_settings()
assert (c["argo_user"], c["retry_minutes"], c["github_token"], c["github_token_set"]) == ("bob", 30, "", True), c
assert "argo_model" not in app.config() and "bogus" not in app.config()  # default values aren't stored
app.set_settings({"github_token": "", "retry_minutes": 9999, "argo_user": ""})
assert app.config() == {"retry_minutes": 1440, "github_token": "tok"}, app.config()
app.set_settings({"clear_github_token": True})
assert "github_token" not in app.config()
print("ok")
# Pets: routines parse and fire once a day; /api/pets sums it up
app.set_settings({"routines": "fri 17 task: what did I finish?\nweekdays 9 scout: top 3\nsun 25 task: bad hour\nnonsense"})
assert [(r["days"], r["hour"], r["pet"]) for r in app.routines()] == [("fri", 17, "task"), ("weekdays", 9, "scout")]
app.RAN = Path(tempfile.mkdtemp()) / "ran.json"
fri5 = datetime(2026, 10, 23, 17, 30)
assert [r["pet"] for r in app.due_routines(fri5)] == ["task", "scout"] and app.due_routines(fri5) == []  # once a day
assert app.due_routines(datetime(2026, 10, 24, 18)) == []                                              # saturday: neither
app.FEED = Path(tempfile.mkdtemp()) / "feed.json"
app.save([{**app.blank(), "title": "x", "due": "2020-01-01T00:00"}])
s = app.pets()
assert s["overdue"] == 1 and s["routines"][0] == {"line": "fri 17 task: what did I finish?", "pet": "task"}
app.say("scout", "Scout", "2 new picks")
assert app.pets()["bubbles"][-1]["text"] == "2 new picks" and app.pets(after=app.bubbles[-1]["id"])["bubbles"] == []
seen = []
app.argo = lambda prompt, n, **kw: seen.append(prompt) or "Friday."
assert app.ask_mail({**app.blank(), "title": "Beamtime", "body": "Slot is Friday", "sender": "Bob", "date": ""}, "when?") == "Friday."
assert "Question: when?" in seen[-1] and "Slot is Friday" in seen[-1]
print("ok")

# shared labels: renaming cascades; agentic search runs tools then resolves refs, never leaking hidden paths
lib = {"groups": [], "labels": {}, "items": [{"labels": ["Ptycho", "B"]}, {"labels": ["B"]}]}
app.relabel(lib["items"], "Ptycho", "Ptychography")
assert [x["labels"] for x in lib["items"]] == [["B", "Ptychography"], ["B"]]
assert app.readable(app.HOME / ".ssh") is None and app.readable(app.HOME / "Library") is None and app.readable(app.HOME) is None
t = {**app.blank(), "title": "Beamtime proposal", "body": "draft due", "sender": "Bob", "date": "", "eml": True, "labels": ["Beamtime"]}
app.save([t])
turns = iter(['{"search": "beamtime proposal"}', '{"read": "task:%s"}' % t["id"],
              '{"answer": "Bob sent it.", "refs": ["task:%s", "/nope", "%s"]}' % (t["id"], app.HOME / ".ssh")])
seen = []
app.argo = lambda prompt, n, **kw: seen.append(prompt) or next(turns)
r = app.lib_ask("where is the beamtime proposal?")
assert r["answer"] == "Bob sent it." and [x["ref"] for x in r["refs"]] == ["task:" + t["id"]], r
assert r["steps"][0] == "Searched “beamtime proposal”" and "draft due" in seen[2] and "Beamtime proposal" in seen[1]
app.argo = lambda prompt, n, **kw: None
assert app.lib_ask("x?") is None
print("ok")

# links in the Library: read by Scout's link reader, deduped by address; a bad link is skipped with why
app.LIB = Path(tempfile.mkdtemp()) / "library.json"
app.link_item = lambda url: {"id": "yt:abc", "src": "YouTube", "title": "Ptycho talk", "url": url, "by": "Ch", "date": "2026-10-19", "text": ""}
app.argo = lambda prompt, n, **kw: None
new, skipped = app.lib_add(["https://www.youtube.com/watch?v=abc", "www.youtube.com/watch?v=abc"])
assert [(x["name"], x["src"], x["path"], x["peek"]) for x in new] == [("Ptycho talk", "YouTube", "https://www.youtube.com/watch?v=abc", "Ch\n2026-10-19")], new
assert skipped == ["Ptycho talk: already in the Library"], skipped
def boom(url): raise OSError("offline")
app.link_item = boom
assert app.lib_add(["https://example.com"])[1] == ["https://example.com: couldn't read that link (offline)"]
# old groups -> automatic types; the user's own group survives
import json
app.LIB.write_text(json.dumps({"groups": ["Projects", "Papers", "Mine"], "labels": {}, "items": [
    {"group": "Projects", "folder": True, "path": "/p", "peek": "", "labels": []},
    {"group": "Mine", "folder": False, "path": "/a.py", "peek": "", "labels": []},
    {"group": "", "folder": False, "path": "/b.key", "peek": "", "labels": []}]}))
m = app.load_lib()
assert m["groups"] == app.TYPES + ["Mine"] and [x["group"] for x in m["items"]] == ["Folders", "Mine", "Slides"] and m["v"] == 2, m
print("ok")

# one-time Scout relabel: only unlabeled 5+ items, never over the user's labels, retried until Argo answers
app.FEED = Path(tempfile.mkdtemp()) / "feed.json"
it = lambda i, score, labels: {"id": i, "src": "arXiv", "title": i, "by": "", "text": "", "score": score, "why": "w", "labels": labels}
app.save_feed({"last": "", "errors": [], "seen": [], "items": [it("a", 8, []), it("b", 3, []), it("c", 7, ["Mine"])]})
app.argo = lambda prompt, n, **kw: None
app.relabel_scout()
assert "relabeled" not in app.load_feed()  # Argo down: try again next launch
seen = []
app.argo = lambda prompt, n, **kw: seen.append(prompt) or '[{"i": 0, "score": 2, "labels": ["mine", "Made Up"]}]'
app.relabel_scout()
f = app.load_feed()
assert [(x["labels"], x["score"], x["why"]) for x in f["items"]] == [(["Mine"], 8, "w"), ([], 3, "w"), (["Mine"], 7, "w")], f
assert f["relabeled"] and len(seen) == 1 and "0. [arXiv] a" in seen[0] and "\n1. " not in seen[0]  # only "a" asked
app.relabel_scout()
assert len(seen) == 1  # done once
print("ok")

# 📚 on a Scout card: into the Library with its labels, once
app.LIB = Path(tempfile.mkdtemp()) / "library.json"
app.lib_enrich = lambda i: None
x = {"id": "arxiv:1", "src": "arXiv", "title": "P", "url": "https://arxiv.org/abs/1", "by": "A", "date": "2026-01-01", "text": "abs", "labels": ["Mine"]}
assert app.feed_to_lib(x) and not app.feed_to_lib(x)
[y] = app.load_lib()["items"]
assert (y["path"], y["name"], y["labels"], y["group"]) == (x["url"], "P", ["Mine"], "Papers"), y
print("ok")

# coding agents: Claude Code hook events -> states; dead pids dropped; Codex threads from the Codex app's thread list
import os, time as _t
said = []
app.say = lambda pet, title, text: said.append((title, text))
app.cc.clear()
app.CLAUDE = Path(tempfile.mkdtemp())  # no real transcripts
ev = lambda name, sid="s1", **kw: app.agent_event({"session_id": sid, "cwd": "/x/proj", "hook_event_name": name, **kw}, "cc", os.getpid())
ev("SessionStart"); ev("UserPromptSubmit")
app.agent_event({"session_id": "dead", "cwd": "/x/gone", "hook_event_name": "UserPromptSubmit"}, "cc", 999999)
app.CODEX = Path(tempfile.mkdtemp())
import sqlite3
db = sqlite3.connect(app.CODEX / "state_5.sqlite")
db.execute("create table threads (id, name, title, cwd, rollout_path, updated_at_ms, recency_at_ms, thread_source, archived)")
ms = int(_t.time() * 1000)
db.executemany("insert into threads values (?,?,?,?,?,?,?,?,?)", [
    ("t1", "", "Plan the house", "/y/cdx", "/nope.jsonl", ms, ms - 5, "user", 0),
    ("t2", "", "Guardian review", "/y/cdx", "", ms, ms, "guardian_review", 0),       # Codex's own subthread
    ("t3", "", "Old idea", "/y/cdx", "", ms, ms, "user", 1),                           # archived
    ("t4", "", "Last week", "/y/cdx", "", ms - 86400_000 * 7, 0, "user", 0)])          # outside the window
db.commit(); db.close()
assert app.agents() == [{"tool": "Claude Code", "where": "proj", "state": "working", "mins": 0, "open": app.app_of(os.getpid()), "host": "", "dir": "/x/proj"},
                        {"tool": "Codex", "where": "Plan the house", "state": "working", "mins": 0, "open": "codex://threads/t1", "host": "", "dir": "/y/cdx"}], app.agents()
assert app.app_of(os.getpid()).startswith("file:///") and app.app_of(os.getpid()).endswith(".app") and app.app_of(1) == ""
ev("Notification", notification_type="idle_prompt", message="Claude is waiting for your input")
ev("Notification", message="Claude needs your permission to use Bash")
ev("Stop")
_t.sleep(0.2)  # bubbles are said from a thread
assert app.cc["s1"]["state"] == "idle" and said == [("Claude Code", "Needs you in proj: Claude needs your permission to use Bash"),
                                                    ("Claude Code", "Finished in proj")], said
app.agent_event({"type": "agent-turn-complete", "cwd": "/y/cdx", "last-assistant-message": "Done."}, "codex"); _t.sleep(0.2)
assert said[-1] == ("Codex", "Finished in cdx: Done.")
n = len(said)
app.agent_event({"type": "agent-turn-complete", "thread-id": "t2", "cwd": "/y/cdx", "last-assistant-message": "ok"}, "codex")
app.agent_event({"type": "agent-turn-complete", "thread-id": "t1", "cwd": "/y/cdx", "last-assistant-message": "Built."}, "codex")
_t.sleep(0.2)
assert said[n:] == [("Codex", "Finished in cdx: Built.")], said[n:]  # t2 is Codex's own Guardian review: silent
ev("SessionEnd")
assert "s1" not in app.cc and [a["tool"] for a in app.agents()] == ["Codex"]
assert app.agents(now=_t.time() + 120)[0]["state"] == "idle" and app.agents(now=_t.time() + 13 * 3600) == []
print("ok")

iso = lambda secs_ago: datetime.utcfromtimestamp(_t.time() - secs_ago).isoformat() + "Z"
# remote Claude Code (hooks on an SSH host, through the tunnel): no pid check, opens VS Code on that host
said.clear()
app.agent_event({"session_id": "r", "cwd": "/home/me/My Proj", "hook_event_name": "UserPromptSubmit"}, "cc", 4242, "me@refiner.lab.gov")
app.agent_event({"session_id": "r", "cwd": "/home/me/My Proj", "hook_event_name": "Stop", "transcript_path": "/home/me/x.jsonl"}, "cc", 4242, "me@refiner.lab.gov")
_t.sleep(0.2)
r = next(a for a in app.agents() if a["tool"] == "Claude Code @refiner")
assert (r["where"], r["state"], r["open"]) == ("My Proj", "idle", "vscode://vscode-remote/ssh-remote+me@refiner.lab.gov/home/me/My%20Proj"), r
assert said == [("Claude Code @refiner", "Finished in My Proj")] and "cache" not in r, said
assert not any(a["tool"].startswith("Claude Code @") for a in app.agents(now=_t.time() + 13 * 3600))  # quiet too long: gone
app.cc.clear()
# ...and from remote_scan(): transcripts read over SSH; hosts sharing a home list each session once
rt = Path(tempfile.mkdtemp()) / "rt.jsonl"
rt.write_text(json.dumps({"type": "user", "cwd": "/h/TomoX", "entrypoint": "claude-vscode", "timestamp": iso(30)}) + "\n"
              + json.dumps({"type": "user", "cwd": "/h/talks", "timestamp": iso(30)}) + "\n"
              + json.dumps({"type": "ai-title", "aiTitle": "Radiation damage"}) + "\n")
info = app.transcript_info(rt)
assert (info["cwd"], info["title"]) == ("/h/TomoX", "Radiation damage"), info  # where it started; Claude's title
app.remote = {"me@a.x": [{"id": "rt", "mtime": _t.time(), **info}], "me@b.x": [{"id": "rt", "mtime": _t.time(), **info}]}
assert [(a["tool"], a["where"], a["state"], a["open"]) for a in app.agents() if "@" in a["tool"]] == [
    ("Claude Code @a", "Radiation damage", "working", "vscode://vscode-remote/ssh-remote+me@a.x/h/TomoX")]
app.agent_event({"session_id": "rt", "cwd": "/h/talks", "hook_event_name": "Stop"}, "cc", 1, "me@a.x"); _t.sleep(0.2)
assert [(a["where"], a["host"], a["dir"], a["open"]) for a in app.agents() if "@" in a["tool"]] == [
    ("Radiation damage", "a", "/h/TomoX", "vscode://vscode-remote/ssh-remote+me@a.x/h/TomoX")]  # hooked: title from the log
# a permission prompt answered (or auto mode switched on) fires no hook: the log moving on means it works again
app.agent_event({"session_id": "rt", "hook_event_name": "Notification", "message": "Claude needs your permission"}, "cc", 1, "me@a.x")
_t.sleep(0.2); assert [a["state"] for a in app.agents() if "@" in a["tool"]] == ["waiting"]  # log older than the prompt
app.remote["me@a.x"][0]["last"] = _t.time() + 30
assert [a["state"] for a in app.agents() if "@" in a["tool"]] == ["working"]
app.remote = {}; app.cc.clear()
print("ok")

# prompt cache: last API call + the TTL it wrote (1 h or 5 min); shown on idle sessions only
tr = Path(tempfile.mkdtemp()) / "s.jsonl"
call = lambda ts, m5, h1: json.dumps({"type": "assistant", "timestamp": ts, "message": {"usage": {
    "cache_creation": {"ephemeral_5m_input_tokens": m5, "ephemeral_1h_input_tokens": h1}}}})
tr.write_text(call("2026-10-20T04:00:00.000Z", 0, 900) + "\n" + json.dumps({"type": "user"}) + "\n")
t0 = datetime.fromisoformat("2026-10-20T04:00:00+00:00").timestamp()
assert app.cache_expiry(tr) == t0 + 3600
tr.write_text(call("2026-10-20T04:00:00.000Z", 0, 900) + "\n" + call("2026-10-20T04:10:00.000Z", 50, 0) + "\n")
assert app.cache_expiry(tr) == t0 + 600 + 300 and app.cache_expiry(tr.with_name("none.jsonl")) == 0
app.cc.clear()
app.agent_event({"session_id": "c", "cwd": "/x/p", "hook_event_name": "Stop", "transcript_path": str(tr)}, "cc", os.getpid())
_t.sleep(0.2)
mine = lambda **kw: next(a for a in app.agents(**kw) if a["tool"] == "Claude Code")
assert mine(now=t0 + 600 + 60)["cache"] == 4 and mine(now=t0 + 2000)["cache"] == 0
app.agent_event({"session_id": "c", "hook_event_name": "UserPromptSubmit"}, "cc", os.getpid())
assert "cache" not in mine()  # working: warm by definition
print("ok")

# Claude sessions without hooks (the desktop app's) come from their transcripts; closed ones and hooked ones don't repeat
d = app.CLAUDE / "proj"; d.mkdir()
(d / "desk.jsonl").write_text(json.dumps({"type": "user", "cwd": "/x/proj", "entrypoint": "claude-desktop"}) + "\n"
                              + json.dumps({"type": "custom-title", "customTitle": "My chat"}) + "\n")
(d / "oneshot.jsonl").write_text(json.dumps({"type": "user", "cwd": "/x/p", "entrypoint": "sdk-cli"}) + "\n")
(d / "gone.jsonl").write_text(json.dumps({"type": "user", "cwd": "/x/old", "entrypoint": "cli"}) + "\n")
iso = lambda secs_ago: datetime.utcfromtimestamp(_t.time() - secs_ago).isoformat() + "Z"
(d / "opened.jsonl").write_text(json.dumps({"type": "user", "timestamp": iso(3 * 86400), "cwd": "/x/o", "entrypoint": "claude-desktop"}) + "\n"
                                + json.dumps({"type": "cost-state"}) + "\n")  # only opened today: fresh mtime, old activity
(d / "ten.jsonl").write_text(json.dumps({"type": "user", "timestamp": iso(600), "cwd": "/x/ten", "entrypoint": "claude-desktop"}) + "\n")
app.cc.clear(); app.cc_ended.add("gone")
assert [(a["where"], a["state"], a["mins"]) for a in app.agents() if a["tool"] != "Codex"] == [("My chat", "working", 0), ("ten", "idle", 10)]
assert [a["state"] for a in app.agents(now=_t.time() + 120) if a["tool"] == "Claude app"] == ["idle", "idle"]
print("ok")

# cache warning: once per cache period, only in the last 5 minutes
said.clear(); app.cc.clear(); app.warned.clear()
app.agent_event({"session_id": "w", "cwd": "/x/p", "hook_event_name": "Stop", "transcript_path": str(tr)}, "cc", os.getpid())
_t.sleep(0.2); said.clear()
exp = app.cc["w"]["cached_until"]
app.warn_caches(now=exp - 600); app.warn_caches(now=exp - 240); app.warn_caches(now=exp - 120); app.warn_caches(now=exp + 10)
assert said == [("Claude Code", "p: cache expires in 4 min")], said
print("ok")

# desktop sessions open by their local_… id, looked up from the desktop app's session files
app.DESKTOP = Path(tempfile.mkdtemp()); (app.DESKTOP / "u" / "o").mkdir(parents=True)
(app.DESKTOP / "u" / "o" / "local_abc.json").write_text(json.dumps({"sessionId": "local_abc", "cliSessionId": "desk",
                                                                   "title": "My chat (fork)"}))
app.desktop_at = 0
assert app.desktop_session("desk") == {"id": "local_abc", "title": "My chat (fork)"} and app.desktop_session("nope") == {}
assert [a["open"] for a in app.agents() if a["where"] == "My chat (fork)"] == ["claude://claude.ai/epitaxy/local_abc"]  # sidebar title wins
print("ok")
