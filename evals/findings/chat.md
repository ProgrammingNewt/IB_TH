# Chat mid-run and provider fallback: findings

Cases: `evals/cases/chat.json`. Results: `runs/evals/chat/<case>/result.json`. All four cases ran live on 2026-10-02. The runner reported 3 pass and 1 fail, but reading the replies shows more problems than the checks caught.

| case | what you asked | what happened | pass/fail | severity |
|---|---|---|---|---|
| chat_ana_waiting | Ana while waiting: "why is Ben being asked?" | Routed as question_about_run. Correct and clear answer about P2 (48 h, no vehicle wells), with no ids. | pass | - |
| chat_ana_waiting | Ana: "what will the new plate look like?" | Said no plate plan exists yet, which is true at that point. But it also told her "Ben would be the person to ask". | pass (minor) | low |
| chat_ana_waiting | Ana: "is the analysis only using P1?" | Answered "No" and said "No P1 is mentioned anywhere in the run state". It should have said P1 only, pending Ben's decision. The check passed only because the word "P1" appears. | fail (checks said pass) | medium |
| chat_ben_chen_waiting | Ben: "what happens if I keep P2?" | Good answer: P2 would be included, it has no vehicle comparison, and the call is his to make. | pass | - |
| chat_ben_chen_waiting | Chen: "is my notebook wrong?" | Routed as `other` and got the canned "Try something like 'Is ABC-12 a hit?'" reply. | fail | medium |
| chat_ben_chen_waiting | Ana after the run: "what did Ben decide about P2?" | Correct: exclude, P1 only, rerun plate will be added. | pass | - |
| chat_correction_and_new_compound | Ana: "actually make the threshold 10%" | Nothing changed silently: facts stayed at 30%/p<0.05 and the verdict stayed "not a hit". The reply explained that 10% was not one of the options and that she has to give it as her answer. | pass | - |
| chat_correction_and_new_compound | Ana: "is XYZ-99 a hit?" | Routed as new_analysis and was not confused with ABC-12. Only 1 goal, because the runner does not start chat analyses. | pass | - |
| chat_correction_and_new_compound | Internal ids in any reply | No f-/q-/c- ids appeared in any reply across all 4 cases. | pass | - |
| chat_fallback_provider | provider 1 refuses; Ana: "why is Ben being asked?" | Fallback worked (`Cerebras:ok, DeepSeek:refused`). Correct, short answer. | pass | - |
| chat_fallback_provider | Ana after the run finished: "so is ABC-12 a hit?" | Routed as **new_analysis**, so the UI would start a second analysis instead of answering from the findings. | fail | medium |

## Problems found

1. **The Q&A agent cannot see which plates the analysis will use (medium).** Asked "is the analysis only using P1?", it replied: "No — your analysis hasn't run yet, so it isn't using P1 or any other plate ... No P1 is mentioned anywhere in the run state; if you expected it to be in play, Ben is the person to ask." That is misleading: P1 is the clean plate and is always used. The run state given to Q&A apparently lists only contested plates. *Fix:* add the planned plate list (P1, plus P2 pending Ben's answer) to `run_state` before the run.
2. **A question about a different person's file falls through to the canned reply (medium).** Chen (who owns the notebook flagged in a note) asked "is my notebook wrong?" and got "I can analyse an experiment's data on this drive, or answer questions about its findings. Try something like "Is ABC-12 a hit?"". Chen has no question or cc on the goal, so `active` is empty, and the `_looks_like_question` fallback only runs when an analysis is active. *Fix:* count notes or notices sent to a person as involvement in `active_analyses` / `_goal_for_questions`, or apply the question fallback even with no active run.
3. **A follow-up about the same compound after the run finishes is routed as a new analysis (medium).** With the fallback provider active, "so is ABC-12 a hit?" came back as `new_analysis` even though findings.md existed. The same-compound guard only applies while a run is active. *Fix:* if findings already exist for the same compound, route the message to question_about_findings, or confirm with the person before re-running.
4. **Wrong person suggested (low).** For "what will the new plate look like?" it said "If you were expecting a proposed plate plan, Ben would be the person to ask". In fact the harness drafts the plan and Ana approves it. *Fix:* the Q&A prompt should say that the replacement plate is drafted after Ben's decision and sent to the requester for approval.
5. **Eval weakness.** `chat_reply_contains` passed "P1" on a reply that said "No P1 is mentioned". Keyword checks on chat replies can pass on wrong answers. *Fix:* add a `chat_reply_excludes` check, or use an LLM-judged check, for factual chat answers.

## What worked
- No second analysis was started, and no facts or runs changed from chat in any case.
- The 10% correction was not applied silently.
- XYZ-99 was treated as a new request.
- No internal ids leaked.
- The provider fallback served both routing and Q&A.
