"""Ex5 — Edinburgh research scenario entrypoint.

Usage:
    make ex5            # offline, FakeLLMClient
    make ex5-real       # uses Nebius (burns tokens)

What's different from a pure scaffold:
  * `Config.from_env()` is used for --real mode so your `.env` models win
  * `example_sessions_dir()` gives us tempdir-offline, persistent-real
  * A preflight checks whether your TODOs are implemented and prints
    a friendly message instead of letting the framework crash cryptically
"""

from __future__ import annotations

import asyncio
import json
import sys

# ---------------------------------------------------------------------------
# Patch _react_loop to exit immediately when complete_task succeeds.
#
# The loop already does this for handoff_to_structured (line 297 in
# executor/__init__.py). complete_task needs identical treatment.
# We wrap the module-level function rather than subclassing DefaultExecutor
# because execute() calls _react_loop directly — subclass method overrides
# are bypassed entirely.
# ---------------------------------------------------------------------------
import sovereign_agent.executor as _executor_module
import sovereign_agent.halves.loop as _loop_module
from sovereign_agent._internal.llm_client import (
    FakeLLMClient,
    OpenAICompatibleClient,
    ScriptedResponse,
    ToolCall,
)
from sovereign_agent._internal.paths import example_sessions_dir
from sovereign_agent.executor import DefaultExecutor
from sovereign_agent.halves.loop import LoopHalf
from sovereign_agent.planner import DefaultPlanner
from sovereign_agent.session.directory import create_session
from sovereign_agent.tickets.ticket import list_tickets

from starter.edinburgh_research.integrity import (
    _TOOL_CALL_LOG,
    clear_log,
    verify_dataflow,
)
from starter.edinburgh_research.tools import build_tool_registry

_original_react_loop = _executor_module._react_loop


