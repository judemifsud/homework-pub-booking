# Ex9 — Reflection

## Q1 — Planner handoff decision

### Your answer

In my Ex7 run (sessions/sess_8b16caa616bf), the planner's second
subgoal was sg_2 "commit the booking under policy rules" with
assigned_half: "structured". The signal that drove this was the task
text naming a deterministic constraint — "under policy rules".
Sovereign-agent's DefaultPlanner is prompted with the list of
available halves and their purposes; when subgoal description
mentions rules/policy/limits, the planner prefers structured.

This decision is advisory, not physical. The orchestrator respects
it only because both halves are wired up. If only a loop half
existed (as in research_assistant), a subgoal assigned to structured
would go to the void. That's failure mode #4 from the course slides.

The broader lesson: the planner makes an architectural decision
based on prose interpretation. The rules need to be explicit so that the LLM
cannot misinterpret ie in the structured half's Python — and then language
ambiguity no longer matters.

### Citation

- sessions/ex7/sess_8b16caa616bf/logs/tickets/tk_*/raw_output.json
- sessions/ex7/sess_8b16caa616bf/logs/trace.jsonl:23

---

## Q2 — Dataflow integrity catch

### Your answer

During Ex5 development my integrity check caught a subtle fabrication
that manual review missed. In session the flyer
claimed "Total: £560" and "Deposit: £112" — plausible numbers that
followed the deposit formula in catering.json. I skimmed and moved on.

verify_dataflow returned ok=False with unverified_facts=['£560','£112'].
The trace showed calculate_cost returned total_gbp=540, deposit=0. The
real total was £540 under the £300 deposit threshold. The LLM had
written "£560" plausibly — close enough that a human reviewer wouldn't
notice without cross-referencing.

The check caught it because it compared against ground truth in
_TOOL_CALL_LOG, not against "does this look reasonable." I would add in boundary tests around the limits (just over and just below) along with excessive values e.g. £9999 and confirm it's caught.

### Citation

- ex5/sessions/sess_8b16caa616bf/workspace/flyer.html
- ex5/sessions/sess_8b16caa616bf/logs/trace.jsonl

---

## Q3 — Removing one framework primitive

### Your answer



The session directories are the key for working through production failures, tracing back logs, and understanding what went wrong. They hold information akin to git commits and allow us to trace the evolution of the session to determine when, where and how the issue manifested. The slides compare it to git commits being the
foundation — you can rebuild merge, diff, blame from commits but
not commits from the rest. Session directories are commits.

### Citation

- sessions/*/ — the directory itself
- sessions/*/logs/trace.jsonl
