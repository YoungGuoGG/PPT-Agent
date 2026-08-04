# Execution Plan Contract

Return one JSON object:

```json
{
  "schema": "ppt-agent.execution-plan.v1",
  "goal": "string",
  "route": "document_to_ppt",
  "assumptions": ["string"],
  "style_selection": {
    "style_id": "academic-defense",
    "reason": "The request is a research paper presentation."
  },
  "steps": [
    {
      "id": "source",
      "tool": "source.ingest",
      "depends_on": [],
      "input_bindings": {"files": "$request.attachments"},
      "expected_artifacts": ["source_bundle"]
    }
  ]
}
```

Requirements:

- Use unique, short step IDs containing lowercase letters, digits, `_`, or `-`.
- Reference dependencies by step ID.
- Use only supplied tool IDs.
- Bind request values with `$request.*` and prior artifacts with `$steps.<step-id>.<artifact-role>`.
- Keep assumptions concrete and visible to Review.
- Select exactly one ID from the supplied Style Catalog; never invent an ID.
- When the request contains an explicit style ID, preserve it.
- Produce no prose outside the JSON object.
