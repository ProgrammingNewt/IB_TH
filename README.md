# Lab Drive Harness

A collaborative, science-focused agent harness. Several scientists and four LLM agents work on the same experiment through a shared lab drive. The agents read everyone's files through tools, ask the owner of a file when something doesn't add up, write their analysis back to the drive with lineage, and reuse what the team has already confirmed on the next run.

> Integrated Bio take-home, problem 2. Work in progress.

## Status

- [ ] Toy data generator with checks on the planted results
- [ ] Drive, manifest, tools, guardrails
- [ ] LLM gateway with provider fallback
- [ ] Router, Planner, Q&A and Reviewer agents
- [ ] Streamlit UI, one tab per scientist
- [ ] Evals
- [ ] Docker

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```
