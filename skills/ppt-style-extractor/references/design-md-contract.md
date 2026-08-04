# design.md Contract

Every reusable style is one directory containing a canonical `design.md` and optional sanitized SVG references:

```text
<style-id>/
├── design.md
└── references/
    ├── reference_01.svg
    └── reference_02.svg
```

The frontmatter requires `id`, `name`, `version`, `status`, `origin`, `tags`, `suitable_for`, and `avoid_for`. Arrays use JSON string-array syntax. A draft has `status: draft`; only review-approved packages are rewritten to `status: published` and exposed to Planner.

The body requires these exact second-level sections:

- `Positioning`
- `Color Tokens`
- `Typography`
- `Page Rhythm`
- `Layout Grammar`
- `Charts`
- `Image and Icon Policy`
- `Do Not`

References must preserve only reusable geometry, hierarchy, spacing, and color relationships. Replace text with semantic placeholders, replace source images with neutral placeholders, and remove scripts, external links, metadata, logos, and embedded objects.

Planner sees only the style summary. `design.compose` and `svg.generate` receive the complete published `design.md`, its version and hash, and sanitized reference paths.
