# Steps

Action items for the Lab Drive Harness take-home, in order. Tick each box when it's done.

## 0. Setup: done
- [x] Create the local repo folder and skeleton
- [x] Keep the local `.git` (local commits only, no remote)
- [x] Create `.venv` and install dependencies
- [x] First local commit (skeleton + this checklist)

**Now working on:** step 4, agents and the loop.

## 1. Toy data (first, because the tools are tested against it): done
- [x] Seeded generator for `planted_batch_issue/`: `protocol.pdf` (A), `readings.xlsx` (B), `analysis.ipynb` (C, executed so it has saved outputs), lab SOP, `manifest.json`
- [x] Generator for the re-run file (batch 3), kept in `planted_batch_issue.incoming/` until run 2 uploads it
- [x] Generator for `clean/`, where there's no flaw and a real effect
- [x] The generator checks its own results and exits with an error if any fail (default seed 20261002):
  - batch 1 alone: drop 2.4%, p = 0.63 (no effect)
  - batch 1 and batch 2 pooled: drop 37.3%, p = 1e-05 (the fake hit)
  - batches 1 + 3, each plate compared with its own vehicle: drop 3.3%, p = 0.27 (no effect)
  - clean folder: drop 35.6%, p = 1e-19 (real hit)
- [x] Answer key for each drive (`<drive>.expected.json`), kept outside the drive so the harness can't read it
- [x] Tests: `tests/test_datagen.py`
- [ ] Later: try a few other seeds to make extra planted and clean drives

## 2. Drive, tools, guardrails (no LLM yet): done
- [x] Drive layer (`harness/drive/store.py`): manifest, file owners, uploads, `.lineage.json` on every derived file, facts and contradictions
- [x] File tools: `parse_xlsx`, `read_notebook`, `extract_protocol_fields` (uses an offline regex extractor until the LLM version arrives in step 4)
- [x] Analysis tools: `check_consistency`, `filter_dataset`, `run_stats` (each plate compared with its own vehicle wells, plates without vehicle refused), `run_notebook`
- [x] Lab tools: `plan_followup_plate` (vehicle on the same plate, pending approval), `search_sops`, `load_skill` + `skills/batch_qc.md`
- [x] Guardrails (`harness/guardrails/rules.py`, all code):
  - unknown tool or bad arguments are rejected
  - step budget
  - `filter_dataset` and `run_stats` blocked while a contradiction is open
  - `run_stats` blocked until the goal has every required slot, and it must use the requester's threshold
  - plate plans come back as pending approval
- [x] Only a file's owner can confirm a fact about it (anyone else's answer is kept as a comment). Facts are scoped to file + plate + batch
- [x] Tool registry (`harness/tools/registry.py`): the one entry point that runs a tool through the guardrails, plus the OpenAI-format tool list for the Planner
- [x] Tests: `tests/test_tools.py` walks run 1 and run 2 by hand and checks every number against the answer key
- [x] Checkpoint before step 3: 17/17 tests pass, plus a manual walk of run 1 on a scratch drive. Checked the files it leaves behind: the contradiction (P2: 48 h and 0 vehicle wells, B decides), B's fact scoped to file + plate, stats on P1 only (2.4% drop, p = 0.63, not a hit), lineage linking the analysis set to fact f-001, and a re-run plan pending approval. Nothing needed fixing
- Not tested yet, and checked in later steps: the LLM-backed protocol extractor (step 4), pause and resume (step 4), and a clean install from a fresh clone (step 7, Docker)

## 3. LLM gateway
- [x] One OpenAI-compatible client (`harness/gateway/gateway.py`), configured by `config.yaml`
- [x] Providers in order: `model_provider_1` (DeepSeek) → `model_provider_2` (Cerebras) → `local_model` (Ollama, only when `LOCAL_MODEL=1`). A provider without a key is skipped
- [x] Each agent has a model tier (small or large) and an output budget
- [x] On a refusal (content-filter stop, policy error, or a refusal reply) it moves to the next provider and benches the refusing one for the session. Timeouts and errors also fall back but don't bench
- [x] Fake providers for tests and the demo (`harness/gateway/fakes.py`): scripted, refusing (3 styles), failing
- [x] Every call is logged (agent, provider, model, tokens, latency, outcome) through a JSONL tracer
- [x] `python -m harness.gateway` checks each provider on its own and reports ok, skipped, refused or error
- [x] Tests: `tests/test_gateway.py` (10)
- [ ] Add keys to `.env` (you do this; never paste keys in chat), then run `python -m harness.gateway`
- [ ] Confirm the model names in `config.yaml` against each provider's current list, and that the chosen models support tool calls

## 4. Agents and the loop
- [ ] Router: works out what kind of message it is and pulls goal slots from it
- [ ] Planner: the tool-calling loop, re-planning from each tool's results
- [ ] Q&A agent: asks named owners with answer choices, delivers reports, answers questions about past findings
- [ ] Reviewer: checks every claim against evidence, at most 2 revisions
- [ ] Pause and resume: a run's state is saved to the drive while it waits for an answer
- [ ] Code renders `findings.md` and the notes from the JSON the Reviewer approved
- [ ] Run 2: an upload is treated as new data for the open goal, and saved facts are reused

## 5. Streamlit UI
- [ ] One tab per scientist (`?as=A`, `?as=B`, `?as=C`)
- [ ] Views: Drive, My questions (with answer choices), Approval inbox, Findings, Trace
- [ ] Upload a file (for the run-2 demo)
- [ ] Toy-data playground page:
  - generate a drive from the UI (pick the scenario and seed, click Generate), calling the same `datagen` code
  - plot the readings per plate (vehicle vs ABC-12, coloured by batch) and show the run metadata
  - show the scenario's checks (pass or fail) and the answer key, so it's easy to experiment with different seeds

## 6. Evals
- [ ] `planted_batch_issue/`: asked the right people, correct stats, no causal wording, re-run plan with vehicle on the plate
- [ ] `clean/`: no questions to owners, agrees with the notebook
- [ ] Vague goal: only the threshold is asked, and before `run_stats`
- [ ] Refusing provider: same result, and the switch shows in the trace
- [ ] Scripted scientist answers so eval runs repeat exactly

## 7. Packaging and write-up
- [ ] `Dockerfile` + `docker-compose.yml`, tested with a clean build
- [ ] A non-Docker path (`make demo`)
- [ ] README:
  - the problem
  - architecture diagram
  - agents and tools
  - the take-home's four questions
  - the skills idea for provider bans
  - limitations and what I'd do next
- [ ] Record or script the demo: run 1 with two scientists, then run 2
- [ ] Hand-off (only when you say so). Pick one:
  - keep history: create an empty private GitHub repo, then `git remote add origin <url>` and `git push -u origin main`
  - clean copy: `git clone ~/integrated-bio-harness <new folder>` and push that
  - fresh history: squash the commits into one before pushing
- [ ] Email the repo link to alexg@integratedbio.com and felix@integratedbio.com
