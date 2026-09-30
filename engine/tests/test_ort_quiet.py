"""ONNX Runtime's native log lines never reach stderr.

Measured on a Hyper-V Ubuntu 22.04 VM: every search printed
`[W:onnxruntime:Default, device_discovery.cc:146 GetPciBusId] Skipping
pci_bus_id …` — written by native code straight to file descriptor 2 while the
runtime loads, before any logger setting Python can apply. An agent whose
terminal merges stderr into stdout pays for the line and fails to parse the
JSON after it. The stand-in below writes to fd 2 the same way, at import and at
session creation, so the test does not need that VM.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from cartograph import embeddings

_FAKE_ORT = '''
import os
os.write(2, b"NATIVE-IMPORT-WARNING\\n")

def set_default_logger_severity(level):
    pass

class SessionOptions:
    log_severity_level = 0

class _Input:
    def __init__(self, name):
        self.name = name

class InferenceSession:
    def __init__(self, *a, **k):
        os.write(2, b"NATIVE-SESSION-WARNING\\n")
    def get_inputs(self):
        return [_Input("input_ids"), _Input("attention_mask")]
'''

_FAKE_TOKENIZERS = '''
class Tokenizer:
    @classmethod
    def from_file(cls, path):
        return cls()
    def enable_truncation(self, n):
        pass
    def enable_padding(self):
        pass
'''


@pytest.fixture()
def fake_runtime(tmp_path, monkeypatch):
    pkgs = tmp_path / "fake"
    (pkgs / "onnxruntime").mkdir(parents=True)
    (pkgs / "onnxruntime" / "__init__.py").write_text(_FAKE_ORT, encoding="utf-8")
    (pkgs / "tokenizers").mkdir()
    (pkgs / "tokenizers" / "__init__.py").write_text(_FAKE_TOKENIZERS, encoding="utf-8")
    for name in ("onnxruntime", "tokenizers"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(pkgs))
    model = tmp_path / "model"
    model.mkdir()
    (model / embeddings.MODEL_MANIFEST).write_text(json.dumps({
        "model": "model.onnx", "tokenizer": "tokenizer.json", "max_seq_length": 128,
        "dimension": 384, "name": "fake",
    }), encoding="utf-8")
    return model


def test_native_writes_during_load_do_not_reach_stderr(fake_runtime, capfd):
    provider = embeddings.OnnxEmbeddingProvider(fake_runtime)

    provider._load()  # noqa: SLF001 — the load is the thing under test

    err = capfd.readouterr().err
    assert "NATIVE-IMPORT-WARNING" not in err
    assert "NATIVE-SESSION-WARNING" not in err


def test_stderr_works_again_after_the_load(fake_runtime, capfd):
    embeddings.OnnxEmbeddingProvider(fake_runtime)._load()  # noqa: SLF001
    os.write(2, b"after\n")
    assert capfd.readouterr().err == "after\n"


def test_a_failing_load_still_raises_and_restores_stderr(tmp_path, monkeypatch, capfd):
    """Silencing fd 2 must not swallow the failure itself: it arrives as an
    exception, which is where the one-line reason comes from."""
    broken = tmp_path / "broken"
    (broken / "onnxruntime").mkdir(parents=True)
    (broken / "onnxruntime" / "__init__.py").write_text(
        "import os\nos.write(2, b'NOISE\\n')\nraise ImportError('GLIBC_2.36 not found')\n",
        encoding="utf-8",
    )
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    monkeypatch.syspath_prepend(str(broken))
    model = tmp_path / "model"
    model.mkdir()
    (model / embeddings.MODEL_MANIFEST).write_text(json.dumps({
        "model": "m.onnx", "tokenizer": "t.json", "max_seq_length": 8,
        "dimension": 384, "name": "fake",
    }), encoding="utf-8")

    with pytest.raises(Exception, match="GLIBC_2.36"):
        embeddings.OnnxEmbeddingProvider(model)._load()  # noqa: SLF001

    os.write(2, b"after\n")
    err = capfd.readouterr().err
    assert "NOISE" not in err
    assert err.endswith("after\n")
