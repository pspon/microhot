"""Small text, URL and time helpers. No model calls, no network."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import UTC, datetime
from html.parser import HTMLParser
from itertools import pairwise
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---- time ------------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)  # Python 3.11+ reads a trailing "Z"
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def hours_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


# ---- HTML to text ----------------------------------------------------------------------

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
    "blockquote", "section", "article", "header", "footer", "figure", "figcaption", "pre", "table",
}
_SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "form", "button"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    if "<" not in value and "&" not in value:
        return collapse_ws(value)
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # malformed markup: fall back to a crude strip
        text = re.sub(r"<[^>]+>", " ", value)
    return collapse_ws(html.unescape(text))


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


# ---- URLs and ids ----------------------------------------------------------------------

_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", "ref_url",
    "cmpid", "ncid", "ocid", "sr_share", "cmp", "_ga", "igshid", "yclid", "spm", "source",
    "rss", "feed", "utm",
}


def canonical_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = parts.netloc.lower()
    host = host.removeprefix("www.")
    if host.endswith((":443", ":80")):
        host = host.rsplit(":", 1)[0]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def item_id_for(url: str) -> str:
    return short_hash(canonical_url(url))


def normalize_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title).lower()
    title = re.sub(r"[\W_]+", " ", title)
    return collapse_ws(title)


def title_fingerprint(title: str) -> str:
    return short_hash(normalize_title(title), 10)


# ---- tokens for lexical similarity ------------------------------------------------------

_STOPWORDS = set(
    """a an the and or but if then of to in on for with at by from as is are was were be been being
    this that these those it its into over under about after before than via vs new how why what when
    who which will can could would should may might has have had do does did not no yes your our their
    his her they them we you i he she more most less up out so just also all any some one two three
    says said say amid ahead per s t""".split()
)
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def _stem(word: str) -> str:
    """Very light suffix stripping so 'releases', 'released' and 'release' meet."""
    if len(word) <= 4 or not word.isalpha():
        return word
    for suffix in ("ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    """Word unigrams + bigrams (Latin) and character bigrams (CJK)."""
    text = unicodedata.normalize("NFKC", text).lower()
    out: list[str] = []
    words: list[str] = []
    for raw in re.findall(r"[a-z0-9][a-z0-9.\-+]*[a-z0-9+]|[a-z0-9]|[㐀-鿿豈-﫿]+", text):
        if _CJK.match(raw):
            chars = list(raw)
            out.extend(a + b for a, b in pairwise(chars))
            if len(chars) == 1:
                out.append(chars[0])
            continue
        word = raw.strip(".-")
        if not word or word in _STOPWORDS:
            words.append("")
            continue
        if "-" in word:  # "1M-token" should also meet "1M tokens", "open-weight" meet "open weights"
            out.extend(_stem(part) for part in word.split("-") if len(part) >= 2 and part not in _STOPWORDS)
        word = _stem(word)
        words.append(word)
        out.append(word)
    out.extend(f"{a} {b}" for a, b in pairwise(words) if a and b)
    return out


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def cjk_ratio(text: str) -> float:
    letters = [c for c in text if not c.isspace()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _CJK.match(c)) / len(letters)


_URL = re.compile(r"https?://\S+")


def safe_error(exc: BaseException, limit: int = 200) -> str:
    """Exception text that is fine for public logs: HTTP errors give only the status code, other URLs are removed."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"{type(exc).__name__}: HTTP {status}"
    return _URL.sub("<url>", f"{type(exc).__name__}: {exc}")[:limit]
