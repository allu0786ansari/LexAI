"""Evaluation harness: metrics, dataset rules, judge parsing, and the offline CI gate."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from lexai.core.errors import ConfigurationError
from lexai.core.settings import Settings
from lexai.evaluation import __main__ as cli
from lexai.evaluation.dataset import (
    GoldenItem,
    load_dataset,
    load_items,
    lock_path,
    parse_label,
    validate_item,
    write_lock,
)
from lexai.evaluation.fixture import export_index, fixture_present, load_fixture, write_queries
from lexai.evaluation.judge import Judge, average_precision
from lexai.evaluation.metrics import (
    abstention_pr,
    answer_scores,
    predicted_abstain,
    ranked_keys,
    retrieval_scores,
)
from lexai.evaluation.sources import IndexTextSource
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore
from lexai.retrieval.sparse import query_vector
from tests.conftest import FakeEmbedder, FakeLLM

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = REPO_ROOT / "eval" / "datasets" / "golden_v1.jsonl"
Live = tuple[QdrantStore, str, BuildMeta]


def item(**kw: Any) -> GoldenItem:
    base: dict[str, Any] = {"id": "x-1", "category": "section_lookup", "question": "q", "gold": ["BNS s.103"]}
    return GoldenItem.model_validate(base | kw)


# ---------------------------------------------------------------- retrieval metrics


def test_ranked_keys_use_labels_for_statutes_and_doc_ids_for_judgments() -> None:
    passages = [
        {"label": "BNS s.103", "law_type": "statute", "doc_id": "bns-2023"},
        {"label": "BNS s.103", "law_type": "statute", "doc_id": "bns-2023"},  # second part, same provision
        {"label": "K.S. Puttaswamy", "law_type": "judgment", "doc_id": "sc-2017-puttaswamy"},
    ]
    assert ranked_keys(passages) == ["BNS s.103", "sc-2017-puttaswamy"]


def test_retrieval_scores_by_hand() -> None:
    gold = item(gold=["BNS s.103", "BNS s.101"])
    scores = retrieval_scores(gold, ["IPC s.302", "BNS s.103", "x", "y", "z", "w", "v", "BNS s.101"])
    assert scores["hit@1"] == 0.0
    assert scores["hit@5"] == 1.0
    assert scores["recall@8"] == 1.0
    assert scores["mrr@10"] == 0.5
    # DCG = 1/log2(3) + 1/log2(9); ideal = 1 + 1/log2(3)
    assert scores["ndcg@10"] == pytest.approx((0.6309 + 0.3155) / 1.6309, abs=1e-3)
    assert retrieval_scores(gold, ["a", "b"]) == {
        "hit@1": 0.0,
        "hit@5": 0.0,
        "recall@8": 0.0,
        "mrr@10": 0.0,
        "ndcg@10": 0.0,
    }


# ---------------------------------------------------------------- answer metrics


def test_answer_scores_citations_mentions_and_code() -> None:
    gold = item(must_mention=["imprisonment for life", "fine"], applicable_code="new")
    events = {
        "meta": {"intent": "provision", "applicable_code": "new"},
        "done": {
            "answer": "Murder is punished with death or imprisonment for life [1].",
            "citations": [
                {"ref": 1, "label": "BNS s.103", "law_type": "statute", "doc_id": "bns-2023"},
                {"ref": 2, "label": "IPC s.302", "law_type": "statute", "doc_id": "ipc-1860"},
            ],
            "verification": {"valid": False, "coverage": 1.0, "removed_markers": [7]},
            "latency": {"time_to_first_token_ms": 900, "total_ms": 4000},
        },
    }
    s = answer_scores(gold, events)
    assert s["answered"] == 1.0 and s["abstained"] == 0.0
    assert s["gold_cited"] == 1.0
    assert s["citation_validity"] == 0.5  # one valid marker, one removed by the verifier
    assert s["must_mention"] == 0.5
    assert s["code_correct"] == 1.0
    assert s["ttft_ms"] == 900


def test_abstention_is_read_from_the_flag_or_the_wording() -> None:
    assert predicted_abstain({"abstained": True})
    assert predicted_abstain({"answer": "The indexed sources do not cover this question."})
    assert not predicted_abstain({"answer": "Section 103 BNS punishes murder [1]."})
    items = [item(id="a"), item(id="u", category="unanswerable", gold=[], should_abstain=True)]
    assert abstention_pr(items, [True, True]) == {
        "abstention_precision": 0.5,
        "abstention_recall": 1.0,
        "false_abstentions": 1.0,
    }


def test_injection_resistance_metric() -> None:
    adversarial = item(category="adversarial", gold=[], must_not_mention=["PWNED"])
    bad = answer_scores(adversarial, {"done": {"answer": "pwned"}})
    good = answer_scores(adversarial, {"done": {"answer": "Section 103 punishes murder [1]."}})
    assert bad["must_not_mention_ok"] == 0.0 and good["must_not_mention_ok"] == 1.0


# ---------------------------------------------------------------- dataset rules


def test_items_are_checked_on_load() -> None:
    with pytest.raises(ValueError, match="should_abstain"):
        item(category="unanswerable", gold=[])
    with pytest.raises(ValueError, match="needs gold"):
        item(gold=[])
    with pytest.raises(ValueError, match="gold_docs"):
        item(category="case_law", gold=[])
    with pytest.raises(ValueError, match="bad gold label"):
        item(gold=["section 103"])
    assert parse_label("BNS s.103") == ("BNS", "Section", "103")
    assert parse_label("Constitution Art.21A") == ("Constitution", "Article", "21A")
    assert parse_label("BNSS Schedule First") == ("BNSS", "Schedule", "First")


def test_split_is_stable_and_the_lock_catches_test_edits(tmp_path: Path) -> None:
    items = [item(id=f"x-{n}") for n in range(60)]
    splits = Counter(i.split for i in items)
    assert splits["dev"] and splits["test"]
    assert [i.split for i in items] == [item(id=f"x-{n}").split for n in range(60)]  # by id, not order

    path = tmp_path / "golden.jsonl"
    path.write_text("".join(i.model_dump_json() + "\n" for i in items), encoding="utf-8")
    write_lock(path, items)
    assert len(load_dataset(path, "test")) == splits["test"]

    dev = next(i for i in items if i.split == "dev")
    test = next(i for i in items if i.split == "test")
    edited_dev = [i.model_copy(update={"question": "changed"}) if i is dev else i for i in items]
    path.write_text("".join(i.model_dump_json() + "\n" for i in edited_dev), encoding="utf-8")
    load_dataset(path)  # editing dev items is allowed

    edited_test = [i.model_copy(update={"question": "changed"}) if i is test else i for i in items]
    path.write_text("".join(i.model_dump_json() + "\n" for i in edited_test), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="locked test split"):
        load_dataset(path)


def test_golden_v1_shape() -> None:
    items = load_items(GOLDEN)
    assert len(items) == 200
    cats = Counter(i.category for i in items)
    assert cats == {
        "concept": 50,
        "section_lookup": 40,
        "case_law": 30,
        "old_to_new": 25,
        "unanswerable": 25,
        "multi_turn": 20,
        "adversarial": 10,
    }
    dev = sum(i.split == "dev" for i in items)
    assert 0.5 < dev / len(items) < 0.7
    for cat in cats:  # every category is represented on both sides
        assert {i.split for i in items if i.category == cat} == {"dev", "test"}
    if lock_path(GOLDEN).exists():
        load_dataset(GOLDEN)  # raises if a locked test item was edited


async def test_validation_against_the_index(live_store: Live) -> None:
    store, name, _ = live_store
    source = IndexTextSource(store, name)
    ok = await validate_item(item(must_mention=["imprisonment for life"]), source)
    assert ok.ok, ok.problems
    missing = await validate_item(item(gold=["BNS s.999"]), source)
    assert missing.problems == ["BNS s.999 not found in the index"]
    wrong = await validate_item(item(must_mention=["rigorous imprisonment"]), source)
    assert wrong.problems == ["must_mention 'rigorous imprisonment' is not in the gold text"]


# ---------------------------------------------------------------- judge


async def test_judge_scores_from_model_json() -> None:
    graded = {
        "claims": [{"supported": True}, {"supported": True}, {"supported": False}, {"supported": True}],
        "cited_sentences": [{"supported_by_cited": True}, {"supported_by_cited": False}],
        "relevancy": 0.9,
        "useful": [True, False, True],
        "reference_statements": [{"in_passages": True}, {"in_passages": False}],
    }
    llm = FakeLLM(completion="Here you go:\n" + json.dumps(graded))

    async def text(_: dict[str, Any]) -> str:
        return "passage text"

    judge = Judge(llm, "fake-judge", text)  # type: ignore[arg-type]
    done = {"answer": "x [1]", "citations": [{"ref": 1, "label": "BNS s.103"}]}
    scores = await judge.score(item(reference_answer="Death or life imprisonment."), done)
    assert scores == {
        "faithfulness": 0.75,
        "answer_relevancy": 0.9,
        "citation_support": 0.5,
        "context_precision": pytest.approx(0.8333, abs=1e-4),
        "context_recall": 0.5,
    }
    assert len(llm.complete_calls) == 2

    broken = Judge(FakeLLM(completion="no json here"), "fake", text)  # type: ignore[arg-type]
    assert set((await broken.score(item(), done)).values()) == {None}


def test_average_precision() -> None:
    assert average_precision([True, True]) == 1.0
    assert average_precision([False, True]) == 0.5
    assert average_precision([False, False]) == 0.0
    assert average_precision([]) is None


# ---------------------------------------------------------------- offline fixture and gate


def test_gate_tolerance() -> None:
    base = {"overall": {"recall@8": 0.80, "mrr@10": 0.60}}
    assert cli.gate({"overall": {"recall@8": 0.79, "mrr@10": 0.59}}, base) == []
    failures = cli.gate({"overall": {"recall@8": 0.70, "mrr@10": 0.60}}, base)
    assert len(failures) == 1 and failures[0].startswith("recall@8")


async def test_fixture_round_trip_runs_the_live_pipeline(
    live_store: Live, settings: Settings, tmp_path: Path
) -> None:
    """Export the test index, rebuild it in memory, and run retrieval over it with no model servers."""
    store, name, meta = live_store
    out = tmp_path / "fixture"
    assert await export_index(store, name, meta, out) == 5
    items = [
        item(id="sl-1", question="What does Section 103 BNS say?", gold=["BNS s.103"]),
        item(id="sl-2", question="Article 21 of the Constitution", gold=["Constitution Art.21"]),
        item(id="sl-3", question="IT Act section 66", gold=["IT Act s.66"]),
    ]
    embedder = FakeEmbedder()
    write_queries(
        out,
        {i.id: {"standalone": i.question, "vector": await embedder.embed_query(i.question)} for i in items},
    )
    assert fixture_present(out)
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text("".join(i.model_dump_json() + "\n" for i in items), encoding="utf-8")

    mem, collection, loaded, _ = await load_fixture(out)
    assert loaded.collection == collection
    for mode in ("bm25", "dense"):  # both vector types survived the round trip
        hits = await mem.hybrid_search(
            collection,
            [await embedder.embed_query("murder")],
            query_vector("punishment for murder"),
            k_dense=5,
            k_sparse=5,
            limit=3,
            mode=mode,
        )
        assert hits and hits[0].chunk.metadata.unit_title == "Punishment for murder", mode
    await mem.close()

    hybrid = await cli.offline_run("hybrid", out, dataset)
    no_routing = await cli.offline_run("no-routing", out, dataset)
    assert hybrid.summary["scored"] == 3
    assert hybrid.summary["overall"]["mrr@10"] == 1.0  # exact section lookups are pinned first
    assert no_routing.summary["overall"]["mrr@10"] <= hybrid.summary["overall"]["mrr@10"]


@pytest.mark.skipif(
    not (fixture_present(cli.FIXTURE_DIR) and cli.BASELINE.exists()),
    reason="no exported fixture / baseline in eval/ (run export-fixture on the laptop)",
)
async def test_offline_gate_against_the_committed_baseline() -> None:
    baseline = json.loads(cli.BASELINE.read_text(encoding="utf-8"))
    run = await cli.offline_run(baseline["config"])
    assert cli.gate(run.summary, baseline) == []
    regressed = await cli.offline_run("no-routing")
    assert cli.gate(regressed.summary, baseline), "removing routing must fail the gate"
