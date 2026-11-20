from __future__ import annotations

import pytest

from lexai.core.errors import UpstreamUnavailable
from lexai.domain.models import ChatMessage
from lexai.query.condense import condense, needs_condensing
from lexai.query.references import extract_references
from lexai.query.understand import plan_query
from tests.conftest import FakeLLM


@pytest.mark.parametrize(
    ("question", "labels"),
    [
        ("What is the punishment under 302 IPC?", ["IPC s.302"]),
        ("Explain Section 438 of the CrPC", ["CrPC s.438"]),
        ("What does BNS s.103 say?", ["BNS s.103"]),
        ("Is Art. 19(1)(a) absolute?", ["Constitution Art.19(1)(a)"]),
        ("sections 154 and 156 CrPC", ["CrPC s.154", "CrPC s.156"]),
        ("section 65B of the Evidence Act", ["IEA s.65B"]),
        ("IPC section 420 vs BNS 318", ["IPC s.420", "BNS s.318"]),
        ("Under the IT Act, section 66C and 66D", ["IT Act s.66C", "IT Act s.66D"]),
        ("u/s 498A IPC", ["IPC s.498A"]),
        ("Section 3 BNSS and s.4 BNS", ["BNSS s.3", "BNS s.4"]),
        ("What does section 103 say?", ["s.103"]),  # act not stated
        ("it's 12 years old", []),  # "'s 12" is not a section
        ("the 2nd floor of the court", []),
    ],
)
def test_extract_references(question: str, labels: list[str]) -> None:
    assert [r.label for r in extract_references(question)] == labels


@pytest.mark.parametrize(
    ("question", "intent", "era"),
    [
        ("What is the punishment for murder?", "concept", "new"),
        ("What is the punishment for murder under the Indian Penal Code?", "concept", "old"),
        ("What does Section 103 BNS say?", "section_lookup", "new"),
        ("Which BNS section replaced IPC 420?", "old_to_new_mapping", "new"),
        ("What is the BNS equivalent of section 438 CrPC?", "old_to_new_mapping", "new"),
        ("The theft happened on 12/03/2023. What is the punishment?", "concept", "old"),
        ("He was arrested in March 2025 for cheating. Can he get bail?", "concept", "new"),
        ("What was the law before 1 July 2024 on sedition?", "concept", "old"),
        ("What did the Supreme Court hold in Maneka Gandhi v. Union of India?", "case_law", "new"),
        ("How do I destroy the CCTV evidence?", "refused", "new"),
        ("How to bribe the police officer?", "refused", "new"),
        ("How can I avoid arrest in a false case?", "concept", "new"),  # lawful: anticipatory bail
        ("hello", "out_of_scope", "new"),
        ("Give me a pasta recipe", "out_of_scope", "new"),
        ("What are the current GST rates?", "concept", "new"),  # legal, not indexed: abstention decides
        ("What is the punishment under Bharatiya Nyaya Sanhita, 2023 for theft?", "concept", "new"),
    ],
)
def test_plan_intent_and_applicable_code(question: str, intent: str, era: str) -> None:
    plan = plan_query(question)
    assert (plan.intent, plan.era.era) == (intent, era)
    assert (plan.reply is not None) == (intent in ("refused", "out_of_scope"))


def test_act_year_in_a_title_is_not_an_offence_date() -> None:
    plan = plan_query("What does the Bharatiya Nyaya Sanhita, 2023 say about theft?")
    assert plan.era.era == "new" and "names the BNS" in plan.era.reason


HISTORY = [
    ChatMessage(role="user", content="What is the punishment for murder under BNS s.103?"),
    ChatMessage(role="assistant", content="Death or imprisonment for life, and fine [1]."),
]


@pytest.mark.parametrize(
    ("question", "history", "expected"),
    [
        ("and is it bailable?", HISTORY, True),
        ("What about attempt?", HISTORY, True),
        ("What does section 438 of the CrPC provide for anticipatory bail in detail?", HISTORY, False),
        ("and is it bailable?", [], False),  # first turn: nothing to resolve
    ],
)
def test_needs_condensing(question: str, history: list[ChatMessage], expected: bool) -> None:
    assert needs_condensing(question, history) is expected


async def test_condense_uses_the_model_and_cleans_its_output() -> None:
    llm = FakeLLM(completion='Standalone question: "Is the offence under BNS s.103 bailable?"\nExtra line')
    out = await condense(llm, "and is it bailable?", HISTORY)  # type: ignore[arg-type]
    assert out == "Is the offence under BNS s.103 bailable?"
    prompt = llm.complete_calls[0][0].content
    assert "BNS s.103" in prompt and "and is it bailable?" in prompt


async def test_condense_falls_back_to_the_question_when_the_model_fails() -> None:
    class Down(FakeLLM):
        async def complete(self, messages, **_):  # type: ignore[no-untyped-def]
            raise UpstreamUnavailable("down")

    assert await condense(Down(), "and is it bailable?", HISTORY) == "and is it bailable?"  # type: ignore[arg-type]