async def _patched_react_loop(executor, subgoal, session, max_turns):
    """_react_loop wrapper that hard-exits after complete_task succeeds."""
    from sovereign_agent.executor import ExecutorResult

    tools_as_openai = _executor_module._registry_to_openai_tools(executor.tools)
    from sovereign_agent._internal.llm_client import ChatMessage

    messages = [
        ChatMessage(role="system", content=executor.system_prompt),
        ChatMessage(
            role="user",
            content=(
                f"SUBGOAL {subgoal.id}: {subgoal.description}\n"
                f"SUCCESS CRITERION: {subgoal.success_criterion}\n"
                "Complete this subgoal using the tools available to you."
            ),
        ),
    ]
    tool_calls_made: list[dict] = []
    handoff_requested = False
    handoff_payload = None
    turn = 0

    while turn < max_turns:
        turn += 1
        response = await executor.client.chat(
            model=executor.model,
            messages=messages,
            tools=tools_as_openai or None,
            temperature=0.0,
        )
        if not response.tool_calls:
            return ExecutorResult(
                subgoal_id=subgoal.id,
                success=True,
                final_answer=response.content or "",
                tool_calls_made=tool_calls_made,
                turns_used=turn,
            )

        messages.append(
            ChatMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )
        tool_outputs = await _executor_module._dispatch_tool_calls(
            executor, response.tool_calls, session
        )

        complete_task_fired = False
        approval_index = None

        for i, tool_output in enumerate(tool_outputs):
            if tool_output.get("requires_human_approval"):
                approval_index = i
                break

        for tc, tool_output in zip(response.tool_calls, tool_outputs, strict=True):
            tool_calls_made.append(
                {
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "success": tool_output.get("success", True),
                    "summary": tool_output.get("summary", ""),
                    "requires_human_approval": tool_output.get("requires_human_approval", False),
                }
            )
            if tc.name == "handoff_to_structured" and tool_output.get("success"):
                handoff_requested = True
                handoff_payload = dict(tc.arguments)
            # THE FIX: set flag when complete_task succeeds
            if tc.name == "complete_task" and tool_output.get("success"):
                complete_task_fired = True
            messages.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=json.dumps(tool_output, default=str),
                )
            )

        # Exit immediately — but only if generate_flyer already ran this
        # subgoal. If complete_task fires in sg_1 (before the flyer is
        # written), it is premature — let the loop continue so the LLM
        # sees the tool result and can recover or sg_2 will handle it.
        flyer_ran_this_subgoal = any(
            tc["name"] == "generate_flyer" and tc.get("success") for tc in tool_calls_made
        )
        if complete_task_fired and flyer_ran_this_subgoal:
            return ExecutorResult(
                subgoal_id=subgoal.id,
                success=True,
                final_answer="(complete_task succeeded)",
                tool_calls_made=tool_calls_made,
                turns_used=turn,
            )

        if approval_index is not None:
            from sovereign_agent.ipc.approval import (
                build_request_from_tool_result,
                write_approval_request,
            )

            tc = response.tool_calls[approval_index]
            tool_output = tool_outputs[approval_index]
            ticket_id = f"exec_{subgoal.id}"
            request = build_request_from_tool_result(
                session=session,
                subgoal_id=subgoal.id,
                ticket_id=ticket_id,
                tool_name=tc.name,
                tool_call_id=tc.id,
                tool_arguments=tc.arguments,
                tool_output=tool_output.get("output", {}),
                tool_summary=tool_output.get("summary", ""),
                reason=tool_output.get("output", {}).get("approval_reason", ""),
            )
            write_approval_request(session, request)
            return ExecutorResult(
                subgoal_id=subgoal.id,
                success=True,
                final_answer=f"(awaiting human approval: {request.request_id})",
                tool_calls_made=tool_calls_made,
                turns_used=turn,
                awaiting_approval=request.request_id,
                approval_request=request.to_dict(),
            )

        if handoff_requested:
            return ExecutorResult(
                subgoal_id=subgoal.id,
                success=True,
                final_answer="(handoff requested)",
                tool_calls_made=tool_calls_made,
                handoff_requested=True,
                handoff_payload=handoff_payload,
                turns_used=turn,
            )
        # Continue loop — model will see tool outputs next turn.
        continue

    return ExecutorResult(
        subgoal_id=subgoal.id,
        success=False,
        final_answer=f"(max_turns={max_turns} exhausted without final answer)",
        tool_calls_made=tool_calls_made,
        turns_used=turn,
    )


# Replace the module-level function before any executor is instantiated
_executor_module._react_loop = _patched_react_loop
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Patch LoopHalf.run to inject sg_1 tool outputs into sg_2's description.
#
# Each subgoal runs in a fresh LLM context — sg_2 has no memory of sg_1's
# tool outputs. We intercept between subgoals and rewrite sg_2's description
# with the concrete values extracted from sg_1's tool_calls_made.
# ---------------------------------------------------------------------------
_original_loop_run = LoopHalf.run


