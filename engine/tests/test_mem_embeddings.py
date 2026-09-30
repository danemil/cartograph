"""Meaning-based recall with the model the payload ships (decision 2).

Two kinds of test live here. The first need no model: they pin where vectors
are written — never in the prompt-capture hook, always eventually — using the
deterministic fake from ``test_mem_store``. The second run the real int8
all-MiniLM-L6-v2 that ``scripts/build-payload.py --model-only`` fetches into
``build/model``, because "a reworded memory is found" is a claim about the
model, and a fake cannot make it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from cartograph import embeddings
from cartograph.mem import ingest
from cartograph.mem import store as mem_store
from cartograph.mem import sync as mem_sync

from .test_mem_store import _FakeProvider

ENGINE = Path(__file__).resolve().parents[1]
BUILT_MODEL = ENGINE.parent / "build" / "model"


def _model_dir() -> Path | None:
    named = os.environ.get(embeddings.MODEL_DIR_ENV)
    candidate = Path(named) if named else BUILT_MODEL
    return candidate if (candidate / embeddings.MODEL_MANIFEST).is_file() else None


needs_model = pytest.mark.skipif(
    _model_dir() is None,
    reason="no embedding model; run: python3 scripts/build-payload.py --model-only",
)

#: Memories as this project actually records them: a one-line title, then why.
MEMORIES = [
    ("Rejected FalkorDB for the graph store",
     "Its SSPLv1 licence is not something the client's legal team will sign off, "
     "and it needs a daemon running. Staying on SQLite."),
    ("Offset paging for mem search",
     "Cursors bind to the provenance snapshot; agents re-issue a query rather "
     "than holding state between calls."),
    ("Hooks always exit 0",
     "Copilot reads exit code 2 as blocking feedback to the model, so a hook "
     "that fails must stay silent."),
    ("build-vsix.sh refuses a stale payload",
     "A local build once shipped a 12-day-old engine because the payload was "
     "older than the source."),
    ("Launcher resolves the engine through runtime.path",
     "A VS Code extension lives in a version-stamped directory; a baked path "
     "would break on the next update."),
]

#: Shares no searchable word with the FalkorDB memory — "licensing" and
#: "licence" stem apart, "storage" and "store" too — so keyword search,
#: relaxed or not, cannot find it.
REWORDED = "which storage option did we turn down over the licensing terms"


def _seed(memory: mem_store.MemoryStore, *, embed: bool = True) -> None:
    for title, body in MEMORIES:
        memory.add(project="p", title=title, body=body, embed=embed)


@pytest.fixture
def fake(monkeypatch):
    pytest.importorskip("sqlite_vec", reason="the vector path is optional by design")
    monkeypatch.setattr("cartograph.embeddings.get_provider", lambda *a, **k: _FakeProvider())


# ---------------------------------------------------------------------------
# Where vectors are written
# ---------------------------------------------------------------------------


class TestNotInTheHookPath:
    def test_capture_never_asks_for_a_provider(self, tmp_path, monkeypatch):
        def refuse(*_a, **_k):
            raise AssertionError("prompt capture resolved an embedding provider")

        monkeypatch.setattr("cartograph.embeddings.get_provider", refuse)
        assert ingest.capture(
            tmp_path, {"session_id": "s1", "prompt": "why do we pin the grammar pack version"}
        )

    def test_capture_imports_none_of_the_embedding_stack(self, tmp_path):
        """In a fresh interpreter, with a model named: nothing heavy is imported."""
        pytest.importorskip("onnxruntime")
        model = tmp_path / "model"
        model.mkdir()
        # A manifest is all provider construction reads; if capture got as far
        # as running the model, the missing model.onnx would say so too.
        (model / embeddings.MODEL_MANIFEST).write_text(json.dumps({
            "name": "stub", "model": "model.onnx", "tokenizer": "tokenizer.json",
            "dimension": 384, "max_seq_length": 256,
        }))
        repo = tmp_path / "repo"
        repo.mkdir()
        script = (
            "import sys, json\n"
            "from pathlib import Path\n"
            "from cartograph.mem import ingest, store\n"
            f"ok = ingest.capture(Path({str(repo)!r}), "
            "{'session_id': 's1', 'prompt': 'why do we pin the grammar pack version'})\n"
            "loaded = [n for n in ('onnxruntime', 'tokenizers', 'numpy', 'sqlite_vec') "
            "if n in sys.modules]\n"
            f"m = store.MemoryStore(store.db_path(Path({str(repo)!r})))\n"
            "print(json.dumps({'ok': ok, 'vectors': m.vector_count(), 'loaded': loaded}))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(ENGINE), embeddings.MODEL_DIR_ENV: str(model)}
        out = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True,
        )
        report = json.loads(out.stdout.strip().splitlines()[-1])
        assert report["ok"] is True
        # Before this release capture loaded sqlite-vec, and numpy with it.
        assert report["loaded"] == []
        assert report["vectors"] == 0


class TestBackfill:
    def test_an_old_store_is_embedded_by_searching_it(self, tmp_path, fake):
        with mem_store.MemoryStore(tmp_path / "m.db") as memory:
            _seed(memory, embed=False)  # as a store written before this release
            assert memory.vector_count() == 0
            assert memory.search(query="SQLite daemon")[1] == "hybrid"
            assert memory.vector_count() == len(MEMORIES)

    def test_a_search_embeds_at_most_the_cap(self, tmp_path, fake, monkeypatch):
        monkeypatch.setattr(mem_store, "BACKFILL_CAP", 2)
        with mem_store.MemoryStore(tmp_path / "m.db") as memory:
            _seed(memory, embed=False)
            memory.search(query="anything at all")
            assert memory.vector_count() == 2

    def test_sync_embeds_everything_the_hooks_left(self, tmp_path, fake, monkeypatch):
        monkeypatch.setattr(mem_store, "BACKFILL_CAP", 2)
        for n in range(5):
            assert ingest.capture(
                tmp_path, {"session_id": "s1", "prompt": f"captured prompt number {n} here"}
            )
        result = mem_sync.sync(tmp_path, user_dirs=[], copilot_dir=tmp_path / "no-copilot")
        assert result["embedded"] == 5
        with mem_store.MemoryStore(mem_store.db_path(tmp_path)) as memory:
            assert memory.vector_count() == 5

    def test_a_write_embeds_itself(self, tmp_path, fake):
        with mem_store.MemoryStore(tmp_path / "m.db") as memory:
            assert memory.add(project="p", title="alpha beta", body="gamma")["embedded"] is True


# ---------------------------------------------------------------------------
# The real model
# ---------------------------------------------------------------------------


@pytest.fixture
def model(monkeypatch):
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    directory = _model_dir()
    monkeypatch.setenv(embeddings.MODEL_DIR_ENV, str(directory))
    monkeypatch.delenv("CRG_EMBEDDING_MODEL", raising=False)
    return directory


@needs_model
class TestTheShippedModel:
    def test_the_payload_model_is_the_default_provider(self, model):
        provider = embeddings.get_provider()
        assert isinstance(provider, embeddings.OnnxEmbeddingProvider)
        assert provider.name == "onnx:all-MiniLM-L6-v2-qint8"
        assert provider.dimension == 384

    def test_hybrid_finds_the_reworded_memory_keyword_misses(self, tmp_path, model, monkeypatch):
        path = tmp_path / "m.db"
        with mem_store.MemoryStore(path) as memory:
            _seed(memory)
            items, mode, _ = memory.search(query=REWORDED, limit=5)
        assert mode == "hybrid"
        assert items[0]["title"] == "Rejected FalkorDB for the graph store"

        # The same store, the same question, without the model.
        monkeypatch.delenv(embeddings.MODEL_DIR_ENV)
        with mem_store.MemoryStore(path) as memory:
            items, mode, _ = memory.search(query=REWORDED, limit=5)
            reason = memory.semantic_status()[1]
        assert mode == "fts"
        assert "Rejected FalkorDB for the graph store" not in [i["title"] for i in items]
        assert embeddings.MODEL_DIR_ENV in reason

    def test_an_unrelated_question_is_an_honest_miss(self, tmp_path, model):
        with mem_store.MemoryStore(tmp_path / "m.db") as memory:
            _seed(memory)
            items, mode, _ = memory.search(query="recipe for banana bread", limit=5)
        # The index was consulted — so hybrid — and nothing cleared the floor.
        assert mode == "hybrid"
        assert items == []

    def test_embedding_and_search_touch_no_network(self, tmp_path, model, monkeypatch):
        """Every Python-level way out is refused while the model loads and runs.

        Native code could in principle open a socket without Python; the
        frozen-binary proof under an OS-level block is scripts/ci_smoke.py.
        """
        def refuse(*_a, **_k):
            raise OSError("network access attempted during embedding")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)
        # A fresh provider, so the model load itself happens under the guard.
        monkeypatch.setattr(embeddings, "_MODEL_CACHE", {})
        with mem_store.MemoryStore(tmp_path / "m.db") as memory:
            _seed(memory)
            items, mode, _ = memory.search(query=REWORDED, limit=3)
        assert mode == "hybrid"
        assert items[0]["title"].startswith("Rejected FalkorDB")


@needs_model
class TestTheEnvelope:
    """Through the CLI, where an agent meets it."""

    @pytest.fixture
    def repo(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        return tmp_path

    def _carto(self, repo: Path, *args: str, model_dir: Path | None) -> dict:
        env = {**os.environ, "PYTHONPATH": str(ENGINE)}
        env.pop(embeddings.MODEL_DIR_ENV, None)
        if model_dir:
            env[embeddings.MODEL_DIR_ENV] = str(model_dir)
        out = subprocess.run(
            [sys.executable, "-m", "cartograph", "mem", *args, "--repo", str(repo)],
            env=env, capture_output=True, text=True,
        )
        return json.loads(out.stdout)

    def test_modes_are_reported_honestly(self, repo, model):
        for title, body in MEMORIES:
            self._carto(repo, "add", "--title", title, "--body", body, model_dir=model)

        hybrid = self._carto(repo, "search", "--query", REWORDED, model_dir=model)
        assert hybrid["search_mode"] == "hybrid"
        assert "semantic_unavailable" not in hybrid["data"]
        assert hybrid["data"]["items"][0]["title"].startswith("Rejected FalkorDB")

        keyword = self._carto(repo, "search", "--query", REWORDED, model_dir=None)
        assert keyword["search_mode"] == "keyword"
        assert embeddings.MODEL_DIR_ENV in keyword["data"]["semantic_unavailable"]

        status = self._carto(repo, "status", model_dir=model)
        assert status["data"]["semantic_search"] is True
        assert status["data"]["embedded_observations"] == len(MEMORIES)

    def test_the_budget_still_holds(self, repo, model):
        for title, body in MEMORIES:
            self._carto(repo, "add", "--title", title, "--body", body, model_dir=model)
        env = self._carto(
            repo, "search", "--query", "engine payload", "--max-tokens", "150",
            model_dir=model,
        )
        assert env["ok"] is True
        assert env["search_mode"] == "hybrid"
        assert env["size"]["tokens_estimated"] <= 150


# ---------------------------------------------------------------------------
# A runtime that will not load
# ---------------------------------------------------------------------------

#: What 0.8.0's linux-x64 payload printed on Ubuntu 22.04, verbatim in shape:
#: NumPy wraps the loader's one useful line in several paragraphs of advice.
NUMPY_GLIBC_ERROR = """

