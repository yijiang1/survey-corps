from datetime import datetime
from app import clean_scores, clean_suggestion, parse_arxiv
now = datetime(2026, 10, 17, 9, 0)
assert clean_suggestion('x {"labels": ["A", " B ", "", "C", "D"], "due": "2026-10-20T17:00"} y', now) == {"labels": ["A", "B", "C"], "due": "2026-10-20T17:00"}
assert clean_suggestion('{"labels": [], "due": "2026-10-01T17:00"}', now)["due"] == ""  # past
assert clean_suggestion('{"due": "next friday"}', now)["due"] == ""                      # malformed
assert clean_suggestion("no json", now) == {"labels": [], "due": ""}
assert clean_scores('ok [{"i": 0, "score": 12, "why": "x"}, {"i": "1", "score": 3}, {"score": 5}, {"i": 2, "score": "?"}]') == {0: (10.0, "x"), 1: (3.0, "")}
assert clean_scores(None) == {} and clean_scores("[oops") == {}
assert clean_scores('[{"i": 0, "score": 7, "why": "a"}, {"i": 1, "score": 4, "wh') == {0: (7.0, "a")}  # truncated reply
a = parse_arxiv(b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2610.01234v2</id>
<title>A  new\n ptychography</title><published>2026-10-15T00:00:00Z</published><summary> abc </summary>
<author><name>A</name></author><author><name>B</name></author><author><name>C</name></author><author><name>D</name></author></entry></feed>''')
assert a == [{"id": "arxiv:2610.01234", "src": "arXiv", "title": "A new ptychography", "url": "https://arxiv.org/abs/2610.01234",
              "date": "2026-10-15", "by": "A, B, C et al.", "text": "abc"}], a
print("ok")
