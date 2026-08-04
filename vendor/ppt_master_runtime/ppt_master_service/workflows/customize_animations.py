from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .scripted import ScriptedWorkflow


class CustomizeAnimationsWorkflow(ScriptedWorkflow):
    name = "customize_animations"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        svgs = sorted((project / "svg_output").glob("*.svg"))
        if not svgs:
            self.fail(
                step="list_groups",
                code="svg_pages_missing",
                message="animation customization requires svg_output/*.svg",
                location={"path": str(project / "svg_output")},
                correction={"action": "complete_svg_generation_first"},
            )
        result = self.run_script(job, "animation_config.py", "list-groups", str(project), step="list_groups")
        return {"ok": True, "stage": "prepare", "groups": result["stdout"], "slide_count": len(svgs)}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        self.run_script(job, "animation_config.py", "scaffold", str(project), step="scaffold")
        path = project / "animations.json"
        scaffold = json.loads(path.read_text(encoding="utf-8"))
        request = job.get("request") or {}
        options = request.get("animation_request")
        if isinstance(options, str):
            options = {"brief": options}
        options = options if isinstance(options, dict) else {}
        transition = {
            "effect": str(options.get("transition") or "fade"),
            "duration": float(options.get("transition_duration") or 0.4),
        }
        animation = {
            "effect": str(options.get("effect") or "auto"),
            "duration": float(options.get("duration") or 0.4),
            "stagger": float(options.get("stagger") or 0.3),
            "trigger": str(options.get("trigger") or "after-previous"),
        }
        slides = {}
        for svg in sorted((project / "svg_output").glob("*.svg")):
            old = (scaffold.get("slides") or {}).get(svg.stem) or {}
            slides[svg.stem] = {
                "transition": dict(transition),
                "animation": dict(animation),
                **({"groups": old["groups"]} if old.get("groups") else {}),
            }
        payload = {"version": 1, "defaults": {"transition": transition, "animation": animation}, "slides": slides}
        self.write_json(path, payload)
        return {"ok": True, "stage": "run", "animation_config": str(path), "slide_count": len(slides)}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        result = self.run_script(job, "animation_config.py", "validate", str(project), step="validate")
        return {"ok": True, "stage": "review", "passed": True, "validation": result["stdout"]}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        output = project / "exports" / f"{project.name}_animated.pptx"
        output.parent.mkdir(parents=True, exist_ok=True)
        self.run_script(job, "svg_to_pptx.py", str(project), "-o", str(output), "--animation-config", str(project / "animations.json"), step="export")
        self.runtime.store.update_job(job["job_id"], output_file=str(output))
        return {"ok": True, "stage": "export", "output_file": str(output)}
