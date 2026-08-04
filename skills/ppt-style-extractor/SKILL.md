---
name: ppt-style-extractor
description: Extract a reusable, privacy-safe presentation design system from an uploaded PPTX. Use when PPT Agent must analyze slide geometry, colors, typography, page rhythm, chart treatment, and image policy; author a canonical design.md; produce sanitized SVG references; validate the draft; and prepare it for user review before publishing to the style catalog.
---

# PPT Style Extractor

Turn sanitized PPTX evidence into a reusable visual system. The source deck is evidence, not a template to copy.

## Workflow

1. Read `references/design-md-contract.md`.
2. Use only the supplied sanitized evidence: theme colors, font families, canvas, shape density, layout geometry, and sanitized reference pages.
3. Infer reusable visual intent, layout grammar, page rhythm, chart rules, and image/icon policy.
4. Return exactly one JSON object with the requested fields. Do not return Markdown.
5. Generalize from the source. Never reproduce its customer names, personal data, body text, logos, proprietary claims, or embedded assets.

## Quality rules

- Prefer explicit, testable rules over adjectives such as “modern” or “premium”.
- Explain how the style behaves across cover, section, content, data, and closing pages.
- Keep colors and fonts grounded in extracted evidence; assign semantic roles rather than listing raw values only.
- Identify contexts where the style should and should not be selected.
- If evidence is sparse, make conservative assumptions and keep the style broadly usable.
- Do not ask the user during extraction. The complete draft is reviewed only after validation.
