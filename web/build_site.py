#!/usr/bin/env python3
"""Build a searchable static site of multi-echo fMRI posters from OHBM 2026.

Fetches all "Poster Abstract" timeslots from the OHBM conference AppSync GraphQL
API, filters them for multi-echo fMRI content (title + abstract), enriches the
matches with presentation day / keywords / categories, and generates a
self-contained ``index.html`` plus ``posters.json``.

The OHBM app exposes a public read-only API key. We are polite: small delay
between pages, bounded retries. Reverse-engineered facts driving this script:

* Posters are fetched by *type*, not parent: ``getTimeslotsWithType`` with the
  "Poster Abstract" type id ``68d10708d0ad57c507ab2483``.
* ``limit`` is silently capped to ~50/page, so the full set is ~65 pages.
* ``info`` is full HTML: ``<h2>NUMBER Title</h2>`` then the authors line ending
  in ``<br/>`` then affiliations then the abstract body. Authors are parsed from
  this HTML (avoids resolving thousands of person ids via ``roles``).
* ``start``/``end``/``schedule`` are null this far out; the presentation *day*
  lives in the Date classifier, resolved via ``batchGet`` over ``classifications``.

Usage::

    python build_site.py            # fetch (or reuse cache), filter, build
    python build_site.py --refetch  # force a fresh fetch from the API
    python build_site.py --no-fetch # use cache only (fast pattern-tuning loop)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# --- API constants (public read-only key) -----------------------------------
ENDPOINT = (
    "https://tm4ylzrgrfhkhdliqxxu3yljm4.appsync-api.eu-central-1.amazonaws.com/graphql"
)
API_KEY = "da2-wpf4mr4yofas7lvpxvajjbcpvq"
EVENT_ID = "68d10707d0ad57c507ab2426"
POSTER_TYPE_ID = "68d10708d0ad57c507ab2483"  # "Poster Abstract"
DETAIL_URL = (
    "https://ohbm.floq.live/event/ohbm-2026/posters-with-category"
    "?objectClass=timeslot&objectId={id}&type=detail"
)

HERE = Path(__file__).resolve().parent
RAW_CACHE = (
    HERE / "posters_raw.json.gz"
)  # generated cache; git-ignored (rebuild with --refetch)
RAW_CACHE_PLAIN = HERE / "posters_raw.json"  # legacy uncompressed cache, if present
POSTERS_JSON = HERE / "posters.json"
INDEX_HTML = HERE / "index.html"

# --- GraphQL queries ---------------------------------------------------------
Q_TIMESLOTS = """
query GetTimeslotsWithType($type: String!, $limit: Int, $nextToken: String) {
  getTimeslotsWithType(type: $type, limit: $limit, nextToken: $nextToken) {
    items {
      id name type deleted parent start end schedule posterPdf roles info
      classifications searchTerms locations subNameDetail webpages links
    }
    nextToken
  }
}
"""

Q_FINDTYPES = """
query FindTypes($event: String!, $filter: TableTypesFilterInput, $limit: Int) {
  findTypes(event: $event, filter: $filter, limit: $limit) {
    items { id singular plural target }
    nextToken
  }
}
"""

Q_BATCHGET = """
query BatchGet($data: [BatchInput]) {
  batchGet(data: $data) { classifiers { id name type } }
}
"""


# --- HTTP / GraphQL helper ---------------------------------------------------
def gql(query: str, variables: dict, *, retries: int = 4) -> dict:
    """POST a GraphQL query, returning ``data``. Retries transient failures."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(ENDPOINT, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.load(resp)
            if "errors" in payload:
                raise RuntimeError(f"GraphQL errors: {payload['errors']}")
            return payload["data"]
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as err:
            last_err = err
            wait = 1.5 * (attempt + 1)
            print(f"  ! request failed ({err}); retry in {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"Giving up after {retries} attempts: {last_err}")


# --- Fetch -------------------------------------------------------------------
def fetch_all_posters() -> list[dict]:
    """Paginate every Poster Abstract timeslot (the ``limit`` is capped ~50)."""
    items: list[dict] = []
    token: str | None = None
    page = 0
    while True:
        data = gql(
            Q_TIMESLOTS, {"type": POSTER_TYPE_ID, "limit": 1000, "nextToken": token}
        )
        node = data["getTimeslotsWithType"]
        batch = node["items"]
        items.extend(batch)
        page += 1
        print(f"  page {page:>2}: +{len(batch):<3} (total {len(items)})")
        token = node["nextToken"]
        if not token:
            break
        time.sleep(0.25)
    return items


def _read_cache() -> list[dict] | None:
    """Read the raw cache, preferring the gzipped file, falling back to plain."""
    if RAW_CACHE.exists():
        with gzip.open(RAW_CACHE, "rt", encoding="utf-8") as f:
            return json.load(f)
    if RAW_CACHE_PLAIN.exists():
        return json.loads(RAW_CACHE_PLAIN.read_text())
    return None


def load_or_fetch(mode: str) -> list[dict]:
    """Return raw poster items per CLI mode ('auto' | 'refetch' | 'no-fetch')."""
    if mode in ("no-fetch", "auto"):
        cached = _read_cache()
        if cached is not None:
            print(
                f"Using cached raw posters ({len(cached)}; pass --refetch to refresh)"
            )
            return cached
        if mode == "no-fetch":
            raise SystemExit(
                "--no-fetch given but no posters_raw.json[.gz] cache found."
            )
    print("Fetching all Poster Abstracts from the OHBM API…")
    items = fetch_all_posters()
    with gzip.open(RAW_CACHE, "wt", encoding="utf-8") as f:
        json.dump(items, f)
    print(f"Cached {len(items)} raw posters → {RAW_CACHE.name}")
    return items


# --- HTML / field parsing ----------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_SUP_RE = re.compile(r"<sup>.*?</sup>", re.S)
_WS_RE = re.compile(r"\s+")

# Oxford Abstracts upload fields for this event, both embedded as <img> in `info`:
# 127505 = abstract figures (shown inline); 127506 = the uploaded poster itself
# (always the last image), which we omit — the poster lives on the official app,
# reachable via each card's floq.live link.
POSTER_FILE_MARK = "/questions/127506/"


def strip_html(s: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def _kw_norm(s: str) -> str:
    """Lowercase, collapse non-alphanumerics to single spaces, and pad with
    spaces so a keyword matches only as a whole word/phrase, tolerant of
    case, hyphenation and punctuation differences."""
    return " " + re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() + " "


# Abstract HTML is injected via innerHTML, so it is sanitized to a strict
# allowlist. Regex sanitizing is bypassable (encoded `javascript:`, unexpected
# tags, attribute edge cases), so we parse and re-emit only these tags, drop all
# attributes except a vetted `href`/`src`, drop the poster image (127506), and
# route figures (127505) through the wsrv.nl proxy.
_ALLOWED_TAGS = {
    "p",
    "br",
    "h3",
    "h4",
    "h5",
    "em",
    "strong",
    "b",
    "i",
    "u",
    "sup",
    "sub",
    "ul",
    "ol",
    "li",
    "a",
    "img",
}
_VOID_TAGS = {"br", "img"}


def _proxy_figure(src: str) -> str:
    """An `<img>` for a figure, proxied/resized via wsrv.nl — or '' to drop it.

    Oxford Abstracts `/content/...` figure URLs hotlink unreliably (browser
    requests get 302-redirected to throttled signed URLs); wsrv.nl fetches them
    server-side and serves a cached, resized copy. The full poster (127506) is
    dropped (reachable via the official-app link); only figures (127505) remain.
    """
    if not src.startswith("https://") or POSTER_FILE_MARK in src:
        return ""
    proxied = (
        "https://wsrv.nl/?url="
        + urllib.parse.quote(src, safe="")
        + "&amp;w=1200&amp;output=jpg&amp;q=88"
    )
    return (
        f'<img src="{proxied}" loading="lazy" alt="Abstract figure"'
        ' role="button" tabindex="0" aria-label="Enlarge figure">'
    )


class _Sanitizer(HTMLParser):
    """Re-emit only an allowlist of tags/attributes from untrusted abstract HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            return
        a = dict(attrs)
        if tag == "img":
            self.out.append(_proxy_figure(a.get("src") or ""))
        elif tag == "a":
            href = a.get("href") or ""
            ok = href.startswith("https://")
            attr = (
                f' href="{html.escape(href, quote=True)}" target="_blank" rel="noopener nofollow"'
                if ok
                else ""
            )
            self.out.append(f"<a{attr}>")
        else:
            self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data, quote=False))


def sanitize_html(s: str) -> str:
    """Sanitize abstract HTML to a small tag allowlist before innerHTML use."""
    if not s:
        return ""
    parser = _Sanitizer()
    parser.feed(s)
    parser.close()
    return "".join(parser.out).strip()


def parse_authors(info: str) -> str:
    """Authors are the text between </h2> and the first <br/> (sans <sup>)."""
    if not info:
        return ""
    m = re.search(r"</h2>(.*?)<br\s*/?>", info, re.S)
    if not m:
        return ""
    return (
        _WS_RE.sub(" ", html.unescape(_SUP_RE.sub("", m.group(1)))).strip().strip(",")
    )


def parse_abstract_body(info: str) -> str:
    """Abstract body from the first <h3>/<p> onward, so title+authors don't repeat."""
    if not info:
        return ""
    body = re.sub(r"(?is)^\s*<h2>.*?</h2>", "", info, count=1)
    m = re.search(r"(?i)<(h3|h4|p)[\s>]", body)
    if m:
        body = body[m.start() :]
    return sanitize_html(body)


def parse_poster(item: dict) -> dict:
    name = (item.get("name") or "").strip()
    info = item.get("info") or ""
    m = re.match(r"^(\d{4})\b\s*(.*)", name, re.S)
    number, title = (m.group(1), m.group(2).strip()) if m else ("", name)
    pdf = item.get("posterPdf")
    pdf = pdf if isinstance(pdf, str) and pdf.startswith("http") else None
    return {
        "id": item["id"],
        "number": number,
        "title": title or name,
        "authors": parse_authors(info),
        "abstract_html": parse_abstract_body(info),
        "url": DETAIL_URL.format(id=item["id"]),
        "posterPdf": pdf,
        "_classifications": item.get("classifications") or "[]",
    }


# --- Multi-echo filtering ----------------------------------------------------
# Strong signals: any one match qualifies a poster.
# Unconditional signals — specific to multi-echo BOLD fMRI; any one qualifies.
ME_SPECIFIC = [
    ("tedana", re.compile(r"\btedana\b", re.I)),
    ("ME-ICA", re.compile(r"\bme-?ica\b", re.I)),
    ("ME-fMRI / ME-EPI", re.compile(r"\bME[\s\-]?(?:fMRI|EPI)\b", re.I)),
]
# fMRI/BOLD context. Bare "multi-echo" is a generic MRI acquisition term used
# heavily in QSM, relaxometry and MRS, so the ambiguous signals below only
# qualify a poster when genuine fMRI context is also present. (Note: we require
# the leading "f" in fmri and avoid bare T2*-weighted, since every BOLD poster
# says "T2*-weighted EPI" — that would re-introduce the noise we are removing.)
FMRI_CTX = re.compile(
    r"\bfmri\b|\bbold\b|resting[\s\-]?state|task[\s\-]?based"
    r"|functional\s+(?:mri|connectiv|activation|imaging|neuroimaging)"
    r"|hemodynamic|\brs-?fmri\b|default[\s\-]?mode\s+network|dual[\s\-]?regression",
    re.I,
)
FMRI_GATED = [
    ("multi-echo", re.compile(r"multi[\s\-]?echo|multiple\s+echoes", re.I)),
    (
        "echo combination",
        re.compile(r"(?:optimal(?:ly)?|echo(?:\s+time)?)\s+combin\w*", re.I),
    ),
    ("T2*/R2* mapping", re.compile(r"\b[TR]2\*[\s\-]?map\w*", re.I)),
    ("TE-dependent", re.compile(r"\bTE[\s\-]?dependen\w*", re.I)),
    ("S0 estimation", re.compile(r"\bS0\b", re.I)),
]


def multiecho_reasons(text: str) -> list[str]:
    """Return the multi-echo-fMRI signals matched in *text* (empty if none)."""
    reasons = [label for label, rx in ME_SPECIFIC if rx.search(text)]
    if FMRI_CTX.search(text):
        reasons += [label for label, rx in FMRI_GATED if rx.search(text)]
    return list(dict.fromkeys(reasons))


# --- Classifier enrichment ---------------------------------------------------
def type_label_map() -> dict[str, str]:
    data = gql(
        Q_FINDTYPES,
        {"event": EVENT_ID, "limit": 1000, "filter": {"deleted": {"eq": False}}},
    )
    return {t["id"]: (t.get("singular") or "") for t in data["findTypes"]["items"]}


def fetch_classifiers(ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        data = gql(Q_BATCHGET, {"data": [{"target": "classifiers", "ids": chunk}]})
        for c in data["batchGet"]["classifiers"]:
            out[c["id"]] = c
        time.sleep(0.2)
    return out


def enrich(matched: list[dict]) -> None:
    """Attach days / keywords / categories to matched posters (in place)."""
    all_ids: set[str] = set()
    for p in matched:
        for c in json.loads(p["_classifications"]):
            if c.get("_id"):
                all_ids.add(c["_id"])
    if not all_ids:
        for p in matched:
            p["days"], p["keywords"], p["categories"] = [], [], []
        return
    labels = type_label_map()
    classifiers = fetch_classifiers(sorted(all_ids))

    def label_of(type_id: str) -> str:
        return labels.get(type_id, "")

    for p in matched:
        days, keywords, categories = [], [], []
        for c in json.loads(p["_classifications"]):
            info = classifiers.get(c.get("_id"))
            if not info:
                continue
            lbl, name = label_of(info["type"]), info["name"]
            if lbl == "Date":
                days.append(name)
            elif lbl == "Keyword":
                keywords.append(name)
            elif lbl in ("Category", "Secondary Category", "Poster category"):
                categories.append(name)
        # de-dupe, preserve order
        p["days"] = list(dict.fromkeys(days))
        # Author keywords are taxonomy terms; only keep those that actually
        # occur in the abstract text (title + body), so the chips reflect content.
        abstract_text = _kw_norm(p["title"] + " " + strip_html(p["abstract_html"]))
        p["keywords"] = [
            k
            for k in dict.fromkeys(keywords)
            if _kw_norm(k).strip() and _kw_norm(k) in abstract_text
        ]
        p["categories"] = list(dict.fromkeys(categories))


# --- Output ------------------------------------------------------------------
def build_records(raw_items: list[dict]) -> list[dict]:
    matched: list[dict] = []
    for item in raw_items:
        if item.get("deleted"):
            continue
        name = item.get("name") or ""
        if "test poster" in name.lower():
            continue
        text = strip_html(name) + "  " + strip_html(item.get("info") or "")
        reasons = multiecho_reasons(text)
        if not reasons:
            continue
        rec = parse_poster(item)
        rec["reasons"] = reasons
        matched.append(rec)
    matched.sort(key=lambda r: (r["number"] or "9999", r["title"]))
    return matched


def write_json(matched: list[dict]) -> None:
    public = []
    for p in matched:
        q = {k: v for k, v in p.items() if not k.startswith("_")}
        public.append(q)
    POSTERS_JSON.write_text(json.dumps(public, indent=2, ensure_ascii=False))
    print(f"Wrote {POSTERS_JSON.name} ({len(public)} posters)")


def generate_html(matched: list[dict], total: int) -> None:
    public = [{k: v for k, v in p.items() if not k.startswith("_")} for p in matched]
    data_js = json.dumps(public, ensure_ascii=False).replace("</", "<\\/")
    today = _dt.date.today()
    generated = f"{today.day} {today:%B %Y}"  # e.g. "4 June 2026"
    page = (
        HTML_TEMPLATE.replace("{{COUNT}}", str(len(public)))
        .replace("{{TOTAL}}", f"{total:,}")
        .replace("{{GENERATED}}", generated)
        .replace("{{DATA}}", data_js)
    )
    INDEX_HTML.write_text(page)
    print(f"Wrote {INDEX_HTML.name} ({len(public)} posters)")


def print_summary(matched: list[dict]) -> None:
    print(f"\nMatched {len(matched)} multi-echo posters.")
    counts: dict[str, int] = {}
    for p in matched:
        for r in p["reasons"]:
            counts[r] = counts.get(r, 0) + 1
    print("Why-matched breakdown (review these — tune patterns + rerun --no-fetch):")
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3}  {label}")


# --- HTML template (self-contained; data inlined) ----------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-echo fMRI posters · OHBM 2026</title>
<meta name="description" content="An unofficial community index of every multi-echo fMRI poster at OHBM 2026, Bordeaux. Search, filter by day and method, and read abstracts.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

/* ============================================================
   Tokens — Eneko Uruñuela design system (editorial minimalism)
   ============================================================ */
:root {
  --c-bg:           #EDEAE6;
  --c-fg:           #1A1A1A;
  --c-surface-dark: #2B2B2B;
  --c-fg-on-dark:   #F5F2EE;

  /* accent is set at runtime (default amber) */
  --accent:         #D4652A;
  --accent-hover:   #B45722;
  --accent-press:   #9C4B1D;

  --bg:          var(--c-bg);
  --surface:     #F4F1ED;          /* very slightly raised panels */
  --fg:          var(--c-fg);
  --fg-muted:    oklch(0% 0 0 / 0.60);
  --fg-subtle:   oklch(0% 0 0 / 0.40);
  --rule:        oklch(0% 0 0 / 0.12);
  --rule-strong: oklch(0% 0 0 / 0.26);
  --chip-bg:     oklch(0% 0 0 / 0.045);
  --accent-tint: color-mix(in srgb, var(--accent) 12%, transparent);

  --font-serif: 'DM Serif Display', Georgia, serif;
  --font-sans:  'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono:  'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;

  --col: 940px;

  --ease:     cubic-bezier(0.2, 0.6, 0.2, 1);
  --dur-fast: 120ms;
  --dur:      200ms;
}

html[data-theme="dark"] {
  --bg:          var(--c-surface-dark);
  --surface:     #313131;
  --fg:          var(--c-fg-on-dark);
  --fg-muted:    oklch(100% 0 0 / 0.64);
  --fg-subtle:   oklch(100% 0 0 / 0.42);
  --rule:        oklch(100% 0 0 / 0.14);
  --rule-strong: oklch(100% 0 0 / 0.30);
  --chip-bg:     oklch(100% 0 0 / 0.06);
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: 17px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

::selection { background: color-mix(in srgb, var(--accent) 28%, var(--bg)); color: var(--fg); }

a { color: inherit; text-decoration: none; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 1px; }

.eyebrow {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.10em;
  color: var(--fg-muted);
  margin: 0;
}

.shell { max-width: var(--col); margin: 0 auto; padding: 0 24px; }

/* ============================================================
   Masthead — quiet, compact
   ============================================================ */
.masthead { padding: 40px 0 26px; }
.masthead__top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}
.masthead h1 {
  font-family: var(--font-serif);
  font-weight: 400;
  font-size: clamp(34px, 5vw, 52px);
  line-height: 1.05;
  letter-spacing: -0.015em;
  margin: 14px 0 0;
  text-wrap: balance;
}
.lede {
  margin: 14px 0 0;
  max-width: 60ch;
  color: var(--fg-muted);
  font-size: 17px;
  text-wrap: pretty;
}
.stat {
  margin: 18px 0 0;
  font-family: var(--font-mono);
  font-size: 12.5px;
  letter-spacing: 0.02em;
  color: var(--fg-muted);
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 14px;
}
.stat b { color: var(--fg); font-weight: 600; }
.stat .stat__count { color: var(--accent); font-weight: 600; }
.stat .dot { color: var(--fg-subtle); }

/* Display control (theme + accent) */
.display { position: relative; flex: 0 0 auto; }
.icon-btn {
  appearance: none;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  border: 1px solid var(--rule-strong);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 9px 12px;
  border-radius: 2px;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.icon-btn:hover { border-color: var(--accent); color: var(--accent); }
.icon-btn svg { width: 15px; height: 15px; }

.panel {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 248px;
  background: var(--surface);
  border: 1px solid var(--rule-strong);
  border-radius: 2px;
  padding: 18px;
  z-index: 40;
  display: none;
}
.panel.open { display: block; }
.panel__label {
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--fg-muted);
  margin: 0 0 9px;
}
.panel__group + .panel__group { margin-top: 20px; }

.segment { display: flex; border: 1px solid var(--rule-strong); border-radius: 2px; overflow: hidden; }
.segment button {
  flex: 1;
  appearance: none;
  background: transparent;
  border: 0;
  border-left: 1px solid var(--rule-strong);
  color: var(--fg-muted);
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 8px 4px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.segment button:first-child { border-left: 0; }
.segment button svg { width: 14px; height: 14px; }
.segment button:hover { color: var(--fg); }
.segment button[aria-pressed="true"] { background: var(--accent); color: var(--c-fg-on-dark); }

.swatches { display: flex; gap: 10px; }
.swatch {
  width: 28px; height: 28px;
  padding: 0;
  border-radius: 999px;
  border: 1px solid var(--rule-strong);
  cursor: pointer;
  position: relative;
  transition: transform var(--dur-fast) var(--ease);
}
.swatch:hover { transform: scale(1.08); }
.swatch[aria-pressed="true"] {
  box-shadow: 0 0 0 2px var(--bg), 0 0 0 3.5px var(--fg);
}

/* ============================================================
   Toolbar — sticky search + filters
   ============================================================ */
.toolbar {
  position: sticky;
  top: 0;
  z-index: 30;
  background: var(--bg);
  border-top: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
}
.toolbar__row {
  display: flex;
  gap: 14px;
  align-items: center;
  padding: 16px 0 14px;
}
.search {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--rule-strong);
  border-radius: 2px;
  padding: 0 12px;
  background: var(--surface);
  transition: border-color var(--dur-fast) var(--ease);
}
.search:focus-within { border-color: var(--accent); }
.search svg { width: 17px; height: 17px; color: var(--fg-subtle); flex: 0 0 auto; }
.search input {
  flex: 1;
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: 16px;
  padding: 11px 0;
  outline: none;
}
.search input::placeholder { color: var(--fg-subtle); }
.search__clear {
  appearance: none; background: transparent; border: 0; cursor: pointer;
  color: var(--fg-subtle); font-family: var(--font-mono); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.06em; padding: 4px;
  display: none;
}
.search__clear:hover { color: var(--accent); }
.search.has-value .search__clear { display: inline; }

.sort {
  flex: 0 0 auto;
  position: relative;
  display: flex;
  align-items: center;
}
.sort select {
  appearance: none;
  background: transparent;
  border: 1px solid var(--rule-strong);
  border-radius: 2px;
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 11px 30px 11px 12px;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease);
}
.sort select:hover { border-color: var(--accent); }
.sort svg { position: absolute; right: 10px; width: 14px; height: 14px; pointer-events: none; color: var(--fg-muted); }

.tool-btn {
  flex: 0 0 auto;
  appearance: none;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: transparent;
  border: 1px solid var(--rule-strong);
  border-radius: 2px;
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 11px 13px;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.tool-btn:hover { border-color: var(--accent); color: var(--accent); }
.tool-btn svg { width: 14px; height: 14px; }
.tool-btn[aria-expanded="true"] { border-color: var(--accent); color: var(--accent); }
.tool-btn__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--accent);
  color: var(--c-fg-on-dark);
  font-size: 10px;
  line-height: 1;
}
/* The class sets display, which would override the `hidden` attribute's
   UA display:none — restore it so the badge truly hides at 0 filters. */
