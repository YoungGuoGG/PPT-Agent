from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .scripted import ScriptedWorkflow


class GenerateAudioWorkflow(ScriptedWorkflow):
    name = "generate_audio"
    is_stub = False

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        notes = sorted((project / "notes").glob("*.md"))
        if not notes:
            self.fail(
                step="select_voice",
                code="speaker_notes_missing",
                message="audio generation requires per-slide speaker notes",
                location={"path": str(project / "notes")},
                correction={"action": "generate_speaker_notes", "naming": "match SVG basename"},
            )
        request = job.get("request") or {}
        language = str(request.get("language") or "zh-CN").lower()
        voice = str(request.get("voice") or ("zh-CN-XiaoxiaoNeural" if language.startswith("zh") else "en-US-JennyNeural"))
        provider = str(request.get("audio_provider") or "edge")
        plan = self.write_json(project / "audio" / "audio_plan.json", {"provider": provider, "voice": voice, "notes": [path.name for path in notes]})
        return {"ok": True, "stage": "prepare", "voice": voice, "provider": provider, "audio_plan": str(plan)}

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        plan = json.loads((project / "audio" / "audio_plan.json").read_text(encoding="utf-8"))
        output = project / "audio"
        args = [str(project), "--provider", plan["provider"], "--voice", plan["voice"], "-o", str(output)]
        self.run_script(job, "notes_to_audio.py", *args, step="generate_audio")
        files = sorted(path.name for path in output.glob("*.*") if path.suffix.lower() in {".mp3", ".wav", ".m4a"})
        manifest = self.write_json(output / "audio_manifest.json", {"voice": plan["voice"], "provider": plan["provider"], "files": files})
        return {"ok": True, "stage": "run", "audio_manifest": str(manifest), "audio_files": files}

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        manifest = json.loads((project / "audio" / "audio_manifest.json").read_text(encoding="utf-8"))
        note_count = len(list((project / "notes").glob("*.md")))
        if len(manifest["files"]) < note_count:
            self.fail(
                step="validate",
                code="audio_incomplete",
                message="not every speaker-note page has narration audio",
                location={"expected": note_count, "actual": len(manifest["files"])},
                correction={"action": "regenerate_missing_audio", "missing_count": note_count - len(manifest["files"])},
                retryable=True,
            )
        return {"ok": True, "stage": "review", "passed": True, "audio_count": len(manifest["files"])}

    def export(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.project_path(job)
        output = project / "exports" / f"{project.name}_narrated.pptx"
        output.parent.mkdir(parents=True, exist_ok=True)
        self.run_script(job, "svg_to_pptx.py", str(project), "-o", str(output), "--recorded-narration", str(project / "audio"), "--use-narration-timings", step="export")
        self.runtime.store.update_job(job["job_id"], output_file=str(output))
        return {"ok": True, "stage": "export", "output_file": str(output)}