async def _patched_loop_run(self, session, input_payload):
    from sovereign_agent.halves import HalfResult
    from sovereign_agent.session.state import now_utc

    task = input_payload.get("task") or ""
    context = input_payload.get("context") or {}

    session.append_trace_event(
        {
            "event_type": "planner.called",
            "actor": self.planner.name,
            "timestamp": now_utc().isoformat(),
            "payload": {"task_preview": task[:200]},
        }
    )
    subgoals = await self.planner.plan(task, context, session)
    session.append_trace_event(
        {
            "event_type": "planner.produced_subgoals",
            "actor": self.planner.name,
            "timestamp": now_utc().isoformat(),
            "payload": {"num_subgoals": len(subgoals)},
        }
    )
    session.update_state(state="executing", planner={"subgoals": [sg.to_dict() for sg in subgoals]})

    executor_results = []
    sg1_tool_outputs = {}  # name -> output dict, populated after sg_1

    for i, sg in enumerate(subgoals):
        if sg.assigned_half != "loop":
            return HalfResult(
                success=True,
                output={
                    "subgoal_id": sg.id,
                    "assigned_half": sg.assigned_half,
                    "executor_results": [
                        _loop_module._execresult_to_dict(r) for r in executor_results
                    ],
                },
                summary=f"subgoal {sg.id} is assigned to {sg.assigned_half}; handing off",
                next_action="handoff_to_structured"
                if sg.assigned_half == "structured"
                else "handoff_to_loop",
                handoff_payload={
                    "subgoal": sg.to_dict(),
                    "prior_results": [
                        _loop_module._execresult_to_dict(r) for r in executor_results
                    ],
                },
            )

        # Inject sg_1 results into sg_2's description before execution
        if i > 0 and sg1_tool_outputs:
            vs = sg1_tool_outputs.get("venue_search", {})
            wx = sg1_tool_outputs.get("get_weather", {})
            cc = sg1_tool_outputs.get("calculate_cost", {})
            # venue dict may use 'name'/'address' or 'venue_name'/'venue_address'
            venue_name = vs.get("venue_name") or vs.get("name")
            venue_address = vs.get("venue_address") or vs.get("address")
            sg.description = (
                f"Call generate_flyer with these EXACT values (already verified from sg_1 tools):\n"
                f"  venue_name={venue_name!r}\n"
                f"  venue_address={venue_address!r}\n"
                f"  date='2026-04-25', time='19:30', party_size=6\n"
                f"  condition={wx.get('condition')!r}\n"
                f"  temperature_c={wx.get('temperature_c')}\n"
                f"  total_gbp={cc.get('total_gbp')}\n"
                f"  deposit_required_gbp={cc.get('deposit_required_gbp')}\n"
                f"Do NOT substitute any other values. Then call complete_task ONCE and stop."
            )

        result = await self.executor.execute(sg, session)
        executor_results.append(result)

        # Extract tool outputs from sg_1 for use in subsequent subgoals
        if i == 0:
            for tc in result.tool_calls_made:
                if tc.get("success") and tc["name"] in (
                    "venue_search",
                    "get_weather",
                    "calculate_cost",
                ):
                    # The output field is in the trace event; we need to get it from _TOOL_CALL_LOG
                    pass
            # Pull from the integrity log which captures full outputs.
            # venue_search may return a list under 'venues'; flatten to top-level.
            for rec in _TOOL_CALL_LOG:
                if rec.tool_name in ("venue_search", "get_weather", "calculate_cost"):
                    out = rec.output
                    if rec.tool_name == "venue_search":
                        # Output is {"results": [{id, venue_name, venue_address, ...}], ...}
                        results = out.get("results") or []
                        if results and isinstance(results, list):
                            out = {**out, **results[0]}
                    sg1_tool_outputs[rec.tool_name] = out

        # If complete_task was called in this subgoal, stop — don't run further subgoals.
        if any(
            tc.get("name") == "complete_task" and tc.get("success") for tc in result.tool_calls_made
        ):
            final_answer = result.final_answer or "(complete_task succeeded)"
            return HalfResult(
                success=True,
                output={
                    "final_answer": final_answer,
                    "executor_results": [
                        _loop_module._execresult_to_dict(r) for r in executor_results
                    ],
                },
                summary=f"loop half completed after complete_task in {sg.id}; {final_answer[:120]}",
                next_action="complete",
            )

        if result.handoff_requested:
            return HalfResult(
                success=True,
                output={
                    "subgoal_id": sg.id,
                    "executor_results": [
                        _loop_module._execresult_to_dict(r) for r in executor_results
                    ],
                },
                summary=f"executor requested handoff to structured from {sg.id}",
                next_action="handoff_to_structured",
                handoff_payload=result.handoff_payload or {},
            )
        if not result.success:
            return HalfResult(
                success=False,
                output={
                    "subgoal_id": sg.id,
                    "executor_results": [
                        _loop_module._execresult_to_dict(r) for r in executor_results
                    ],
                },
                summary=f"executor failed on {sg.id}: {result.final_answer}",
                next_action="escalate",
            )

    final_answer = executor_results[-1].final_answer if executor_results else ""
    return HalfResult(
        success=True,
        output={
            "final_answer": final_answer,
            "executor_results": [_loop_module._execresult_to_dict(r) for r in executor_results],
        },
        summary=f"loop half completed {len(executor_results)} subgoal(s); final answer: {final_answer[:120]}",
        next_action="complete",
    )