.tool-btn__badge[hidden] { display: none; }

.filters {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 0 2px;
  margin-top: 14px;
  border-top: 1px solid var(--rule);
}
.filters[hidden] { display: none; }
.filter-line { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.filter-line__label {
  flex: 0 0 auto;
  width: 58px;
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--fg-subtle);
  padding-top: 2px;
}
.chips { display: flex; flex-wrap: wrap; gap: 7px; }
.chip {
  appearance: none;
  background: transparent;
  border: 1px solid var(--rule);
  border-radius: 999px;
  color: var(--fg-muted);
  font-family: var(--font-mono);
  font-size: 11.5px;
  letter-spacing: 0.02em;
  padding: 5px 12px;
  cursor: pointer;
  white-space: nowrap;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.chip:hover { border-color: var(--accent); color: var(--fg); }
.chip[aria-pressed="true"] {
  border-color: var(--accent);
  background: var(--accent-tint);
  color: var(--accent);
}
.chip .chip__n { opacity: 0.55; margin-left: 5px; }

.result-line {
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--fg-muted);
  padding: 12px 0 16px;
}
.result-line .reset {
  cursor: pointer; color: var(--fg-subtle); margin-left: 12px;
  border-bottom: 1px solid transparent;
}
.result-line .reset:hover { color: var(--accent); border-bottom-color: var(--accent); }

