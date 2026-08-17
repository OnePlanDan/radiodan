"""
The dev journal, on the web.

House rule: Dan never reads repo files — for the agent a file is one tool
call, for him it's a chain of FTP, password managers and VPNs. So the journal
is served: a table of entries (date, title, author) and full rendered bodies.

Two sources, merged:

- `doc/journal.md` — the running log. Each `- YYYY-MM-DD: …` bullet becomes
  an entry; its title is derived from the first sentence. History gets a web
  face without rewriting a single old line.
- `doc/journal/*.md` — rich entries with YAML frontmatter (title, date,
  author) and full markdown bodies: fenced code, tables, and ```mermaid
  blocks (rendered client-side into diagrams).
"""

import hashlib
import logging
import re
from pathlib import Path

import markdown as md
import yaml
from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

_PAGES_DIR = Path(__file__).parent.parent / "pages"
_DOC_DIR = Path(__file__).parent.parent.parent.parent / "doc"

_BULLET_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}): ", re.MULTILINE)
_DEFAULT_AUTHOR = "Claude (radio agent)"

_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists"]


def _derive_title(text: str) -> str:
    """First sentence of an entry, cleaned of markup, cut to table width."""
    first_line = text.strip().splitlines()[0]
    sentence = re.split(r"(?<=[.!?])\s", first_line, maxsplit=1)[0]
    sentence = re.sub(r"[*_`]", "", sentence).rstrip(".:").strip()
    if len(sentence) > 80:
        sentence = sentence[:77].rsplit(" ", 1)[0] + "…"
    return sentence or "(untitled)"


def _entry_id(date: str, text: str) -> str:
    return f"{date}-{hashlib.sha1(text[:80].encode()).hexdigest()[:6]}"


def _bullet_entries() -> list[dict]:
    """The running log, split into entries."""
    path = _DOC_DIR / "journal.md"
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")

    entries = []
    matches = list(_BULLET_RE.finditer(raw))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[m.end():end].strip()
        # Continuation lines are indented under the bullet; dedent for markdown.
        body = re.sub(r"^ {2}", "", body, flags=re.MULTILINE)
        date = m.group(1)
        entries.append({
            "id": _entry_id(date, body),
            "date": date,
            "title": _derive_title(body),
            "author": _DEFAULT_AUTHOR,
            "source": "log",
            "_markdown": body,
        })
    return entries


def _rich_entries() -> list[dict]:
    """Standalone entries with frontmatter — diagrams, code, the works."""
    folder = _DOC_DIR / "journal"
    if not folder.is_dir():
        return []
    entries = []
    for path in sorted(folder.glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
            meta: dict = {}
            body = raw
            if raw.startswith("---"):
                _, front, body = raw.split("---", 2)
                meta = yaml.safe_load(front) or {}
            date = str(meta.get("date") or "")[:10]
            entries.append({
                "id": path.stem,
                "date": date,
                "title": str(meta.get("title") or _derive_title(body)),
                "author": str(meta.get("author") or _DEFAULT_AUTHOR),
                "source": "entry",
                "_markdown": body.strip(),
            })
        except Exception:
            logger.exception(f"Unreadable journal entry: {path}")
    return entries


def _all_entries() -> list[dict]:
    merged = _rich_entries() + _bullet_entries()
    # Newest first; rich entries win ties so a written-up piece leads its day.
    merged.sort(key=lambda e: (e["date"], e["source"] == "entry"), reverse=True)
    return merged


@routes.get("/journal")
async def journal_page(request: web.Request) -> web.Response:
    """The dev journal: table of entries, rendered bodies, diagrams."""
    page = _PAGES_DIR / "journal.html"
    if not page.exists():
        raise web.HTTPNotFound(reason="Journal page missing from build")
    return web.Response(text=page.read_text(encoding="utf-8"), content_type="text/html")


@routes.get("/api/journal")
async def journal_index(request: web.Request) -> web.Response:
    """Journal entries: id, date, title, author. Newest first."""
    listing = [{k: v for k, v in e.items() if not k.startswith("_")}
               for e in _all_entries()]
    return web.json_response({"entries": listing, "count": len(listing)})


@routes.get("/api/journal/{entry_id}")
async def journal_entry(request: web.Request) -> web.Response:
    """One entry, rendered to HTML. ```mermaid fences become live diagrams."""
    entry_id = request.match_info["entry_id"]
    for e in _all_entries():
        if e["id"] == entry_id:
            html = md.markdown(e["_markdown"], extensions=_MD_EXTENSIONS)
            return web.json_response({
                **{k: v for k, v in e.items() if not k.startswith("_")},
                "html": html,
            })
    raise web.HTTPNotFound(reason=f"No journal entry {entry_id}")
