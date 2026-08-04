# Workflow Selection

Choose the smallest complete route that reaches the requested result.

Before composing the route, select one published style from the supplied summaries. This is a routing decision only: the design tools resolve and apply the complete `design.md` later.

## Initial generation

- Attachments containing source material: choose `document_to_ppt`.
- Topic without source material: choose `topic_research`, then continue through deck generation.
- Explicit template PPTX with guided redesign: choose `template_guided_svg`.
- Explicit native template filling: choose `template_fill_pptx` only when executable.
- Image-led request: add `assets.prepare` with image-heavy policy; do not replace the core route.

Default generation chain:

```text
source.ingest -> style.retrieve -> design.compose -> assets.prepare
-> svg.generate -> quality.check_repair -> pptx.export -> review.prepare
```

Insert `charts.verify` after quality only when chart pages exist. Add audio or custom animations only when explicitly requested.

## Review

- Whole-deck instruction: plan `deck.revise` for all pages, then quality, export, and review.
- Selected-page instruction: bind the exact page list; preserve every other page artifact.

Never plan `live_preview`, a browser editor, an unregistered script, or a design confirmation during initial generation.