LoopHalf.run = _patched_loop_run
# ---------------------------------------------------------------------------

_TASK_TEXT = (
    "You are booking a private event. Follow these steps EXACTLY and IN ORDER.\n\n"
    "FIXED CONSTANTS (use verbatim, do not look these up):\n"
    "  party_size = 6\n"
    "  date = 2026-04-25\n"
    "  time = 19:30\n"
    "  area = Haymarket\n\n"
    "REQUIRED TOOL CALLS IN ORDER:\n"
    "  1. venue_search(near='Haymarket', party_size=6, budget_max_gbp=800)\n"
    "     → extract: venue_id, venue_name, venue_address\n"
    "  2. get_weather(city='edinburgh', date='2026-04-25')\n"
    "     → extract: condition, temperature_c\n"
    "  3. calculate_cost(venue_id=<venue_search.venue_id>, party_size=6,\n"
    "                    duration_hours=3, catering_tier='bar_snacks')\n"
    "     → extract: total_gbp, deposit_required_gbp\n"
    "  4. generate_flyer(event_details={\n"
    "       venue_name: <venue_search.venue_name>,\n"
    "       venue_address: <venue_search.venue_address>,\n"
    "       date: '2026-04-25', time: '19:30', party_size: 6,\n"
    "       condition: <get_weather.condition>,\n"
    "       temperature_c: <get_weather.temperature_c>,\n"
    "       total_gbp: <calculate_cost.total_gbp>,\n"
    "       deposit_required_gbp: <calculate_cost.deposit_required_gbp>\n"
    "     })\n"
    "  5. complete_task — ONLY after generate_flyer confirms flyer.html written. "
    "This is the final tool call. Do not call anything after it.\n\n"
    "ABSOLUTE RULES:\n"
    "  - Every <...> placeholder above MUST be replaced with the exact value "
    "from the named tool's output. No exceptions.\n"
    "  - Do NOT skip any step. Do NOT reorder steps.\n"
    "  - Do NOT call complete_task before generate_flyer succeeds.\n"
    "  - If generate_flyer returns an error, fix the inputs and retry before completing.\n"
)

_PLANNER_SYSTEM = (
    "You are the PLANNER of an always-on agent.\n\n"
    "OUTPUT FORMAT: Respond with ONLY a valid JSON array. No prose, no markdown, "
    "no code fences.\n\n"
    "Produce EXACTLY 2 subgoals:\n\n"
    "sg_1 — description must be exactly:\n"
    '  \'Call venue_search(near="Haymarket", party_size=6, budget_max_gbp=800). '
    'Then call get_weather(city="edinburgh", date="2026-04-25"). '
    "Then call calculate_cost using the venue_id value returned by venue_search, party_size=6, "
    'duration_hours=3, catering_tier="bar_snacks". '
    "All three tools MUST be called. Do not skip any.'\n"
    "  success_criterion: 'venue_search, get_weather, and calculate_cost all returned data'\n"
    "  estimated_tool_calls: 3\n"
    "  depends_on: []\n\n"
    "sg_2 — description must be exactly:\n"
    "  'Call generate_flyer with event_details assembled from sg_1 tool outputs only: "
    "venue_name, venue_address from venue_search; condition, temperature_c from get_weather; "
    "total_gbp, deposit_required_gbp from calculate_cost. "
    'Fixed constants: date="2026-04-25", time="19:30", party_size=6. '
    "Then call complete_task ONCE. complete_task is the final action — stop immediately after it returns. Do not call any further tools.'\n"
    "  success_criterion: 'flyer.html exists in workspace/'\n"
    "  estimated_tool_calls: 2\n"
    '  depends_on: ["sg_1"]\n\n'
    'Both subgoals must have assigned_half: "loop".\n'
    "Do not deviate from these descriptions. The executor reads them literally.\n"
)

