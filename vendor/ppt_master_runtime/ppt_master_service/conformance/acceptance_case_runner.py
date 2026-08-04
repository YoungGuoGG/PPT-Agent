from __future__ import annotations

import argparse
import importlib
import json
import re
from pathlib import Path
from typing import Callable

from .acceptance import AcceptanceEvidence, write_acceptance_evidence


_SAFE_WORKFLOW_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
AcceptanceCase = Callable[..., AcceptanceEvidence]


def run_acceptance_case(
    workflow_id: str,
    *,
    provider: bool,
    output_root: Path | None = None,
) -> AcceptanceEvidence:
    """Load and run one append-only workflow acceptance case."""
    if _SAFE_WORKFLOW_ID.fullmatch(workflow_id) is None:
        raise ValueError(f"invalid acceptance workflow id: {workflow_id!r}")
    module_name = f"{__package__}.acceptance_cases.{workflow_id}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise ValueError(
            f"no acceptance case is registered for {workflow_id}; "
            f"create ppt_master_service/conformance/acceptance_cases/{workflow_id}.py"
        ) from exc
    run_case = getattr(module, "run_case", None)
    if not callable(run_case):
        raise ValueError(
            f"acceptance case {module_name} must expose callable run_case(provider=, output_root=)"
        )
    evidence = run_case(provider=provider, output_root=output_root)
    if not isinstance(evidence, AcceptanceEvidence):
        raise TypeError(f"acceptance case {workflow_id} returned {type(evidence).__name__}")
    if evidence.workflow_id != workflow_id:
        raise ValueError(
            f"acceptance case returned evidence for {evidence.workflow_id}, expected {workflow_id}"
        )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an append-only workflow acceptance case")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--provider", action="store_true")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--evidence-path", type=Path)
    args = parser.parse_args()
    if args.evidence_path and not args.provider:
        parser.error("--evidence-path requires --provider; offline runs must not write canonical evidence")
    evidence = run_acceptance_case(
        args.workflow,
        provider=args.provider,
        output_root=args.output_root,
    )
    if args.evidence_path:
        write_acceptance_evidence(args.evidence_path, evidence)
    print(json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
