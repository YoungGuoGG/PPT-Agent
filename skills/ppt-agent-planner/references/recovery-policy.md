# Recovery Policy

Use the structured error to select the narrowest recovery:

1. Retry transport failures without changing the plan.
2. Apply deterministic compatibility repair.
3. Apply an element-level constrained patch.
4. Regenerate the affected page.
5. Replan only the affected page range.
6. Replan the whole deck only when the design contract itself is invalid.

Never broaden page scope silently. Respect retry budgets and preserve the last valid artifact version.