_EXECUTOR_SYSTEM = (
    "You are the EXECUTOR of an always-on agent. You receive one subgoal at a time.\n\n"
    "SOURCE OF TRUTH HIERARCHY — in strict order:\n"
    "  1. Tool output values (highest authority — always use these verbatim)\n"
    "  2. Fixed constants from the task (party_size, date, time, area)\n"
    "  3. Nothing else. If a value is not in 1 or 2, call the tool that produces it.\n\n"
    "EXECUTION RULES:\n"
    "- Call tools in the order specified by the subgoal description. Do not reorder.\n"
    "- When passing a value from a prior tool output to the next tool, copy it "
    "character-for-character. Do not reformat, round, or paraphrase.\n"
    "- If a tool returns an error, read the error message, correct your input, "
    "and retry once. Do not skip ahead.\n"
    "- Call each tool exactly once unless a retry is required.\n\n"
    "generate_flyer RULE:\n"
    "- You MUST call generate_flyer. It is not optional.\n"
    "- The event_details dict must contain ALL of these fields, sourced as shown:\n"
    "    venue_name           <- from venue_search output\n"
    "    venue_address        <- from venue_search output\n"
    "    date                 <- fixed constant '2026-04-25'\n"
    "    time                 <- fixed constant '19:30'\n"
    "    party_size           <- fixed constant 6\n"
    "    condition            <- from get_weather output\n"
    "    temperature_c        <- from get_weather output\n"
    "    total_gbp            <- from calculate_cost output\n"
    "    deposit_required_gbp <- from calculate_cost output\n"
    "- Every field must be traceable to either a tool output or a fixed constant.\n\n"
    "COMPLETION RULE:\n"
    "- complete_task may only be called after generate_flyer has returned successfully "
    "and confirmed flyer.html was written. Calling it earlier is a task failure.\n"
    "- complete_task is a TERMINAL action. Stop after it. Do not call any further tools.\n"
    "- The sequence for sg_2 is exactly 2 tool calls: generate_flyer, then complete_task. Full stop.\n"
)


def _build_fake_client() -> FakeLLMClient:
    """Scripts a 2-subgoal trajectory for offline mode."""
    plan_json = json.dumps(
        [
            {
                "id": "sg_1",
                "description": "research Edinburgh venues near Haymarket for a party of 6",
                "success_criterion": "at least one candidate identified",
                "estimated_tool_calls": 3,
                "depends_on": [],
                "assigned_half": "loop",
            },
            {
                "id": "sg_2",
                "description": "produce an HTML flyer with the chosen venue, weather, and cost",
                "success_criterion": "flyer.html written to workspace/",
                "estimated_tool_calls": 1,
                "depends_on": ["sg_1"],
                "assigned_half": "loop",
            },
        ]
    )
    search_call = ToolCall(
        id="c1",
        name="venue_search",
        arguments={"near": "Haymarket", "party_size": 6, "budget_max_gbp": 800},
    )
    weather_call = ToolCall(
        id="c2",
        name="get_weather",
        arguments={"city": "edinburgh", "date": "2026-04-25"},
    )
    cost_call = ToolCall(
        id="c3",
        name="calculate_cost",
        arguments={
            "venue_id": "haymarket_tap",
            "party_size": 6,
            "duration_hours": 3,
            "catering_tier": "bar_snacks",
        },
    )
    flyer_call = ToolCall(
        id="c4",
        name="generate_flyer",
        arguments={
            "event_details": {
                "venue_name": "Haymarket Tap",
                "venue_address": "12 Dalry Rd, Edinburgh EH11 2BG",
                "date": "2026-04-25",
                "time": "19:30",
                "party_size": 6,
                "condition": "cloudy",
                "temperature_c": 12,
                "total_gbp": 356,
                "deposit_required_gbp": 71,
            }
        },
    )
    complete_call = ToolCall(
        id="c5",
        name="complete_task",
        arguments={"result": {"flyer": "workspace/flyer.html", "venue": "haymarket_tap"}},
    )

    return FakeLLMClient(
        [
            ScriptedResponse(content=plan_json),
            ScriptedResponse(tool_calls=[search_call, weather_call, cost_call]),
            ScriptedResponse(tool_calls=[flyer_call]),
            ScriptedResponse(tool_calls=[complete_call]),
            ScriptedResponse(content="Subgoal 1 complete."),
            ScriptedResponse(content="Booking researched; flyer at workspace/flyer.html."),
            ScriptedResponse(content="Task complete."),
        ]
    )