/* ============================================================
   List + poster cards — separated by space + hairline
   ============================================================ */
.list { padding: 8px 0 40px; }
.poster {
  padding: 30px 0;
  border-top: 1px solid var(--rule);
}
.poster:first-child { border-top: 0; }

.poster__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.poster__num {
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--fg);
}
.poster__why {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.03em;
  color: var(--accent);
  text-align: right;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.poster__why .why-sep { color: var(--accent); opacity: 0.5; }

.poster__title {
  font-family: var(--font-serif);
  font-weight: 400;
  font-size: clamp(21px, 2.6vw, 27px);
  line-height: 1.22;
  letter-spacing: -0.01em;
  margin: 0 0 8px;
  text-wrap: pretty;
  max-width: 52ch;
}
.poster__authors {
  margin: 0;
  color: var(--fg-muted);
  font-size: 15px;
  line-height: 1.5;
  max-width: 70ch;
}
.poster__authors .lead-author { color: var(--accent); font-weight: 600; }
.poster__tags {
  margin: 12px 0 0;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.03em;
  color: var(--fg-subtle);
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
}
.poster__tags .tag-sep { opacity: 0.5; }
.poster__tags .day { color: var(--fg-muted); }

.poster__actions { margin: 16px 0 0; display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: center; }
.act {
  appearance: none;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: transparent;
  border: 1px solid var(--rule-strong);
  border-radius: 2px;
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: 13.5px;
  font-weight: 500;
  line-height: 1.55;
  padding: 8px 13px;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.act:hover { border-color: var(--accent); color: var(--accent); }
.act svg { width: 15px; height: 15px; transition: transform var(--dur) var(--ease); }
.act.is-open svg.chev { transform: rotate(180deg); }
.act--ghost { border-color: transparent; padding-left: 0; }
.act--ghost:hover { border-color: transparent; }

.abstract {
  display: none;
  margin: 22px 0 4px;
  padding: 24px 28px;
  background: var(--surface);
  border-left: 3px solid var(--accent);
}
.abstract.open { display: block; }
.abstract { max-width: 760px; }
.abstract h2, .abstract h3, .abstract h4 {
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--accent);
  margin: 26px 0 8px;
  line-height: 1.4;
}
.abstract > h2:first-child, .abstract > h3:first-child { margin-top: 0; }
.abstract p { margin: 0 0 12px; font-size: 15.5px; line-height: 1.62; color: var(--fg); }
.abstract em { font-style: italic; }
.abstract sup, .abstract sub { font-size: 0.7em; }
.abstract ol, .abstract ul { margin: 0 0 12px; padding-left: 22px; font-size: 14px; color: var(--fg-muted); line-height: 1.55; }
.abstract li { margin: 0 0 6px; }
.abstract img {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 18px 0;
  border: 1px solid var(--rule);
  background: #fff;
  cursor: zoom-in;
}

/* Tap a figure to view it full-screen */
.lightbox {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: oklch(0% 0 0 / 0.9);
  cursor: zoom-out;
}
.lightbox.open { display: flex; }
.lightbox img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  background: #fff;
  box-shadow: 0 10px 60px oklch(0% 0 0 / 0.6);
}
.lightbox__close {
  position: absolute;
  top: 14px;
  right: 14px;
  appearance: none;
  border: 0;
  cursor: pointer;
  color: #fff;
  background: oklch(0% 0 0 / 0.7);
  padding: 7px 12px;
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  -webkit-backdrop-filter: blur(4px);
  backdrop-filter: blur(4px);
}

