# Steps

Action items for the Lab Drive Harness take-home, in order. Tick each box when it's done.

## 0. Setup: done
- [x] Create the local repo folder and skeleton
- [x] Keep the local `.git` (local commits only, no remote)
- [x] Create `.venv` and install dependencies
- [x] First local commit (skeleton + this checklist)

**Now working on:** step 6, evals.

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

## 3. LLM gateway: done
- [x] One OpenAI-compatible client (`harness/gateway/gateway.py`), configured by `config.yaml`
- [x] Providers in order: `model_provider_1` (DeepSeek) → `model_provider_2` (Cerebras) → `local_model` (Ollama, only when `LOCAL_MODEL=1`). A provider without a key is skipped
- [x] Each agent has a model tier (small or large). No per-call output cap is sent, so each provider uses its own maximum
- [x] Run token budget of 1,000,000 (prompt + completion across the whole run). Past it, the gateway stops and the loop reports what it has
- [x] On a refusal (content-filter stop, policy error, or a refusal reply) it moves to the next provider at once and benches the refusing one for the session
- [x] Timeouts, connection errors, rate limits (429) and server errors (5xx) are retried 5 times on the same provider, waiting 1, 2, 4, 8, 16 s, before falling back. They don't bench the provider. A bad key (401) falls back without retrying
- [x] Fake providers for tests and the demo (`harness/gateway/fakes.py`): scripted, refusing (3 styles), failing
- [x] Every call is logged (agent, provider, model, tokens, latency, outcome) through a JSONL tracer
- [x] `python -m harness.gateway` checks each provider on its own and reports ok, skipped, refused or error
- [x] Tests: `tests/test_gateway.py` (15)
- [x] Keys added to `.env` (gitignored). `python -m harness.gateway` reports ok for DeepSeek and Cerebras
- [x] Model names confirmed, and all four models (`deepseek-flash`, `deepseek-v4-pro`, `qwen-3.8-27b`, `gpt-oss-120b`) made a correct `parse_xlsx` tool call

## 4. Agents and the loop: done
- [x] Router (`harness/agents/agents.py`): new analysis, question about findings, or other. Never sets the threshold
- [x] Planner loop (`harness/harness.py`): 10 tools plus `ask_scientists` (pauses the run) and `submit_conclusion` (goes to review)
- [x] Q&A agent: writes each question for the named person, reports results, answers questions about findings, turns an "other" reply into a value
- [x] Reviewer: code checks first (every number matches its source file; no overclaiming, and causal verbs flagged only when the compound is the subject), then the LLM check. At most 2 revisions
- [x] LLM protocol extractor, checked field by field, with the regex as fallback
- [x] Pause and resume: each run's whole conversation is saved in `runs/`, and it resumes once every question is answered
- [x] Questions and approvals in `questions/`; inboxes in `messages/`. Contradictions go to the owner who decides, copied to the other owner. Slots go to the requester. Plate plans go to the requester for approval
- [x] Each run has its own 1M token budget, kept across pause and resume
- [x] Code renders `findings.md` (plus history and notes for owners) from the reviewed draft, including "What changed since the last run"
- [x] Run 2: an upload starts a new run for each existing goal, reusing confirmed facts
- [x] Scripted end-to-end test (`tests/test_harness.py`): run 1 with two owners, approval, run 2. 36 tests pass
- [x] Live run on DeepSeek (`scripts/live_planted.py`), all goal conditions met:
  - asked only the threshold (to A) and the plate P2 question (to B, A copied); A's answer about B's file would only have been a comment
  - run 1: P2 excluded by B, 2.4% drop, p = 0.632, **not a hit**; re-run plan with vehicle on the plate, approved by A; note left for C
  - run 2: no new questions, P3 added, 3.3% drop, p = 0.267, still not a hit, "what changed" section filled in
  - about 44k tokens per run, about 3 min for both runs
