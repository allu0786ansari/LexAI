# Phase 3 — run and report (Windows Command Prompt)

Phase 3 makes answers correct and checkable: the question is understood before
searching, named sections are fetched exactly, the old and new criminal codes are
linked, and every answer is checked against its sources after it is written.
Everything runs in **cmd.exe**. Run the steps below, then push
`runlogs\phase3\output\`. Phase 4 starts only after it is reviewed.

## What changed for you

- **Named sections come first.** "What does Section 103 BNS say?" puts BNS s.103
  as passage [1], whatever its search rank, together with its counterpart in the
  old code (IPC s.302).
- **Old ↔ new code links.** LexAI matches every IPC/CrPC/IEA section to its
  BNS/BNSS/BSA counterpart by title and text when the API starts (about a second).
  It is a derived table, not an official one; answers say "corresponds to".
- **Which code applies.** With no offence date the answer uses the BNS/BNSS/BSA and
  names the old section; a date before 1 July 2024, or a question about the IPC,
  switches to the old codes.
- **Follow-ups.** From the second question in a chat, a short model call rewrites
  "and is it bailable?" into a full question before searching (adds ~5–10 s).
- **Checked answers.** After the answer streams, LexAI removes citation numbers
  that point nowhere and flags any section the answer mentions that is not in its
  sources. The UI shows "All provisions checked against the sources" or a warning.
- **Guardrails.** Requests for help with a crime (destroying evidence, bribing,
  threatening witnesses) are refused without searching; small talk is redirected.
  Phone, Aadhaar, PAN and e-mail are masked in logs.
- **Cache.** A repeated question is answered from memory in under a second.
- **New endpoint** `POST /api/search`: what the model would see for a question
  (plan, passages, scores), without generating an answer.

No re-download or rebuild: the Phase 2 index is reused.

## 1. Get the branch

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git fetch origin
git checkout phase-3-retrieval
git pull
```

## 2. Run everything

```bat
runlogs\phase3\run_phase3.bat
```

| Step | What it does | Log |
| --- | --- | --- |
| `check` | Git state, Python, Ollama, GPU, RAM, port 8000 | `01_check.log` |
| `setup` | pip install; laptop profile v3 (adds the abstention threshold; the old `.env` is saved as `.env.previous`) | `02_setup.log` |
| `quality` | ruff, format check, mypy, 140 tests | `03_quality.log` |
| `models` | Models present and responding | `04_models.log` |
| `status` | The live index from Phase 2 | `05_status.log` |
| `phase3` | Code links, exact lookups, mappings, follow-ups, calibration, checked answers, refusal, cache (~5 min) | `06_phase3.log`, `06_server.log` |
| `smoke` | Same as Phase 2 (streaming, citations, parallel answers) | `07_smoke.log`, `07_server.log` |

Optional: `runlogs\phase3\run_phase3.bat outage` (log `08_outage.log`).

## 3. Send the results

```bat
git add runlogs\phase3\output
git commit -m "Phase 3 run logs"
git push
```

## What "pass" looks like (`06_phase3.log`)

| Section | Pass |
| --- | --- |
| A. Correspondence | at least 80% of 45 well-known pairs (IPC→BNS, CrPC→BNSS, IEA→BSA) |
| B1. Exact lookups | each named section is `[1]`; CrPC s.438 brings BNSS s.482, IEA s.65B brings BSA s.63 |
| B2. Mappings | IPC s.420 with BNS s.318; CrPC s.154 with BNSS s.173 |
| B3. Code in force | BNS s.103 ranks before IPC s.302 |
| B4. Follow-ups | the rewritten follow-up finds an answering provision whenever the standalone question does |
| B5. Abstention | no answerable question declined (threshold 0.63 from run 1); unanswerable ones declined are reported |
| B6. Answers | events end `verification`, `done`; BNS s.103 is `[1]`; refusal without a model call; repeat from cache |
| | `SUMMARY: ALL PASSED` |

`07_smoke.log` should still end with `SUMMARY: ALL PASSED`.

## Run history

| Run | Date | Result |
| --- | --- | --- |
| 1 | 2026-11-20 | Correspondence 42/45 (IPC→BNS 18/21, CrPC→BNSS 12/12, IEA→BSA 12/12); exact lookups 4/4; mappings 2/2; smoke all passed; time to first token 11 s (was 32 s). **Failed:** (a) the verifier missed citations written after the full stop ("fine. [1]") → coverage 0, answer not cached; (b) BNS s.103 ranked after IPC s.302 because IPC 302's top match was BNS 104 and only the top match was moved; (c) a follow-up returned one passage: two hits in the 74-piece BNSS First Schedule pulled in the whole schedule; (d) IPC 379/406/392 unmatched: the BNS merged definition and punishment into one section; (e) a cache test timed out on Windows' clock resolution. Calibration: answerable 0.687–0.849, unanswerable 0.614–0.681. Fixed all; threshold set to 0.63 (profile v3). |
| 2 | 2026-11-20 | Correspondence **44/45** (IPC→BNS 20/21; IPC 379 → BNS 303 still missed: its one sentence is a small part of BNS 303(2)); lookups 4/4; mappings 2/2; **BNS before IPC**; verification valid, coverage 1.0; cache hit in 0.01 s; GST declined before generation, no answerable question declined; smoke passed. **Failed:** (a) 7 API tests on Windows: the test settings read the laptop's `backend\.env`, whose new 0.63 threshold made the fake index abstain — tests now ignore `.env` and `LEXAI_*` variables; (b) the follow-up check compared overlap with a differently worded standalone question although both rewrites were right ("Is section 103 of the BNS bailable?" → BNS s.103; "old IPC section for murder" → IPC s.302 first) — it now checks that the follow-up finds an answering provision whenever the standalone does (both pass on run 2's data). Also: pieces of one long section are joined into one passage (BNS s.101 appeared three times); light stemming in the correspondence. |

