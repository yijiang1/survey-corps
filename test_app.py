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
assert clean_scores('ok [{"i": 0, "score": 12, "why": "x"}, {"i": "1", "score": 3}, {"score": 5}, {"i": 2, "score": "?"}]') == {0: (10.0, "x"), 1: (3.0, "")}
assert clean_scores(None) == {} and clean_scores("[oops") == {}
assert clean_scores('[{"i": 0, "score": 7, "why": "a"}, {"i": 1, "score": 4, "wh') == {0: (7.0, "a")}  # truncated reply
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
assert app.clean_lib_suggestion('x {"summary": " A  tool ", "group": "projects", "labels": ["Py", "", "' + "x" * 30 + '"]}', ["Projects"]) == {
    "summary": "A tool", "group": "Projects", "labels": ["Py"]}
assert app.clean_lib_suggestion('{"group": "Made Up"}', ["Projects"])["group"] == "" and app.clean_lib_suggestion("nope", []) == {"summary": "", "group": "", "labels": []}
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
# Pets: routines parse and fire once a day; Argo's fill-ins can be undone; /api/pets sums it up
app.set_settings({"routines": "fri 17 task: what did I finish?\nweekdays 9 scout: top 3\nsun 25 task: bad hour\nnonsense"})
assert [(r["days"], r["hour"], r["pet"]) for r in app.routines()] == [("fri", 17, "task"), ("weekdays", 9, "scout")]
app.RAN = Path(tempfile.mkdtemp()) / "ran.json"
fri5 = datetime(2026, 10, 23, 17, 30)
assert [r["pet"] for r in app.due_routines(fri5)] == ["task", "scout"] and app.due_routines(fri5) == []  # once a day
assert app.due_routines(datetime(2026, 10, 24, 18)) == []                                              # saturday: neither
t = {**app.blank(), "title": "Review draft", "body": "", "labels": ["Review"], "due": "2026-10-23T17:00", "actions": ["a", "b"],
     "review": {"labels": ["Review"], "due": "2026-10-23T17:00", "title": "Review draft", "was": "review draft by fri",
                "body": "review draft by fri", "actions": ["a", "b"]}}
u = {**t, "labels": ["Mine"]}  # user changed the labels since: those stay
app.undo_review(t); app.undo_review(u)
assert (t["title"], t["labels"], t["due"], t["actions"], "review" in t) == ("review draft by fri", [], "", [], False), t
assert u["labels"] == ["Mine"] and u["due"] == ""
app.FEED = Path(tempfile.mkdtemp()) / "feed.json"
app.save([{**app.blank(), "title": "x", "due": "2020-01-01T00:00", "review": {"labels": ["L"]}}])
s = app.pets()
assert s["overdue"] == 1 and s["review"][0]["labels"] == ["L"] and s["routines"][0] == {"line": "fri 17 task: what did I finish?", "pet": "task"}
app.say("scout", "Scout", "2 new picks")
assert app.pets()["bubbles"][-1]["text"] == "2 new picks" and app.pets(after=app.bubbles[-1]["id"])["bubbles"] == []
print("ok")
