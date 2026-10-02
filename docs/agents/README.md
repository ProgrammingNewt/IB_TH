# Agents

Four agents, each with one job and its own loop. The orchestrator passes work between them and makes no model calls itself.

| Agent | One job | Code | Prompt |
|---|---|---|---|
| [Router](router.md) | Decide what kind of message a scientist sent | `harness/agents/router.py` | `ROUTER` |
| [Planner](planner.md) | Pick the next tool, run it, repeat, until it needs another agent | `harness/agents/planner.py` | `PLANNER` |
| [Q&A](qa.md) | Every conversation with a scientist | `harness/agents/qa.py` | `QA_ASK`, `QA_REPORT`, `QA_ANSWER`, `QA_PARSE_OTHER` |
| [Reviewer](reviewer.md) | Pass or fail a draft conclusion | `harness/agents/reviewer.py` | `REVIEWER` |

All prompts are in `harness/agents/prompts.py`.

Not agents:
- [Orchestrator](orchestrator.md) (`harness/harness.py`): passes work between agents and saves state.
- [Protocol extractor](protocol-extractor.md) (`harness/agents/extractor.py`): an LLM inside the `extract_protocol_fields` tool. It reads text but never decides what happens next.

## Who hands off to whom

```
scientist message
      │
      ▼
   Router ──"question_about_findings"──► Q&A answers from findings.md
      │
  "new_analysis"
      ▼
   Planner ◄──────────── answers ──────────── scientists
      │  ▲                                        ▲
      │  └── review failed (problems) ──┐         │
      ├── ask_scientists ──► Q&A writes the questions, run waits
      │                                 │
      └── submit_conclusion ──► Reviewer ┘
                                  │
                                pass ──► findings.md + Q&A report
```

## What all four loops share

Each agent's loop has the same shape: call the model, check the reply in code, and if the reply is wrong, say what's wrong and try again, up to a limit. When the limit is reached, use a safe fallback. Only the Planner's loop calls tools.
