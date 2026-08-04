# Quality Review and Repair

Evaluate deterministic facts before visual judgement: XML validity, canvas, bounds, fonts, images, chart coordinates, page count, and PPTX relationships.

Every repair instruction must include:

- error code and affected step;
- page, element ID, and bounding box when available;
- observed failure and likely cause;
- allowed actions;
- forbidden actions;
- measurable acceptance condition;
- retry budget.

Repair in this order: deterministic fix, constrained element patch, affected-page regeneration, local replan, whole-deck replan. Preserve unaffected pages and source claims.
