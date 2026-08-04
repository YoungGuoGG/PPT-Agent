# SVG Authoring

- Use the locked canvas and `viewBox` on every page.
- Use only supported SVG/PPTX primitives and declared fonts, colors, icons, images, and charts.
- Give semantic top-level groups stable IDs for export and revision.
- Keep text inside safe areas and ensure every required claim remains visible.
- Preserve image aspect ratio and use the declared crop behavior.
- Read all referenced layout/chart SVGs before the first page, then author pages sequentially.
- Re-read the current design lock before each page.
- Run the deterministic page checker after writing each SVG and repair blocking findings before continuing.
- Never start or depend on a preview server.