.empty {
  padding: 64px 0;
  text-align: center;
  color: var(--fg-muted);
}
.empty .serif { font-family: var(--font-serif); font-size: 26px; color: var(--fg); display: block; margin-bottom: 8px; }

/* ============================================================
   Footer
   ============================================================ */
.footer {
  border-top: 1px solid var(--rule);
  padding: 28px 0 48px;
  color: var(--fg-subtle);
  font-size: 13px;
  line-height: 1.6;
}
.footer a { border-bottom: 1.5px solid transparent; transition: border-color var(--dur-fast) var(--ease); }
.footer a:hover { border-bottom-color: var(--accent); }

@media (max-width: 720px) {
  .masthead { padding: 28px 0 20px; }
  .toolbar__row { flex-wrap: wrap; }
  .sort { order: 3; }
  .filter-line__label { width: 100%; }
  .poster__head { flex-direction: column; gap: 4px; }
  .poster__why { text-align: left; justify-content: flex-start; }
  .abstract { padding: 18px 16px; }
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; }
}
</style>
</head>
<body>
<header class="masthead shell">
  <div class="masthead__top">
    <div>
      <p class="eyebrow">OHBM 2026 · Organization for Human Brain Mapping · Bordeaux, France</p>
      <h1>Multi-echo fMRI posters</h1>
    </div>
    <div class="display">
      <button class="icon-btn" id="displayBtn" aria-haspopup="true" aria-expanded="false" aria-label="Display settings">
        <i data-lucide="sliders-horizontal"></i><span>Display</span>
      </button>
      <div class="panel" id="displayPanel" role="dialog" aria-label="Display settings">
        <div class="panel__group">
          <p class="panel__label">Theme</p>
          <div class="segment" id="themeSeg" role="group" aria-label="Theme">
            <button data-theme-mode="auto" aria-pressed="false"><i data-lucide="monitor"></i>Auto</button>
            <button data-theme-mode="light" aria-pressed="false"><i data-lucide="sun"></i>Light</button>
            <button data-theme-mode="dark" aria-pressed="false"><i data-lucide="moon"></i>Dark</button>
          </div>
        </div>
        <div class="panel__group">
          <p class="panel__label">Accent</p>
          <div class="swatches" id="swatches" role="group" aria-label="Accent color"></div>
        </div>
      </div>
    </div>
  </div>
  <p class="lede">An unofficial community index of every multi-echo poster at the 2026 Annual Meeting. Search by title, author or keyword, filter by session day and method, and read the abstracts in place.</p>
  <p class="stat">
    <span><span class="stat__count" id="totalCount">{{COUNT}}</span> multi-echo posters</span>
    <span class="dot">·</span>
    <span>of {{TOTAL}} total</span>
    <span class="dot">·</span>
    <span><b>14–18 June 2026</b></span>
  </p>
