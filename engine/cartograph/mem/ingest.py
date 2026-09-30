"""What a hook records, and what it declines to.

The hook path writes observations nobody asked for, which makes *not* writing
the first-class decision here. An ingestion that records every event a host
emits produces a store where every search matches and nothing is worth
reading — the failure mode is not a full disk, it is a memory that no longer
distinguishes anything.

So exactly one host moment is recorded: the prompt the user submitted. It is
the only payload any host sends that carries text a *person* wrote, and it is
the question every later search is really asking about ("what was I asked to
do, and when"). The other candidates were read rather than guessed at:

* ``PostToolUse`` carries ``tool_name``/``tool_input``/``tool_response`` — a
  transcript of what the agent did. Durable copies of that already exist in
  git and in the graph, and one row per tool call is what turns a store into
  a log.
* ``SessionStart``/``SessionEnd``/``Stop`` carry a reason and some ids. There
  is no content in them to record.

Everything written here is ``summary_source="verbatim"``: this module copies
text, it does not summarise it, and claiming otherwise would make the field
useless for telling the two apart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from . import store as _store

logger = logging.getLogger(__name__)

#: Each field is looked up through a list rather than a constant. Copilot CLI
#: and Copilot Chat both send ``prompt``/``session_id``, read off both on
#: 2026-09-29 (``docs/copilot-hooks.md``); the other spellings are tolerated
#: rather than relied on. First non-empty wins.
_PROMPT_KEYS = ("prompt", "query", "input", "message")
_SESSION_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId", "id")

#: Below this an acknowledgement — "ok", "yes", "go ahead" — is all a prompt
#: can be, and an acknowledgement records nothing that outlives the turn it
#: was typed in.
MIN_PROMPT_CHARS = 16

#: A prompt can carry a pasted stack trace or a whole file. The text is kept
#: verbatim up to here and then cut, marked, because the first few thousand
#: characters are what makes an observation findable and the rest is weight
#: every search pays to skip. The same bound holds any body this package
#: writes, summaries included — the reason is about what a search result can
#: afford to carry, not about where the text came from.
MAX_BODY_CHARS = 4_000

#: One line, so a search result lists rather than scrolls.
MAX_TITLE_CHARS = 120

#: Observations one session may record. Not a quality filter — it is the
#: bound on how bad a loop can get. The re-entrancy marker is what stops a
#: capture from triggering a capture; this stops anything the marker does not
#: cover from filling the store before a human notices.
SESSION_CAP = 100


def _first_string(payload: dict[str, Any], keys: "tuple[str, ...]") -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def session_from(payload: dict[str, Any]) -> Optional[str]:
    """The session id a host payload carries, under whichever name it used.

    Public because the session-end hook needs the same answer from a payload
    that carries no prompt at all, and the spellings in :data:`_SESSION_KEYS`
    are the kind of list that goes stale in one copy and not the other.
    """
    if not isinstance(payload, dict):
        return None
    return _first_string(payload, _SESSION_KEYS)


def title_for(text: str) -> str:
    """The first line, bounded.

    The first line of a prompt is where a person puts the request; the rest is
    usually context for it. Taking it whole and cutting is closer to the
    intent than any cleverer summary would be, and it cannot mislead.

    Public because :mod:`cartograph.mem.summarise` needs the same one-line
    bound for a title it did not write — a second implementation of "first
    line, cut at 120" is how the two surfaces would come to disagree about
    what a title is.
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), text)
    if len(first) <= MAX_TITLE_CHARS:
        return first
    return first[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def clip_body(text: str, *, by: str) -> str:
    """*text* bounded by :data:`MAX_BODY_CHARS`, saying who cut it.

    ``by`` names the step that did the cutting, because a truncated body is
    read later by an agent with no way to tell a capture that clipped a pasted
    file from a summariser whose host ran long.
    """
    if len(text) <= MAX_BODY_CHARS:
        return text
    return text[:MAX_BODY_CHARS] + f"\n…[truncated by carto {by}]"


def observation_from(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Shape one host payload into arguments for :meth:`MemoryStore.add`.

    ``None`` means there is nothing durable in it, which is the common case
    and not an error. Pure, so the budget rules can be exercised without a
    database.
    """
    if not isinstance(payload, dict):
        return None
    text = _first_string(payload, _PROMPT_KEYS)
    if not text or len(text) < MIN_PROMPT_CHARS:
        return None
    return {
        "title": title_for(text),
        "body": clip_body(text, by="capture"),
        # `prompts` is its own doc_type in the schema because a prompt is a
        # different kind of record from an observation about the code, and an
        # agent searching for one rarely wants the other.
        "doc_type": "prompts",
        "kind": "prompt",
        "session": session_from(payload),
        "summary_source": "verbatim",
    }


def capture(
    repo_root: Path, payload: dict[str, Any], *, host: Optional[str] = None
) -> bool:
    """Record one payload, if it is worth recording. Never raises.

    Whether anything was written, for the tests and for nothing else: the
    caller is a hook, and there is no one it could report a failure to.
    """
    shaped = observation_from(payload)
    if shaped is None:
        return False
    try:
        # create=True: capture is the one caller allowed to bring the store
        # into existence. Nothing else would ever create it — the read paths
        # must be able to answer "there is no store" — so a first prompt in a
        # fresh checkout would otherwise be dropped forever.
        path = _store.db_path(repo_root, create=True)
        with _store.MemoryStore(path) as memory:
            session = shaped.get("session")
            if session and memory.session_count(session) >= SESSION_CAP:
                logger.debug("session %s is at the capture cap", session)
                return False
            if session and memory.has_document(
                session, shaped["body"], doc_type=shaped["doc_type"]
            ):
                return False
            # No embedding here: this runs inside the host's turn, and the
            # model's load would be most of a second on every prompt. The
            # next sync, write or search gives the row its vector.
            memory.add(project=repo_root.name, platform_source=host, embed=False, **shaped)
    except Exception as exc:  # noqa: BLE001 — a hook must not be the thing that fails
        logger.debug("capture skipped: %s", exc)
        return False
    return True
