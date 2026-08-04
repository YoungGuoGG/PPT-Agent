---
name: ppt-agent-planner
description: Build a validated, end-to-end execution plan for presentation creation or revision. Use when PPT Agent must route a request, choose registered presentation tools, record assumptions, compose dependencies, or replan after a structured failure. Do not use it to design slide visuals or author SVG.
---

# PPT Agent Planner

Create an executable plan from the request, available artifacts, review state, and the supplied Tool Catalog summary.

## Plan

1. Read `references/plan-contract.md`.
2. Read `references/workflow-selection.md`.
3. Read `references/recovery-policy.md` only when replanning after a failure.
4. Select only tools present in the supplied catalog and marked executable.
5. Record reasonable assumptions instead of asking the user during initial generation.
6. Return exactly one JSON object matching `ppt-agent.execution-plan.v1`.

## Select one published style

- Read only the supplied Style Catalog summaries; never read full `design.md` files while planning.
- Honor an explicit valid `style_id` before semantic selection.
- Otherwise choose the style whose `suitable_for` and tags best match the request while respecting `avoid_for`.
- Return `style_selection.style_id` and a short evidence-based `reason`.
- Do not invent a style ID. Exact colors, typography, layouts, and SVG decisions remain outside Planner.

## Keep the planning scope narrow

- Decide goals, route, tool order, dependencies, inputs, outputs, and recovery intent.
- Choose a style ID, but do not choose exact colors, fonts, layouts, image crops, SVG coordinates, or decorative details.
- Do not read or quote the `ppt-design` skill.
- Do not emit shell commands or unregistered script paths.
- Do not add interactive steps before `review.prepare`.
- End every initial deck-generation plan with `review.prepare`.

The Plan Compiler is authoritative. Treat compiler errors as precise protocol feedback and repair only the rejected fields.
