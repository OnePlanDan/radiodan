"""Tests for script-generation retry, salvage, and the silent-script fallback.

An unusable LLM reply used to end the rebuild cycle on the spot: the producer
took the silent fallback and ~50 minutes of radio went out with no DJ. Across
the retained journal that happened in 36 of ~631 builds (~6%) with nothing
re-asking.
"""

import json

import pytest

from bridge.plugins.producer.models import CharacterConfig
from bridge.plugins.producer.script_generator import (
    DEFAULT_MAX_ATTEMPTS,
    ScriptParseError,
    _extract_json,
    _parse_response,
    _salvage_json,
    generate_script,
)

BOB = CharacterConfig(id="bob", name="Bad Mouth Bob", personality="Gruff.")
LANI = CharacterConfig(id="lani", name="Lani", personality="Excitable.")
SONGS = [{"artist": f"A{i}", "title": f"T{i}", "genre": "hip-hop", "year": "1994"}
         for i in range(4)]


def _valid(cues_on: int = 2) -> str:
    return json.dumps({"segments": [
        {"song_index": i,
         "voice": ([{"character": "bob", "style": "intro", "text": f"line {i}"}]
                   if i < cues_on else [])}
        for i in range(len(SONGS))
    ]})


class FakeBackend:
    """Returns a scripted sequence of replies (str) or raises (Exception)."""

    name = "fake"
    model = "fake-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def chat(self, prompt, *, system_prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else self.replies_exhausted()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def replies_exhausted(self):
        raise AssertionError("backend called more times than the test scripted")


# =====================================================================
# RETRY
# =====================================================================

async def test_valid_first_reply_makes_one_call():
    backend = FakeBackend(_valid())
    script = await generate_script(backend, BOB, SONGS, {})
    assert len(backend.prompts) == 1
    assert sum(len(s.voice_cues) for s in script.segments) == 2


async def test_invalid_then_valid_recovers_and_keeps_the_talk():
    backend = FakeBackend("not json at all", _valid())
    script = await generate_script(backend, BOB, SONGS, {})
    assert len(backend.prompts) == 2
    assert sum(len(s.voice_cues) for s in script.segments) == 2, "the DJ should be back"


async def test_retry_prompt_explains_the_previous_failure():
    backend = FakeBackend("not json at all", _valid())
    await generate_script(backend, BOB, SONGS, {})
    retry = backend.prompts[1]
    assert "[Retry]" in retry
    assert "could not be used" in retry
    # And it still carries the original request.
    assert backend.prompts[0] in retry


async def test_backend_exception_is_retried_not_fatal():
    backend = FakeBackend(RuntimeError("ollama asleep"), _valid())
    script = await generate_script(backend, BOB, SONGS, {})
    assert len(backend.prompts) == 2
    assert any(s.voice_cues for s in script.segments)


async def test_gives_up_after_max_attempts_with_silent_script():
    backend = FakeBackend("junk", "junk", "junk")
    script = await generate_script(backend, BOB, SONGS, {}, max_attempts=3)
    assert len(backend.prompts) == 3
    # Music still flows; there is simply no talk.
    assert len(script.segments) == len(SONGS)
    assert not any(s.voice_cues for s in script.segments)


async def test_max_attempts_is_configurable():
    backend = FakeBackend("junk", "junk", "junk", "junk", "junk")
    await generate_script(backend, BOB, SONGS, {}, max_attempts=5)
    assert len(backend.prompts) == 5


async def test_max_attempts_below_one_still_tries_once():
    backend = FakeBackend("junk")
    await generate_script(backend, BOB, SONGS, {}, max_attempts=0)
    assert len(backend.prompts) == 1


async def test_default_is_three_attempts():
    backend = FakeBackend(*(["junk"] * DEFAULT_MAX_ATTEMPTS))
    await generate_script(backend, BOB, SONGS, {})
    assert len(backend.prompts) == DEFAULT_MAX_ATTEMPTS


async def test_all_silent_reply_is_retried():
    """The prompt asks for talk on 60-70% of songs; zero talk is a defect."""
    backend = FakeBackend(_valid(cues_on=0), _valid(cues_on=3))
    script = await generate_script(backend, BOB, SONGS, {})
    assert len(backend.prompts) == 2
    assert sum(len(s.voice_cues) for s in script.segments) == 3


