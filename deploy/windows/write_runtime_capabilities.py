from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PPT_MASTER = ROOT / "vendor" / "ppt_master_runtime"
if str(PPT_MASTER) not in sys.path:
    sys.path.insert(0, str(PPT_MASTER))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest() -> dict:
    from ppt_master_service.conformance import WorkflowMaturity, audit_maturity, load_registry

    registry_path = PPT_MASTER / "workflows" / "registry.json"
    workflows = {}
    for workflow_id, definition in load_registry().items():
        issues = audit_maturity(definition)
        executable = definition.maturity is WorkflowMaturity.COMPLETE and not issues
        if definition.maturity is WorkflowMaturity.COMPLETE and issues:
            codes = ", ".join(issue.code for issue in issues)
            raise RuntimeError(f"complete workflow failed build audit: {workflow_id}: {codes}")
        evidence_path = (
            PPT_MASTER / definition.acceptance_evidence
            if definition.acceptance_evidence
            else None
        )
        workflows[workflow_id] = {
            "maturity": definition.maturity.value,
            "executable": executable,
            "evidence_sha256": sha256(evidence_path) if evidence_path and evidence_path.is_file() else None,
        }
    return {
        "schema": "ppt-agent.runtime-capabilities.v1",
        "registry_sha256": sha256(registry_path),
        "workflows": workflows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Runtime capability manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
