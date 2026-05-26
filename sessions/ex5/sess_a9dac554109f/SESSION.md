# Session sess_a9dac554109f

**Scenario:** edinburgh-research
**Created:** 2026-05-26T06:36:53.388324+00:00

## Your task

(The loop half reads this file on every turn. The initial task description
has been written below by the orchestrator when the session was created.
Additional per-session instructions — constraints, identity, voice — can
be added by the scenario author.)

## Task description

Research an Edinburgh pub and produce an HTML event flyer.

Context:
  - party size: 6
  - date: 2026-04-25 (a Saturday)
  - time: 19:30
  - area: near Haymarket station, Edinburgh

REQUIRED tool sequence (all four tools MUST run, in order):
  1. venue_search(near='Haymarket', party_size=6, budget_max_gbp=800)
  2. get_weather(city='edinburgh', date='2026-04-25')
  3. calculate_cost(venue_id=<chosen pub's id>, party_size=6,
                    duration_hours=3, catering_tier='bar_snacks')
  4. generate_flyer(event_details={...})  <-- MUST be called
  5. complete_task(result={'flyer': 'workspace/flyer.html', ...})

Do NOT call complete_task until you have called generate_flyer. The scenario is graded by the existence of workspace/flyer.html, not by your final text response. The flyer is HTML — exact tool names and argument shapes are in each tool's docstring; call them exactly as described.

## Constraints

- Be honest when you do not know something.
- Prefer reading memory over guessing.
- When the task is ambiguous, ask for clarification rather than inventing an answer.