async def test_all_silent_reply_is_accepted_on_the_final_attempt():
    """Better a valid song order with no talk than discarding the cycle."""
    backend = FakeBackend(_valid(cues_on=0), _valid(cues_on=0))
    script = await generate_script(backend, BOB, SONGS, {}, max_attempts=2)
    assert len(backend.prompts) == 2
    assert len(script.segments) == len(SONGS)


# =====================================================================
# PARSING
# =====================================================================

def test_parse_raises_on_invalid_json():
    with pytest.raises(ScriptParseError, match="JSON was invalid"):
        _parse_response("definitely not json", SONGS, BOB, [BOB])


def test_parse_raises_on_non_object_json():
    with pytest.raises(ScriptParseError, match="not an object"):
        _parse_response('["a list"]', SONGS, BOB, [BOB])


def test_parse_raises_when_segments_is_not_a_list():
    with pytest.raises(ScriptParseError, match="not a list"):
        _parse_response('{"segments": {"nope": 1}}', SONGS, BOB, [BOB])


def test_parse_raises_when_no_segments_are_usable():
    # Every song_index is out of range.
    payload = json.dumps({"segments": [{"song_index": 99, "voice": []}]})
    with pytest.raises(ScriptParseError, match="no usable segments"):
        _parse_response(payload, SONGS, BOB, [BOB])


def test_require_voice_raises_on_a_fully_silent_script():
    with pytest.raises(ScriptParseError, match="every segment was silent"):
        _parse_response(_valid(cues_on=0), SONGS, BOB, [BOB], require_voice=True)


def test_silent_script_allowed_when_voice_not_required():
    script = _parse_response(_valid(cues_on=0), SONGS, BOB, [BOB], require_voice=False)
    assert len(script.segments) == len(SONGS)


def test_malformed_segment_and_voice_entries_are_skipped_not_fatal():
    payload = json.dumps({"segments": [
        "not a dict",
        {"song_index": "one", "voice": []},
        {"song_index": 0, "voice": "not a list"},
        {"song_index": 1, "voice": ["not a dict", {"character": "bob", "text": "kept"}]},
    ]})
    script = _parse_response(payload, SONGS, BOB, [BOB])
    texts = [c.text for s in script.segments for c in s.voice_cues]
    assert texts == ["kept"]


def test_unknown_character_is_attributed_to_the_primary():
    payload = json.dumps({"segments": [
        {"song_index": 0, "voice": [{"character": "ghost", "text": "who am i"}]}]})
    script = _parse_response(payload, SONGS, BOB, [BOB, LANI])
    assert script.segments[0].voice_cues[0].character_id == "bob"


# =====================================================================
# SALVAGE
# =====================================================================

def test_salvage_recovers_a_truncated_response():
    full = _valid(cues_on=4)
    truncated = full[: int(len(full) * 0.6)]
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)

    data = _salvage_json(truncated)
    assert data is not None
    assert len(data["segments"]) >= 1


def test_parse_uses_salvage_instead_of_failing():
    full = _valid(cues_on=4)
    truncated = full[: int(len(full) * 0.6)]
    script = _parse_response(truncated, SONGS, BOB, [BOB])
    assert any(s.voice_cues for s in script.segments), "talk recovered from a cut-off reply"


async def test_salvage_avoids_a_retry():
    full = _valid(cues_on=4)
    backend = FakeBackend(full[: int(len(full) * 0.6)])
    script = await generate_script(backend, BOB, SONGS, {})
    assert len(backend.prompts) == 1, "salvage should spare the round-trip"
    assert any(s.voice_cues for s in script.segments)


def test_salvage_gives_up_on_hopeless_input():
    assert _salvage_json("no braces here") is None
    assert _salvage_json('{"segments": []}') is None  # empty is not usable
    assert _salvage_json("{{{{{{") is None


def test_salvage_is_bounded_on_pathological_input():
    """Many closing braces must not turn into an unbounded scan."""
    junk = "{" + "}" * 5000
    assert _salvage_json(junk) is None


# =====================================================================
# EXTRACTION
# =====================================================================

def test_extract_json_handles_markdown_fences():
    payload = '{"segments": []}'
    assert json.loads(_extract_json(f"```json\n{payload}\n```")) == {"segments": []}


def test_extract_json_handles_surrounding_prose():
    payload = '{"segments": []}'
    assert json.loads(_extract_json(f"Sure! Here you go:\n{payload}\nHope that helps")) \
        == {"segments": []}
