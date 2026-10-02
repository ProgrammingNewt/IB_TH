# Lab Drive Harness

A collaborative, science-focused agent harness. Several scientists and four LLM agents work on the same experiment through a shared lab drive. The agents read everyone's files through tools, ask the owner of a file when something doesn't add up, write their analysis back to the drive with lineage, and reuse what the team has already confirmed on the next run.

> Integrated Bio take-home, problem 2. Work in progress.

## Status

- [x] Toy data generator with checks on the planted results
- [x] Drive, manifest, tools, guardrails
- [x] LLM gateway with provider fallback
- [x] Router, Planner, Q&A and Reviewer agents
- [x] Streamlit UI, one tab per scientist
- [ ] Evals
- [ ] Docker

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # add DEEPSEEK_API_KEY and/or CEREBRAS_API_KEY
python -m harness.gateway       # check the providers answer
streamlit run ui/app.py         # http://localhost:8501
```

Open one tab per scientist: `http://localhost:8501/?as=A` (Ana, biologist, asks the question), `?as=B` (Ben, screening, owns the readings), `?as=C` (Chen, data science, owns the notebook).

The demo, in the UI:
1. As Ana, ask "Is ABC-12 a hit?" on the `planted_batch_issue` drive.
2. As Ben, answer the plate P2 question. As Ana, pick a threshold. The run resumes.
3. As Ana, approve the re-run plate plan. Read the findings; Chen gets a note on the notebook.
4. As Ben, upload the demo re-run file (Drive tab). Run 2 asks nothing and updates the findings.
5. Optional: in the sidebar, turn on "Provider 1 refuses biology" and watch the Trace tab fall back to provider 2.

The toy-data playground page generates new drives from a scenario and a seed, and plots their readings.