</header>

<div class="toolbar">
  <div class="shell">
    <div class="toolbar__row">
      <div class="search" id="searchWrap">
        <i data-lucide="search"></i>
        <input id="search" type="search" placeholder="Search title, author, or keyword…" autocomplete="off" aria-label="Search posters">
        <button class="search__clear" id="searchClear" aria-label="Clear search">Clear</button>
      </div>
      <button class="tool-btn" id="filterToggle" aria-expanded="false" aria-controls="filterPanel">
        <i data-lucide="sliders-horizontal"></i><span>Filters</span><span class="tool-btn__badge" id="filterBadge" hidden>0</span>
      </button>
      <div class="sort">
        <select id="sort" aria-label="Sort posters">
          <option value="number">Sort · poster №</option>
          <option value="title">Sort · title A–Z</option>
        </select>
        <i data-lucide="chevron-down"></i>
      </div>
    </div>
    <div class="filters" id="filterPanel" hidden>
      <div class="filter-line">
        <span class="filter-line__label">Day</span>
        <div class="chips" id="dayChips"></div>
      </div>
      <div class="filter-line">
        <span class="filter-line__label">Method</span>
        <div class="chips" id="reasonChips"></div>
      </div>
    </div>
    <p class="result-line" id="resultLine"></p>
  </div>
