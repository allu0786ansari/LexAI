# Phase 2 — run and report (Windows Command Prompt)

Phase 2 replaces the v0.1 ingestion (FAISS + whole-page chunks) with a
pinned corpus, a parser that understands sections and articles, and a
hybrid Qdrant index. Everything runs in **cmd.exe** with plain Python and pip.
Run the steps below, then push `runlogs\phase2\output\` **and**
`data\manifest.lock.json`. Phase 3 starts only after they are reviewed.

## What changed for you

- **The new criminal codes are downloaded.** BNS, BNSS and BSA (in force since
  1 July 2024) come from India Code; the full IPC and Indian Evidence Act come
  from IIT Kanpur, UNODC and NCW mirrors. Downloads go to `data\raw\` (ignored by
  git). Their SHA-256 checksums are written to `data\manifest.lock.json`, which
  **you commit**: later builds refuse a file whose content changed upstream.
- **The bundled PDFs moved** from `backend\Data\` to `data\bundled\` and are
  checked against `data\bundled\SHA256SUMS`. The incomplete IPC PDF was removed.
- **The index is Qdrant**, stored in `backend\var\qdrant\` (embedded mode, no
  server or Docker needed). Each build creates a new collection and switches to
  it only after checks pass; if anything fails, the previous index stays live.
  Your old `backend\var\index\` (FAISS) is no longer used and can be deleted.
- **One process at a time.** Embedded Qdrant locks its folder. Close any LexAI API
  you started yourself before running `build` or `inspect`. The `smoke` step starts
  and stops its own API.
- **Laptop profile v2.** `setup` notices that `backend\.env` has the Phase 1 profile,
  saves it as `backend\.env.previous` and writes the new one: chunks of at most
  1,400 characters and a 6,000-character passage budget, so 4–5 passages fit in
  the 4K context.
- **Citations name the provision** ("IPC s.302 — Punishment for murder") and say
  whether the law is in force. IPC, CrPC and the Evidence Act are marked
  "repealed 2024-07-01; replaced by BNS/BNSS/BSA; applies to earlier offences".

## 0. Before you start

1. Ollama is running (system tray icon) and Phase 1's models are present.
2. **Internet** for the `fetch` step (five PDFs, a few MB each, from indiacode.nic.in and the
   mirrors). If India Code is slow, re-run just `fetch`; files already downloaded
   and verified are skipped.
3. About 1 GB of free disk space.
4. Close browsers and other heavy apps before `build` and `smoke`.

## 1. Get the branch

```bat
cd /d C:\Users\LENOVO\OneDrive\Desktop\LexAI
git fetch origin
git checkout phase-2-ingestion
git pull
```

## 2. Run everything

```bat
runlogs\phase2\run_phase2.bat
```

| Step | What it does | Log |
| --- | --- | --- |
| `check` | Git state, Python, Ollama, India Code reachability, GPU, RAM, disk, port 8000 | `01_check.log` |
| `setup` | pip install (adds qdrant-client, removes faiss/rank-bm25), writes the v2 laptop profile | `02_setup.log` |
| `quality` | ruff, format check, mypy, 62 tests | `03_quality.log` |
| `models` | Same as Phase 1: models present, embedder and generator respond | `04_models.log` |
| `fetch` | Downloads and pins BNS, BNSS, BSA, IPC, IEA; verifies all 58 bundled files | `05_fetch.log` |
| `build` | Parses every document into sections/articles, embeds them, builds the hybrid index, checks it, makes it live (several minutes) | `06_build.log`, `06_build_report.json` |
| `status` | Shows the live index: collection, model, chunk count per document | `07_status.log` |
| `inspect` | Looks up 8 provisions directly (e.g. BNS s.103, CrPC s.154, Art. 21) and runs 6 test searches | `08_inspect.log` |
| `smoke` | Starts the API, asks about murder, checks streaming, citations, repeal flag, parallel answers | `09_smoke.log`, `09_server.log` |

Optional: `runlogs\phase2\run_phase2.bat outage` (same as Phase 1, log `10_outage.log`).

Re-run a single step by naming it, e.g. `runlogs\phase2\run_phase2.bat fetch build`.

## 3. Send the results

```bat
git add runlogs\phase2\output data\manifest.lock.json
git commit -m "Phase 2 run logs"
git push
```

## What "pass" looks like

| Log | Pass |
| --- | --- |
| `03_quality.log` | every `exit=0`; pytest `62 passed` |
| `05_fetch.log` | `FETCH: all sources present and verified`, `exit=0` |
| `06_build.log` | `BUILD STATUS: live ...`, each document's checks `PASS`, `exit=0`; roughly 4,000–5,500 chunks |
| `07_status.log` | live collection with `nomic-embed-text (768-dim)` and all 63 documents |
| `08_inspect.log` | all 8 lookups `PASS`; search quality at least 4/6; `SUMMARY: ALL PASSED` |
| `09_smoke.log` | `SUMMARY: ALL PASSED`; the murder answer cites BNS s.103 and/or IPC s.302 |

## If something fails

- **`fetch` fails for one document**: the log says which URL and why. Re-run
  `fetch`; a network timeout is usually transient. If a site changed its file,
  the log says "does not match its pinned checksum": send me the log rather than
  using `--update`.
- **`build` says the store is locked**: a LexAI API (or another build) is still
  running. Close it, or restart the PC, and re-run `build`.
- **`build` fails with source or parsing problems**: the live index is unchanged.
  Push the logs; `06_build_report.json` lists the exact problem per document.
- **`smoke` says the index does not match the configuration**: `backend\.env`
  uses a different embedding model from the one the index was built with. Re-run
  `setup` and `build`.
