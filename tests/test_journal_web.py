"""Tests for the web journal: parsing the log, rich entries, rendering."""

import pytest
from aiohttp import web

import bridge.web.routes.journal as journal_mod
from bridge.web.routes.journal import routes as journal_routes


@pytest.fixture
def doc_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "_DOC_DIR", tmp_path)
    (tmp_path / "journal.md").write_text(
        "# RadioDan Journal\n\n"
        "- 2026-08-17: The **newest** thing happened. And then details followed "
        "at great length, sentence after sentence.\n\n"
        "  ### An indented continuation\n\n"
        "  > quoted block belonging to the same entry\n\n"
        "- 2026-08-13: Fixed the older thing. More detail here.\n",
        encoding="utf-8",
    )
    rich = tmp_path / "journal"
    rich.mkdir()
    (rich / "2026-08-17-with-diagram.md").write_text(
        "---\ntitle: A rich entry with a diagram\ndate: 2026-08-17\n"
        "author: Claude (radio agent)\n---\n\nBody text.\n\n"
        "```mermaid\nflowchart LR\n  A --> B\n```\n\n"
        "```python\nprint('hello')\n```\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def client(aiohttp_client, doc_dir):
    app = web.Application()
    app.router.add_routes(journal_routes)
    return aiohttp_client(app)


async def test_index_lists_log_and_rich_entries_newest_first(client):
    c = await client
    d = await (await c.get("/api/journal")).json()
    assert d["count"] == 3
    dates = [e["date"] for e in d["entries"]]
    assert dates == sorted(dates, reverse=True)
    # Rich entry leads its day.
    assert d["entries"][0]["title"] == "A rich entry with a diagram"


async def test_log_bullet_titles_come_from_the_first_sentence(client):
    c = await client
    d = await (await c.get("/api/journal")).json()
    titles = [e["title"] for e in d["entries"]]
    assert "The newest thing happened" in titles, "markup stripped, cut at sentence"
    assert "Fixed the older thing" in titles


async def test_log_entry_keeps_its_indented_continuation(client):
    c = await client
    d = await (await c.get("/api/journal")).json()
    entry = next(e for e in d["entries"] if e["title"].startswith("The newest"))
    full = await (await c.get(f"/api/journal/{entry['id']}")).json()
    assert "An indented continuation" in full["html"]
    assert "<blockquote>" in full["html"]
    assert "Fixed the older thing" not in full["html"], "entries split correctly"


async def test_rich_entry_renders_code_and_mermaid_fences(client):
    c = await client
    full = await (await c.get("/api/journal/2026-08-17-with-diagram")).json()
    assert full["author"] == "Claude (radio agent)"
    assert 'class="language-mermaid"' in full["html"], "fence survives for client render"
    assert 'class="language-python"' in full["html"]


async def test_unknown_entry_is_a_404(client):
    c = await client
    assert (await c.get("/api/journal/nope")).status == 404


async def test_the_page_is_served(client):
    c = await client
    resp = await c.get("/journal")
    assert resp.status == 200
    assert "mermaid" in (await resp.text())