</div>

<main class="list shell" id="list"></main>

<footer class="footer">
  <div class="shell">
    Unofficial community index · data from the OHBM 2026 conference app · generated {{GENERATED}} · not affiliated with OHBM.
    Use the official app link on a card for the authoritative schedule.
  </div>
</footer>

<script src="https://unpkg.com/lucide@0.460.0/dist/umd/lucide.min.js"></script>
<script>
const POSTERS = {{DATA}};

/* ---------- Accent palette (warm, colormap-harmonious) ----------
   Each accent carries a deeper shade for light mode and a lighter
   shade for dark mode, both tuned to ~4.5:1+ text contrast (WCAG AA)
   against the warm-gray / charcoal backgrounds. `swatch` is the vivid
   hue shown in the picker. */
const ACCENTS = [
  { id: 'amber',  name: 'Amber (signature)', swatch: '#C25D24',
    light: { base: '#9F4E1D', hover: '#883F15', press: '#722F0E' },
    dark:  { base: '#E08A4A', hover: '#E89A5E', press: '#D27C3C' } },
  { id: 'magma',  name: 'Magma rose', swatch: '#B5476B',
    light: { base: '#A93E61', hover: '#933353', press: '#7D2945' },
    dark:  { base: '#DE87A4', hover: '#E699B2', press: '#CE7794' } },
  { id: 'plasma', name: 'Plasma violet', swatch: '#7B4DA6',
    light: { base: '#74479E', hover: '#623A87', press: '#522F72' },
    dark:  { base: '#AE8FD6', hover: '#BCA0E0', press: '#9E7FCA' } },
  { id: 'slate',  name: 'Slate blue', swatch: '#4A6580',
    light: { base: '#46627E', hover: '#3A5269', press: '#2F4456' },
    dark:  { base: '#88A3BF', hover: '#9AB2CB', press: '#7894B2' } },
  { id: 'forest', name: 'Forest', swatch: '#2F6E57',
    light: { base: '#2F6E57', hover: '#285E4A', press: '#214F3E' },
    dark:  { base: '#6FB196', hover: '#84BEA6', press: '#5EA286' } },
];

const LS_THEME = 'me-posters-theme';
const LS_ACCENT = 'me-posters-accent';

/* Persist preferences across sessions. Wrapped so a storage-blocked
   environment (private mode, disabled cookies) degrades gracefully
   instead of throwing and breaking the page. */
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } }

/* ---------- Theme ---------- */
const root = document.documentElement;
const mql = window.matchMedia('(prefers-color-scheme: dark)');

function resolveTheme(mode) {
  if (mode === 'dark') return 'dark';
  if (mode === 'light') return 'light';
  return mql.matches ? 'dark' : 'light';
}
function applyTheme() {
  const mode = lsGet(LS_THEME) || 'auto';
  root.setAttribute('data-theme', resolveTheme(mode));
  document.querySelectorAll('#themeSeg button').forEach(b => {
    b.setAttribute('aria-pressed', String(b.dataset.themeMode === mode));
  });
  applyAccent();
}
mql.addEventListener('change', () => {
  if ((lsGet(LS_THEME) || 'auto') === 'auto') applyTheme();
});

/* ---------- Accent ---------- */
function applyAccent() {
  const id = lsGet(LS_ACCENT) || 'amber';
  const a = ACCENTS.find(x => x.id === id) || ACCENTS[0];
  const shade = root.getAttribute('data-theme') === 'dark' ? a.dark : a.light;
  root.style.setProperty('--accent', shade.base);
  root.style.setProperty('--accent-hover', shade.hover);
  root.style.setProperty('--accent-press', shade.press);
  document.querySelectorAll('#swatches .swatch').forEach(s => {
    s.setAttribute('aria-pressed', String(s.dataset.accent === a.id));
  });
}