def _tools_are_implemented() -> tuple[bool, str]:
    """Probe tool modules for NotImplementedError. Returns (ok, message)."""
    from starter.edinburgh_research.tools import (
        calculate_cost,
        generate_flyer,
        get_weather,
        venue_search,
    )

    unimplemented: list[str] = []
    for name, call in [
        ("venue_search", lambda: venue_search("Haymarket", 6, 1000)),
        ("get_weather", lambda: get_weather("edinburgh", "2026-04-25")),
        ("calculate_cost", lambda: calculate_cost("haymarket_tap", 6, 3)),
    ]:
        try:
            call()
        except NotImplementedError:
            unimplemented.append(name)
        except Exception:
            pass  # any other exception = some work done

    import inspect

    try:
        src = inspect.getsource(generate_flyer)
        if "raise NotImplementedError" in src and src.count("\n") < 30:
            unimplemented.append("generate_flyer")
    except (OSError, TypeError):
        pass

    try:
        from starter.edinburgh_research.integrity import verify_dataflow as _vd

        src = inspect.getsource(_vd)
        if "raise NotImplementedError" in src and src.count("\n") < 60:
            unimplemented.append("verify_dataflow")
    except (OSError, TypeError, ImportError):
        pass

    if not unimplemented:
        return True, ""

    msg = "\n".join(
        [
            "",
            "━" * 72,
            "  Ex5 isn't implemented yet — expected for a fresh checkout.",
            "━" * 72,
            "",
            f"  Unimplemented: {', '.join(unimplemented)}",
            "",
            "  What to do:",
            "    1. Open starter/edinburgh_research/tools.py",
            "    2. Implement each function marked `TODO` in order: venue_search,",
            "       get_weather, calculate_cost, generate_flyer.",
            "    3. Open starter/edinburgh_research/integrity.py and implement",
            "       verify_dataflow (the heart of Ex5's grade).",
            "    4. Run `make test` — aim to turn the 3 skipped tests green.",
            "    5. Run `make ex5` again.",
            "",
            "  Reference pattern: examples/pub_booking/run.py in the sovereign-agent",
            "  repo has a similar structure (loop half, parallel-safe reads, one",
            "  file write). Copy patterns, change the scenario.",
            "",
            "━" * 72,
            "",
        ]
    )
    return False, msg