- [x] Fixed after the first live run: the Reviewer now sees the SOP text (it had rejected a correct claim citing the SOP), and the Planner no longer states a plan's approval status
- [x] Refactor, one loop per agent: `Router`, `Planner`, `QA` and `Reviewer` each have their own class and `run()` loop in `harness/agents/`. The orchestrator (`harness/harness.py`) only passes work between them and saves state; it makes no model calls. The Planner hands control back with `ask`, `submit` or `failed`
- [x] Docs: `docs/agents/` has a one-page loop description for each agent, the orchestrator and the protocol extractor. Prompts are all in `harness/agents/prompts.py`
- [x] After the refactor: 36 tests pass, and the live run gives the same results (run 1 not a hit after 2 questions, run 2 no questions)
- [x] Only the Q&A agent writes to scientists. The copy to other owners and the note to a file's owner were the Planner's raw text; they're now Q&A tasks (`copy`, `note`). The threshold buttons' labels are written by Q&A from the values (`labels`, rejected if a number is missing), with a fixed format as the fallback. The 3 system notices are fixed templates in `qa.py`. 38 tests pass
- [x] Live check of the new Q&A tasks: labels came back with their numbers. The copy to A first said "Ben *asked*" when Ben was the one being asked; fixed by naming the input `question_sent_to` and saying so in `QA_COPY`. 3 of 3 live retries correct

## 5. Streamlit UI: done
- [x] `streamlit run ui/app.py`. One tab per scientist with `?as=A`, `?as=B`, `?as=C`, plus a drive picker
- [x] Harness work runs in a background thread (one job at a time, shared by every tab), so a 1-2 minute Planner run doesn't freeze the page. Each tab refreshes itself when a job finishes
- [x] Tabs: Ask (send a request, goals, inbox), My questions (answer buttons, "something else" box, plate map for approvals, what you were copied on), Drive (team files with previews, harness-made files with lineage, upload), Findings (findings.md, plate plans, notes, earlier versions), Trace (runs, every model and tool call, the Planner's conversation)
- [x] Demo switch in the sidebar: "Provider 1 refuses biology" swaps provider 1 for the refusing fake, to show the fallback live
- [x] Demo upload button for the scenario's re-run file
- [x] Toy-data playground page: pick a scenario and seed, Generate (same `datagen` code and checks), readings plotted per plate, summary, run metadata, answer key
- [x] Bound to `localhost` by `.streamlit/config.toml`
- [x] Tests: `tests/test_ui.py` renders both pages as every scientist with no exceptions, and checks the job runner. 45 tests pass. The server boots and serves the page
- [ ] You: click through the demo in a browser (steps in the README). The live clicks haven't been tested headless, because they make real model calls

### 5b. Removing files, and any file type
- [x] Remove a team file (Drive tab, "Your files"): only its owner, with an inline confirmation. The file moves to `trash/` and is logged in the manifest. Rows, facts and open contradictions derived from it are dropped. Findings get an "out of date" banner with a Re-evaluate button; no run starts by itself
- [x] Upload any file type. Same name: only its owner can replace it (old version to trash). Checkbox to upload without re-running
- [x] New tools: `read_table` (.csv, .tsv, any .xlsx) and `read_document` (.pdf, .docx, .txt, .md, .json, .py…). Files nothing can read (e.g. images) fail with a clear reason, and the Planner is told not to guess
- [x] Previews for the new types in the Drive tab (tables, Word text, images, code)
- [x] Findings list every team file the analysis didn't read, with the reason, from code
- [x] Live check: with a meeting-notes .txt and a .png on the drive, the Planner read the notes with `read_document` on its own and still reached the right verdict. It skipped the image without saying so, which is why the "Files not used" section is built in code
- [x] Headless click-through: Ben removes `readings.xlsx`, it lands in trash, and the out-of-date banner appears. 51 tests pass
- [x] Found while playing the scientists: Ben's question ended "How should we handle P2?" without naming the decision. Q&A questions must now end with a direct question naming the options (checked in code: a question mark, plus "exclude"/"keep" for contradictions and "approve" for plans), and the inbox shows each open question's options. 3 of 3 live rewrites correct. 52 tests pass
- [x] "Start fresh" in the sidebar: clear the harness's work (keeps all team files, uploads included), reset to the generated files (same scenario and seed, recorded in the manifest), or clear my inbox. Each confirms first and is disabled while a job runs. Headless click-through of all three. 53 tests pass
- [x] Live progress, like a chat: the Ask tab shows each request with a reply that updates every second. The first line ("Got it. Working on: …") is written before any model call; then one line per step (reading each file, checking plates, writing questions, waiting for named people, someone answered, statistics, review, done). Lines are fixed Q&A templates, so they're instant. Runs started by answers and uploads update the same or a new thread, and the requester sees both. Measured live: first reply 0.5 s after Enter (was 31 s), a new line every few seconds. 54 tests pass
- [x] Found while playing: Ben was pinged, but his tab opened on the `clean` drive (the first alphabetically). Tabs now open on the last-used drive (falling back to `planted_batch_issue`), and each scientist sees a live notice of questions waiting for them, checked every 2 s, with a toast when a new one arrives

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