/* ---------- Build display panel controls ---------- */
function buildSwatches() {
  const wrap = document.getElementById('swatches');
  wrap.innerHTML = '';
  ACCENTS.forEach(a => {
    const b = document.createElement('button');
    b.className = 'swatch';
    b.dataset.accent = a.id;
    b.style.background = a.swatch;
    b.title = a.name;
    b.setAttribute('aria-label', a.name);
    b.addEventListener('click', () => { lsSet(LS_ACCENT, a.id); applyAccent(); });
    wrap.appendChild(b);
  });
}
document.getElementById('themeSeg').addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  lsSet(LS_THEME, btn.dataset.themeMode);
  applyTheme();
});
const displayBtn = document.getElementById('displayBtn');
const displayPanel = document.getElementById('displayPanel');
displayBtn.addEventListener('click', e => {
  e.stopPropagation();
  const open = displayPanel.classList.toggle('open');
  displayBtn.setAttribute('aria-expanded', String(open));
});
document.addEventListener('click', e => {
  if (!displayPanel.contains(e.target) && e.target !== displayBtn) {
    displayPanel.classList.remove('open');
    displayBtn.setAttribute('aria-expanded', 'false');
  }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { displayPanel.classList.remove('open'); displayBtn.setAttribute('aria-expanded', 'false'); }
});

/* ---------- Helpers ---------- */
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

/* Render an authors string with the first (presenting) author in the accent color. */
function authorsHtml(authors) {
  const parts = String(authors).split(',');
  const first = parts.shift().trim();
  let html = '<span class="lead-author">' + esc(first) + '</span>';
  if (parts.length) html += esc(', ' + parts.join(',').replace(/^\s+/, ''));
  return html;
}

const WD = { Monday: 'Mon', Tuesday: 'Tue', Wednesday: 'Wed', Thursday: 'Thu', Friday: 'Fri', Saturday: 'Sat', Sunday: 'Sun' };
function dayShort(d) {
  // "Monday, June 15" -> "Mon 15"
  const m = /^(\w+),\s+\w+\s+(\d+)/.exec(d || '');
  if (!m) return d;
  return (WD[m[1]] || m[1].slice(0, 3)) + ' ' + m[2];
}
function dayNum(d) { const m = /(\d+)\s*$/.exec(d || ''); return m ? parseInt(m[1], 10) : 999; }

/* ---------- Derive filter facets ---------- */
const dayOrder = [...new Set(POSTERS.flatMap(p => p.days || []))].sort((a, b) => dayNum(a) - dayNum(b));
const reasonCounts = {};
POSTERS.forEach(p => (p.reasons || []).forEach(r => { reasonCounts[r] = (reasonCounts[r] || 0) + 1; }));
const reasonOrder = Object.keys(reasonCounts).sort((a, b) => reasonCounts[b] - reasonCounts[a] || a.localeCompare(b));

const state = { q: '', days: new Set(), reasons: new Set(), sort: 'number' };

function buildFilterChips() {
  const dayWrap = document.getElementById('dayChips');
  dayOrder.forEach(d => {
    const b = document.createElement('button');
    b.className = 'chip';
    b.dataset.day = d;
    b.setAttribute('aria-pressed', 'false');
    b.textContent = dayShort(d);
    b.addEventListener('click', () => { toggleSet(state.days, d); syncChips(); render(); });
    dayWrap.appendChild(b);
  });
  const rWrap = document.getElementById('reasonChips');
  reasonOrder.forEach(r => {
    const b = document.createElement('button');
    b.className = 'chip';
    b.dataset.reason = r;
    b.setAttribute('aria-pressed', 'false');
    b.innerHTML = esc(r) + '<span class="chip__n">' + reasonCounts[r] + '</span>';
    b.addEventListener('click', () => { toggleSet(state.reasons, r); syncChips(); render(); });
    rWrap.appendChild(b);
  });
}
function toggleSet(set, v) { set.has(v) ? set.delete(v) : set.add(v); }
function syncChips() {
  document.querySelectorAll('#dayChips .chip').forEach(c => c.setAttribute('aria-pressed', String(state.days.has(c.dataset.day))));
  document.querySelectorAll('#reasonChips .chip').forEach(c => c.setAttribute('aria-pressed', String(state.reasons.has(c.dataset.reason))));
}

/* ---------- Card ---------- */
function haystack(p) {
  return [p.number, p.title, p.authors, (p.keywords || []).join(' '),
          (p.categories || []).join(' '), (p.reasons || []).join(' ')]
         .filter(Boolean).join(' ').toLowerCase();
}

function card(p) {
  const el = document.createElement('article');
  el.className = 'poster';

  const why = (p.reasons || []).map(r =>
    '<span class="why-item">' + esc(r) + '</span>'
  ).join('<span class="why-sep">·</span>');

  const days = (p.days || []).map(d => '<span class="day">' + esc(dayShort(d)) + '</span>')
    .join('<span class="tag-sep">·</span>');
  const kws = (p.keywords || []).filter(k => k && k.toLowerCase() !== 'other')
    .map(k => '<span>' + esc(k) + '</span>').join('<span class="tag-sep">·</span>');
  const tagBits = [days, kws].filter(Boolean).join('<span class="tag-sep" style="opacity:.35">/</span>');

  const links = [];
  if (p.url) links.push('<a class="act" href="' + esc(p.url) + '" target="_blank" rel="noopener">Official app <i data-lucide="external-link"></i></a>');

  const hasAbstract = !!(p.abstract_html && p.abstract_html.trim());
  const toggle = hasAbstract
    ? '<button class="act toggle" type="button">Read abstract <i data-lucide="chevron-down" class="chev"></i></button>'
    : '';
  const abstract = hasAbstract
    ? '<div class="abstract">' + p.abstract_html + '</div>'
    : '';

  el.innerHTML =
    '<div class="poster__head">' +
      (p.number ? '<span class="poster__num">№ ' + esc(p.number) + '</span>' : '<span></span>') +
      (why ? '<span class="poster__why">' + why + '</span>' : '') +
    '</div>' +
    '<h2 class="poster__title">' + esc(p.title) + '</h2>' +
    (p.authors ? '<p class="poster__authors">' + authorsHtml(p.authors) + '</p>' : '') +
    (tagBits ? '<p class="poster__tags">' + tagBits + '</p>' : '') +
    '<div class="poster__actions">' + toggle + links.join('') + '</div>' +
    abstract;

  const t = el.querySelector('.toggle');
  if (t) t.addEventListener('click', () => {
    const a = el.querySelector('.abstract');
    const open = a.classList.toggle('open');
    t.classList.toggle('is-open', open);
    t.childNodes[0].nodeValue = open ? 'Hide abstract ' : 'Read abstract ';
  });
  return el;
}

