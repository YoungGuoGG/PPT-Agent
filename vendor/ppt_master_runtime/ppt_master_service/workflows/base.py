from __future__ import annotations

from typing import Any


class BaseWorkflow:
    name = "base"
    is_stub = True

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        self.runtime.log(job["job_id"], f"{self.name}: prepare")
        return {"ok": True, "stage": "prepare"}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(f"{self.name} workflow is a stub and cannot run")

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        self.runtime.log(job["job_id"], f"{self.name}: review")
        return {"ok": True, "stage": "review", "passed": True}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(f"{self.name} workflow is a stub and cannot export")
