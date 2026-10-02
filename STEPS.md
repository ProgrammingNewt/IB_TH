# Steps

Action items for the Lab Drive Harness take-home, in order. Tick each box when it's done.

## 0. Setup
- [x] Create the local repo folder and skeleton
- [x] Keep the local `.git` (local commits only, no remote)
- [x] Create `.venv` and install dependencies
- [x] First local commit (skeleton + this checklist)

**Now working on:** step 1, toy data.

## 1. Toy data (first, because the tools are tested against it)
- [ ] Seeded generator for `planted_batch_issue/`: `protocol.pdf` (A), `readings.xlsx` (B), `analysis.ipynb` (C), lab SOP text, `manifest.json`
- [ ] Generator for the re-run file (batch 3) used in run 2
- [ ] Generator for `clean/`, where there's no flaw and a real effect
- [ ] The generator checks its own results and exits with an error if any fail:
  - batch 1 alone: p > 0.05
  - batch 1 and batch 2 pooled: drop of at least 30%, p < 0.01
  - batch 3: no effect
  - clean folder: drop of at least 30%, p < 0.05

## 2. Drive, tools, guardrails (no LLM yet)
- [ ] Drive layer: read the manifest, look up file owners, write files with a `.lineage.json`
- [ ] File tools: `parse_xlsx`, `read_notebook`, `extract_protocol_fields` (has an LLM inside, stubbed for now)
- [ ] Analysis tools: `check_consistency`, `filter_dataset`, `run_stats` (each plate compared with its own vehicle wells), `run_notebook`
- [ ] Lab tools: `plan_followup_plate` (vehicle on the same plate), `search_sops`, `load_skill`
- [ ] Guardrails:
  - goal check (required slots filled)
  - analysis blocked while a contradiction is unresolved
  - only a file's owner can confirm facts about it
  - facts scoped to file + plate + batch
  - plate plans need approval
  - step budget
- [ ] Unit tests for every tool and guardrail against the generated data

## 3. LLM gateway
- [ ] One OpenAI-compatible client, configured by `config.yaml`
- [ ] Fallback order OpenAI → DeepSeek → Ollama when a provider refuses
- [ ] A fake provider that always refuses, for tests and the demo
- [ ] Log every call (provider, model, tokens) to `trace/`

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
