---
name: ppt-design
description: Compose, author, review, and repair presentation visual design inside PPT Agent. Use only for registered design tools such as design.compose, svg.generate, quality.visual_review, quality.repair_page, or deck.revise_design; do not use for routing, source conversion, export, or application preview.
---

# PPT Design

Turn a structured source and design request into coherent slide specifications or page SVG while preserving content truth and PowerPoint compatibility.

## Load only what the current tool needs

- `design.compose`: read `references/design-contract.md` and `references/layout-and-style.md`.
- `svg.generate`: read `references/svg-authoring.md`, the locked design contract, and only the referenced layout/chart examples.
- `quality.visual_review` or `quality.repair_page`: read `references/quality-repair.md` plus the affected page contract.
- `deck.revise_design`: read the design contract, the review instruction, and only the affected page references.

## Rules

- Treat source claims, required data, brand constraints, and page scope as immutable unless the request explicitly changes them.
- Record design assumptions; never stop initial generation to ask for preferences.
- Borrow visual grammar and layout rhythm from style cases, never their customer content or unlicensed assets.
- Treat the resolved published `design.md`, version, and hash as the canonical visual contract. Translate it into the job design specification and lock, and keep it authoritative for every generated SVG.
- Generate or repair only the requested page scope.
- Return structured artifacts or structured errors. Never answer only that a page is wrong.
- Do not start a preview server. Application preview is outside this skill.
