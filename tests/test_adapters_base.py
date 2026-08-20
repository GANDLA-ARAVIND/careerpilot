from adapters.base import strip_html

# ---------------------------------------------------------------------------
# strip_html and non-ASCII punctuation. A real bug was reported as "HTML
# stripping or unescaping is corrupting smart quotes and dashes." Traced to
# the actual root cause: it wasn't here. strip_html (and everything upstream
# of it - the HTTP fetch, JSON decoding, DB storage) was already correct;
# verified live against real stored data by checking the exact stored
# codepoint (0x2019), not by printing it, since printing was the actual bug.
# These tests lock in that this function is not, and was never, the
# corruption point.
# ---------------------------------------------------------------------------


def test_strip_html_preserves_raw_utf8_smart_quotes_and_dashes():
    html = "<p>Master’s degree required. 5–9 years – senior preferred — hybrid role.</p>"
    text = strip_html(html)
    assert "Master’s degree" in text
    assert "5–9 years" in text
    assert "– senior preferred" in text
    assert "— hybrid role" in text


def test_strip_html_preserves_html_entity_encoded_smart_quotes_and_dashes():
    """Some sources (Greenhouse) send these as HTML entities rather than raw
    UTF-8 characters - html.unescape must resolve them to the same
    codepoints, not mangle them."""
    html = "<p>Master&rsquo;s degree required. 5&ndash;9 years &mdash; hybrid role.</p>"
    text = strip_html(html)
    assert "Master’s degree" in text
    assert "5–9 years" in text
    assert "— hybrid role" in text


def test_strip_html_block_tags_still_become_newlines_around_non_ascii_text():
    """Confirms the block-tag-to-newline behavior (added for extraction.py's
    header detection) doesn't interact badly with adjacent non-ASCII text."""
    html = "<p>Master’s degree</p><h3>Requirements</h3><li>5–9 years</li>"
    text = strip_html(html)
    assert "Master’s degree" in text
    assert "Requirements" in text
    assert "5–9 years" in text
    assert "\n" in text