async def run_scenario(real: bool, persist: bool | None = None) -> int:
    ok, message = _tools_are_implemented()
    clear_log()

    if not ok:
        print(message)
        return 3

    should_persist = persist if persist is not None else real
    with example_sessions_dir("ex5-edinburgh-research", persist=should_persist) as sessions_root:
        session = create_session(
            scenario="edinburgh-research",
            task="Research Edinburgh venue near Haymarket and produce an HTML event flyer.",
            sessions_dir=sessions_root,
        )
        print(f"Session {session.session_id}")
        print(f"  dir: {session.directory}")

        if real:
            from sovereign_agent.config import Config

            cfg = Config.from_env()
            print(f"  LLM: {cfg.llm_base_url} (live)")
            print(f"  planner:  {cfg.llm_planner_model}")
            print(f"  executor: {cfg.llm_executor_model}")
            client = OpenAICompatibleClient(
                base_url=cfg.llm_base_url,
                api_key_env=cfg.llm_api_key_env,
            )
            planner_model = cfg.llm_planner_model
            executor_model = cfg.llm_executor_model
        else:
            print("  LLM: FakeLLMClient (offline, scripted)")
            client = _build_fake_client()
            planner_model = executor_model = "fake"

        tools = build_tool_registry(session)

        half = LoopHalf(
            planner=DefaultPlanner(
                model=planner_model, client=client, system_prompt=_PLANNER_SYSTEM
            ),
            executor=DefaultExecutor(
                model=executor_model,
                client=client,
                tools=tools,
                system_prompt=_EXECUTOR_SYSTEM,
            ),  # type: ignore[arg-type]
        )

        result = await half.run(session, {"task": _TASK_TEXT})

        print(f"\nLoop half outcome: {result.next_action}")
        print(f"  summary: {result.summary}")

        # Save tool call log for external verification

        log_path = session.logs_dir / "tool_call_log.json"
        log_data = [
            {
                "tool_name": rec.tool_name,
                "arguments": rec.arguments,
                "output": rec.output,
                "timestamp": rec.timestamp.isoformat(),
            }
            for rec in _TOOL_CALL_LOG
        ]
        log_path.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
        print(f"   Tool call log saved to: {log_path}")

        print("\nTickets:")
        for t in list_tickets(session):
            r = t.read_result()
            print(f"  {t.ticket_id}  {t.operation:50s}  {r.state.value}")

        flyer_path = session.workspace_dir / "flyer.html"
        if not flyer_path.exists():
            print("\n✗ No flyer written to workspace/. Ex5 failed.")

            if _TOOL_CALL_LOG:
                print(f"\n  Tools that DID run ({len(_TOOL_CALL_LOG)} calls):")
                for i, rec in enumerate(_TOOL_CALL_LOG, 1):
                    args_preview = str(rec.arguments)[:80]
                    print(f"    {i}. {rec.tool_name}({args_preview})")
                if not any(r.tool_name == "generate_flyer" for r in _TOOL_CALL_LOG):
                    print(
                        "\n  ★ generate_flyer was never called. The LLM either completed "
                        "the task without writing the flyer, or called complete_task "
                        "too early. Check sessions/<id>/logs/trace.jsonl."
                    )
            else:
                print("\n  No tools ran at all — the LLM didn't invoke any registered tool.")
                print(f"  Check the trace: {session.trace_path}")
            return 1

        print(f"\n=== flyer.html ({flyer_path.stat().st_size} bytes) ===")
        flyer_content = flyer_path.read_text(encoding="utf-8")
        print(flyer_content[:500] + ("...\n[truncated]" if len(flyer_content) > 500 else ""))

        print("\n=== Dataflow integrity check ===")
        integrity = verify_dataflow(flyer_content)
        if integrity.ok:
            print(f"✓  {integrity.summary}")
            if integrity.verified_facts:
                print(f"   Verified {len(integrity.verified_facts)} fact(s) against tool outputs.")
        else:
            print(f"✗  {integrity.summary}")
            print(f"   Unverified facts: {integrity.unverified_facts}")
            return 2

        if real:
            print(f"\nArtifacts persist at: {session.directory}")
            print(f'Inspect with: ls -R "{session.directory}"')
            print(f"📜 Narrate this run: make narrate SESSION={session.session_id}")

        return 0


def main() -> None:
    real = "--real" in sys.argv
    persist = "--persist" in sys.argv
    # If --persist is specified, use True; otherwise default to None (which follows real mode)
    persist_value = True if persist else None
    sys.exit(asyncio.run(run_scenario(real=real, persist=persist_value)))


if __name__ == "__main__":
    main()