IMPORTANT: PLEASE READ THIS FOR ADVICE ON HOW TO SOLVE THIS ISSUE!

Importing the numpy C-extensions failed. This error can happen for
many reasons, often due to issues with your setup or how NumPy was
installed.

We have compiled some common reasons and troubleshooting tips at:

    https://numpy.org/devdocs/user/troubleshooting-importerror.html

Please note and check the following:

  * The Python version is: Python 3.12 from "/root/.vscode-server/extensions/cartograph.cartograph-0.8.0/payload/runtime/carto"
  * The NumPy version is: "2.5.3"

and make sure that they are the versions you expect.

Original error was: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.36' not found (required by /root/.vscode-server/extensions/cartograph.cartograph-0.8.0/payload/runtime/_internal/libstdc++.so.6)
"""

#: An agent reads the reason on every search; it has to stay a line.
REASON_MAX = 160


class TestTheFailureReason:
    def test_a_glibc_mismatch_is_named_in_one_line(self):
        reason = embeddings.load_failure_reason(ImportError(NUMPY_GLIBC_ERROR))
        assert reason == (
            "embedding runtime failed to load: GLIBC_2.36 not found (libstdc++.so.6)"
        )

    def test_a_missing_library_is_named_in_one_line(self):
        exc = ImportError(
            "\nIMPORTANT: PLEASE READ THIS\n\nOriginal error was: libgomp.so.1: "
            "cannot open shared object file: No such file or directory\n"
        )
        assert embeddings.load_failure_reason(exc) == (
            "embedding runtime failed to load: libgomp.so.1: cannot open shared "
            "object file: No such file or directory"
        )

    def test_anything_else_is_its_first_line_and_bounded(self):
        reason = embeddings.load_failure_reason(RuntimeError("x" * 500 + "\nsecond line"))
        assert "\n" not in reason
        assert len(reason) <= REASON_MAX
        assert reason.startswith("embedding runtime failed to load: RuntimeError: xxx")


@pytest.fixture
def broken_numpy(tmp_path):
    """A `numpy` that fails to import the way 0.8.0's did on glibc 2.35."""
    shadow = tmp_path / "shadow"
    (shadow / "numpy").mkdir(parents=True)
    (shadow / "numpy" / "__init__.py").write_text(f"raise ImportError({NUMPY_GLIBC_ERROR!r})\n")
    return shadow