/* ---------- Render ---------- */
const list = document.getElementById('list');
const resultLine = document.getElementById('resultLine');

function render() {
  const terms = state.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  let shown = POSTERS.filter(p => {
    const h = haystack(p);
    if (!terms.every(t => h.includes(t))) return false;
    if (state.days.size && !(p.days || []).some(d => state.days.has(d))) return false;
    if (state.reasons.size && !(p.reasons || []).some(r => state.reasons.has(r))) return false;
    return true;
  });

  shown.sort((a, b) => {
    if (state.sort === 'title') return (a.title || '').localeCompare(b.title || '');
    return (parseInt(a.number, 10) || 0) - (parseInt(b.number, 10) || 0);
  });

  list.innerHTML = '';
  if (!shown.length) {
    list.innerHTML = '<div class="empty"><span class="serif">Nothing matches.</span>Try removing a filter or broadening your search.</div>';
  } else {
    const frag = document.createDocumentFragment();
    shown.forEach(p => frag.appendChild(card(p)));
    list.appendChild(frag);
  }

  const activeFilters = state.days.size + state.reasons.size + (terms.length ? 1 : 0);
  resultLine.innerHTML = 'Showing ' + shown.length + ' of ' + POSTERS.length +
    (activeFilters ? ' <span class="reset" id="resetBtn">Clear all</span>' : '');
  const rb = document.getElementById('resetBtn');
  if (rb) rb.addEventListener('click', resetAll);

  updateFilterBadge();
  if (window.lucide) lucide.createIcons();
}

function updateFilterBadge() {
  const n = state.days.size + state.reasons.size;
  const badge = document.getElementById('filterBadge');
  badge.textContent = n;
  badge.hidden = n === 0;
}

function resetAll() {
  state.q = ''; state.days.clear(); state.reasons.clear();
  const s = document.getElementById('search'); s.value = '';
  document.getElementById('searchWrap').classList.remove('has-value');
  syncChips();
  render();
}

/* ---------- Wire search + sort ---------- */
const searchInput = document.getElementById('search');
const searchWrap = document.getElementById('searchWrap');
searchInput.addEventListener('input', e => {
  state.q = e.target.value;
  searchWrap.classList.toggle('has-value', !!e.target.value);
  render();
});
document.getElementById('searchClear').addEventListener('click', () => {
  state.q = ''; searchInput.value = ''; searchWrap.classList.remove('has-value'); searchInput.focus(); render();
});
document.getElementById('sort').addEventListener('change', e => { state.sort = e.target.value; render(); });

/* ---------- Filters toggle ---------- */
const filterToggle = document.getElementById('filterToggle');
const filterPanel = document.getElementById('filterPanel');
filterToggle.addEventListener('click', () => {
  const open = filterPanel.hasAttribute('hidden');
  if (open) filterPanel.removeAttribute('hidden'); else filterPanel.setAttribute('hidden', '');
  filterToggle.setAttribute('aria-expanded', String(open));
});

/* Hide broken abstract figures (withdrawn submissions etc.) */
list.addEventListener('error', e => {
  if (e.target.tagName === 'IMG') e.target.style.display = 'none';
}, true);

/* ---------- Figure lightbox (tap a figure to view it full-screen) ---------- */
const lightbox = document.createElement('div');
lightbox.className = 'lightbox';
lightbox.setAttribute('role', 'dialog');
lightbox.setAttribute('aria-modal', 'true');
lightbox.setAttribute('aria-label', 'Figure, full screen');
const lightboxClose = document.createElement('button');
lightboxClose.type = 'button';
lightboxClose.className = 'lightbox__close';
lightboxClose.textContent = 'Esc / tap to close';
const lightboxImg = document.createElement('img');
lightboxImg.alt = 'Poster figure, full size';
lightbox.append(lightboxClose, lightboxImg);
document.body.appendChild(lightbox);
let lightboxOpener = null;
function closeLightbox() {
  if (!lightbox.classList.contains('open')) return;
  lightbox.classList.remove('open');
  document.body.style.overflow = '';
  if (lightboxOpener) lightboxOpener.focus();             // return focus to the figure
}
function openLightbox(img) {
  lightboxOpener = img;
  const cur = img.currentSrc || img.src;
  lightboxImg.src = cur;                                  // show the loaded image instantly
  const hi = cur.replace(/([?&])w=\d+/, '$1w=2000').replace(/([?&])q=\d+/, '$1q=92');
  if (hi !== cur) {                                       // then upgrade to a sharper version
    const pre = new Image();
    pre.onload = () => { if (lightbox.classList.contains('open')) lightboxImg.src = hi; };
    pre.src = hi;
  }
  lightbox.classList.add('open');
  document.body.style.overflow = 'hidden';
  lightboxClose.focus();                                  // move focus into the dialog
}
lightbox.addEventListener('click', closeLightbox);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox(); });
list.addEventListener('click', e => {
  const img = e.target.closest('.abstract img');
  if (img) openLightbox(img);
});
list.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('.abstract img')) {
    e.preventDefault();
    openLightbox(e.target);
  }
});

/* ---------- Init ---------- */
document.getElementById('totalCount').textContent = POSTERS.length;
buildSwatches();
buildFilterChips();
applyTheme();
render();
if (window.lucide) lucide.createIcons();
</script>
</body>
</html>
"""


# --- Main --------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--refetch", action="store_true", help="force a fresh API fetch")
    g.add_argument(
        "--no-fetch", action="store_true", help="use cache only (fast re-filter)"
    )
    args = ap.parse_args()
    mode = "refetch" if args.refetch else "no-fetch" if args.no_fetch else "auto"

    raw_items = load_or_fetch(mode)
    print(f"Total posters available: {len(raw_items)}")

    matched = build_records(raw_items)
    if matched:
        print(f"Enriching {len(matched)} matches with day/keywords/categories…")
        enrich(matched)
    write_json(matched)
    generate_html(matched, len(raw_items))
    print_summary(matched)
    print(f"\nDone. Open {INDEX_HTML.name} in a browser.")


if __name__ == "__main__":
    main()
