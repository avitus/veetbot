from pathlib import Path

from scripts.gate_registry import load_registry

ROOT = Path(__file__).resolve().parents[2]


def test_corpus_minimum() -> None:
    entries, errors = load_registry(ROOT)
    assert errors == []
    corpus_gates = [entry for entry in entries if entry.kind == "corpus" and entry.milestone <= 4]
    assert corpus_gates
    for gate in corpus_gates:
        assert gate.corpus is not None
        assert gate.minimum_members is not None
        members = [path for path in (ROOT / gate.corpus).iterdir() if path.is_file()]
        assert len(members) >= gate.minimum_members
