# Lab Drive Harness

An agent harness for a team of scientists working on the same experiment. They share a drive. The agents read everyone's files, ask the right person when something doesn't add up, and write their analysis back to the drive with a record of where every number came from.

Built for the Integrated Bio take-home, problem 2: *a collaborative, science-focused agent harness.*

---

## The demo problem

Three scientists share a drive:

| Who | File | What it says |
|---|---|---|
| **Ana** (biologist) | `protocol.pdf` | "Does ABC-12 reduce the senescence marker vs vehicle? Treat for 72 h. Every plate has its own vehicle wells. Hit criteria: to be agreed." |
| **Ben** (screening) | `readings.xlsx` | Plate P1 (72 h, vehicle + ABC-12). Plate P2 (**48 h, ABC-12 only, no vehicle**). |
| **Chen** (data science) | `analysis.ipynb` | Pools both plates: "ABC-12 drops the marker 37%, p = 1e-05. **Hit.**" |

Ana asks: **"Is ABC-12 a hit?"**

The catch sits *between* their files. P2 ran too short and has no vehicle wells, so pooling it fakes a drop. Using P1 alone, the drop is **2.4% (p = 0.63): not a hit.** No single scientist can see this from their own file.

What the harness does:

1. Reads all three files, notices "hit criteria: to be agreed", and asks Ana what counts as a hit.
2. Spots that P2 breaks the protocol, and asks **Ben**, because it's his data, whether to exclude it.
3. Once both answer, analyses P1 against its own vehicle wells: not a hit.
4. Drafts a replacement plate (72 h, vehicle on the plate) and asks Ana to approve it.
5. Writes the findings, and leaves Chen a note explaining why the notebook got 37%.
6. When Ben uploads the replacement plate, it re-runs on its own, asks nobody anything again, and updates the findings.

---

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # add DEEPSEEK_API_KEY and/or CEREBRAS_API_KEY
python -m harness.gateway       # check the providers answer
streamlit run ui/app.py         # http://localhost:8501
```

Open one tab per scientist: `?as=A` (Ana), `?as=B` (Ben), `?as=C` (Chen).

- **Ana:** ask "Is ABC-12 a hit?" in the chat.
- **Ben:** answer the plate P2 question in ❓ My questions.
- **Ana:** pick a threshold, then approve the plate plan. Read the Findings tab.
- **Ben:** upload the demo re-run file (Drive tab). Run 2 updates the findings.
- Anyone can ask about the analysis while it runs, e.g. "is the analysis only using P1?"

**Start fresh → Clear everything** in the sidebar resets every drive. Restart the server after changing anything in `harness/`.

Other commands:

```bash
pytest                                                              # offline tests, no API keys needed
python -m datagen                                                   # rebuild the toy drives
python -m evals.runner evals/cases/core.json --out runs/evals/core  # live evals
```

---

## How it works

```
 scientists ──► Router ──► Planner ◄──── answers ──── scientists
                  │          │  ▲
                  │          │  └── review failed ──┐
                  │          ├── asks ──► Q&A writes the questions, run pauses
                  │          └── submits ──► Reviewer ┘
                  │                            │
                  └──► Q&A (questions          pass ──► findings + Q&A report
                       about the analysis)
```

Four agents, each one LLM with one job and its own loop:

| Agent | Job |
|---|---|
| **Router** | Works out what a message is: a new analysis, a question, or something else. Copies terms exactly as typed. |
| **Planner** | Calls tools, reads their results, and decides the next step. Never reads a file itself, never talks to a person. |
| **Q&A** | The only agent that talks to scientists: questions, answers, reports, live progress. |
| **Reviewer** | Checks the conclusion against the evidence before anyone sees it. |

The parts that don't trust the LLMs are plain code:

- **Tools** do the work: parse files, check plates against the protocol, run the statistics, draft plates. No LLM touches a file.
- **Guardrails** run before every tool. Analysis is blocked until the threshold is agreed and every contradiction is settled, and statistics must use the requester's threshold and compound.
- **Checks on every agent reply:** numbers must match their source file; the Router can't "correct" a typo; Q&A must ask the question first and can't quote internal ids; nothing claims causation.
- **The drive is the memory.** Facts, questions, answers, runs and findings are files, so a run can pause for hours, resume, and be audited. Every derived file records what it was made from.

More detail: [`docs/agents/`](docs/agents/) has one page per agent with its loop in pseudocode. All prompts are in [`harness/agents/prompts.py`](harness/agents/prompts.py).

---

## The take-home's questions

**Collaborative, not git?** Git merges lines of code. Here the unit is a *claim*: each fact records who confirmed it, questions go to whoever owns the data, and only the owner of a file can confirm facts about it. Later runs reuse what the team already confirmed.

**What happens when the prompt is underspecified?** The harness asks, and only for what the files don't already say. "Is ABC-12 a hit?" leaves out the threshold, so it asks for that. Any name it can't find in the files ("acb 12", "IL-6", "untreated cells") is asked about first, with the files' options, and never substituted.

**What if a provider bans biology?** Models are swappable in `config.yaml`: DeepSeek, then Cerebras, then a local model. A refusal benches that provider for the session and the same call goes to the next one. Timeouts retry 5 times before moving on. The sidebar has a switch that makes provider 1 refuse, to show it live.

**Skill, tool, CLI, MCP?** A *tool* is one typed function the Planner can call (`run_stats`). A *skill* is a markdown procedure it can load (`skills/batch_qc.md`). The harness runs from the UI or from Python (`Harness.message`). An MCP server would expose the same tool registry to Claude Code; it isn't built.

---

## Evals

<!-- EVALS -->

---

## Limits

- **One toy experiment.** Two scenarios (planted, clean), one seed each. The structure is general, but it's only been exercised on this data.
- **Not production.** No accounts or auth, a local folder instead of Google Drive, one process.
- **Corrections mid-run aren't supported.** You can ask about an analysis while it runs, but "change the threshold to 10%" won't change a run in progress.
- **The Reviewer uses the same model family as the Planner**, so they can share blind spots. The code checks don't.

---

## Repo map

| Path | What |
|---|---|
| `harness/agents/` | The four agents and their prompts |
| `harness/harness.py` | The orchestrator: passes work between agents, keeps state |
| `harness/tools/` | Tools and their registry |
| `harness/guardrails/` | The checks that run before every tool |
| `harness/gateway/` | Model providers, fallback, retries, fakes for tests |
| `harness/drive/` | The shared drive: files, owners, facts, lineage |
| `datagen/` | Builds the toy drives, and checks their planted story holds |
| `ui/` | The Streamlit app |
| `evals/` | Live eval runner, cases and findings |
| `tests/` | Offline tests |
| `docs/agents/` | One page per agent |
| `STEPS.md` | The build log: every step, and every bug found while testing |