@needs_model
class TestABrokenRuntimeInTheEnvelope:
    """The reason reaches the agent as one line, on stdout and stderr alike."""

    def _carto(self, repo: Path, shadow: Path, model_dir: Path, *args: str):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(shadow), str(ENGINE)]),
               embeddings.MODEL_DIR_ENV: str(model_dir)}
        env.pop("CARTO_DEBUG", None)
        out = subprocess.run(
            [sys.executable, "-m", "cartograph", "mem", *args, "--repo", str(repo)],
            env=env, capture_output=True, text=True,
        )
        return json.loads(out.stdout), out.stderr

    def test_search_and_status_carry_one_short_line(self, tmp_path, broken_numpy):
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        model_dir = _model_dir()
        expected = "embedding runtime failed to load: GLIBC_2.36 not found (libstdc++.so.6)"

        added, stderr = self._carto(repo, broken_numpy, model_dir,
                                    "add", "--title", MEMORIES[0][0], "--body", MEMORIES[0][1])
        assert added["ok"] is True
        assert added["data"]["observation"]["embedded"] is False
        search, search_err = self._carto(repo, broken_numpy, model_dir, "search", "--query", REWORDED)
        status, status_err = self._carto(repo, broken_numpy, model_dir, "status")

        assert search["search_mode"] == "keyword"
        assert search["data"]["semantic_unavailable"] == expected
        assert status["data"]["semantic_search"] is False
        assert status["data"]["semantic_search_unavailable"] == expected
        for reason in (search["data"]["semantic_unavailable"],
                       status["data"]["semantic_search_unavailable"]):
            assert "\n" not in reason and len(reason) <= REASON_MAX
        for err in (stderr, search_err, status_err):
            assert "PLEASE READ THIS" not in err
            assert len(err.strip().splitlines()) <= 1
