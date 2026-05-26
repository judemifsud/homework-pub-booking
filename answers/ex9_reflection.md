# Ex9 — Reflection

## Q1 — Planner handoff decision

### Your answer

In my Ex7 run (sessions/ex7/sess_8b16caa616bf), the planner produced a
handoff from the loop half to the structured half for confirmation under
policy rules. The concrete evidence is the trace event in
`logs/trace.jsonl` showing `handoff_to_structured` with reason
"passing to structured half for confirmation under policy rules".

This decision is advisory, not physical. The orchestrator respects it only
because both halves are wired up. If only a loop half existed (as in
research_assistant), a structured handoff would still be requested but it
would be unfulfilled. That's failure mode #4 from the course slides.

The broader lesson: the planner makes an architectural decision based on
prose interpretation. The rules need to be explicit so that the LLM cannot
misinterpret them in the structured half's Python — and then language
ambiguity matters less.

### Citation

- sessions/ex7/sess_8b16caa616bf/logs/trace.jsonl
- sessions/ex7/sess_8b16caa616bf/logs/tickets/tk_ff67586b/raw_output.json

---

## Q2 — Dataflow integrity catch

### Your answer

In the Ex5 session `sessions/ex5/sess_c1b88436de4f`, the integrity check is
meant to prevent the flyer from inventing numbers that don't actually come
from the tool outputs. In this run, `logs/tool_call_log.json` shows
`calculate_cost(haymarket_tap, party=6, duration=3, tier=bar_snacks)`
returned `total_gbp: 556` and `deposit_required_gbp: 111`.
The generated flyer in `workspace/flyer.html` also records `£556` total and
`£111` deposit.

A plausible subtle failure would be if the LLM had written `Total: £560`
and `Deposit: £112` in the flyer despite the tool log proving the true
values were `£556` and `£111`. A human reviewer could easily miss that
because `£560` / `£112` looks reasonable, but `verify_dataflow` would flag
those specific facts as unverified against `_TOOL_CALL_LOG`.

The core point is that the check is not doing plausibility judgment; it is
checking the exact inclusion for each value in the final flyer.

### Citation

- sessions/ex5/sess_c1b88436de4f/logs/tool_call_log.json
- sessions/ex5/sess_c1b88436de4f/logs/trace.jsonl
- sessions/ex5/sess_c1b88436de4f/workspace/flyer.html

---

## Q3 — Removing one framework primitive

### Your answer

If I were shipping this agent next week, the first production failure I would expect is a structured-half dependency failure surfaced through the ticket state machine. The bridge can generate a valid forward handoff, but if the structured half is unavailable, the session transitions back to loop with a rejection reason instead of committing the booking.

This is exactly what the example logs show in `sessions/ex7/sess_e5cc79683c99/logs/trace.jsonl`: the bridge records `session.state_changed` from `structured` to `loop` with `rejection_reason: "rasa unreachable: <urlopen error [Errno 61] Connection refused>"`. That line proves the ticket/state-machine machinery is the primitive that surfaces a real production failure mode.

The session directories are the key for working through production failures, tracing back logs, and understanding what went wrong. They hold information akin to git commits and allow us to trace the evolution of the session to determine when, where and how the issue manifested. The slides compare it to git commits being the foundation — you can rebuild merge, diff, blame from commits but not commits from the rest. Session directories are commits.

### Citation

- sessions/ex7/sess_e5cc79683c99/logs/trace.jsonl
- sessions/ex7/sess_e5cc79683c99/*/ — the directory itself
