"""Ad lines taken out of episode descriptions, and chapter times moved to match the cut audio."""

from wren import notes

TEXT = """A great episode about bridges.

SPONSORS:
- Go to https://example.com/show for 20% off with code SHOW.
Learn more about your ad choices. Visit example.fm/adchoices

00:00 Intro
07:15 Bridges
1:02:03 Questions"""

HTML = ("<p>About <a href='x'>bridges</a> &amp; tunnels.</p><p>Sponsors:</p><ul><li>Acme: 10% off</li>"
        "<li>Widgets: code SHOW</li></ul><p>Guests:</p><ul><li>A. Engineer</li></ul>")


def test_lines_are_numbered_as_shown():
    assert notes.lines(TEXT)[:4] == ["A great episode about bridges.", "SPONSORS:",
                                     "- Go to https://example.com/show for 20% off with code SHOW.",
                                     "Learn more about your ad choices. Visit example.fm/adchoices"]
    assert notes.lines(HTML) == ["About bridges & tunnels.", "Sponsors:", "Acme: 10% off", "Widgets: code SHOW",
                                 "Guests:", "A. Engineer"]
    assert notes.numbered(HTML).splitlines()[1] == "2: Sponsors:"
    assert notes.lines(None) == [] and notes.numbered("") == ""


def test_marked_lines_are_removed_whole():
    cleaned, removed = notes.remove(TEXT, {2, 3, 4})
    assert cleaned == "A great episode about bridges.\n\n00:00 Intro\n07:15 Bridges\n1:02:03 Questions"
    assert removed[0] == "SPONSORS:" and len(removed) == 3
    cleaned, _ = notes.remove(HTML, {2, 3, 4})
    # The emptied list goes too; everything else is as it was, tags and all.
    assert cleaned == "<p>About <a href='x'>bridges</a> &amp; tunnels.</p><p>Guests:</p><ul><li>A. Engineer</li></ul>"


def test_chapter_times_move_back_by_the_cuts_before_them():
    cut = [(60.0, 90.0), (400.0, 415.0)]  # 30s, then 15s (one inside 7:15's minute)
    out = notes.retime(TEXT, cut)
    assert out.endswith("00:00 Intro\n06:30 Bridges\n1:01:18 Questions")
    assert notes.retime("Intro at 7:15 is great\n0:30 Start", cut) == "Intro at 7:15 is great\n0:30 Start"
    # A time inside a cut moves to where the cut was.
    assert notes.retime("1:10 Ad break", cut) == "1:00 Ad break"
    assert notes.retime("<li><b>00:01:30</b> Bridges</li>", cut) == "<li><b>00:01:00</b> Bridges</li>"
    assert notes.retime(TEXT, []) == TEXT
