from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, session_id: str, role: str, source: str | Path) -> dict[str, str | int]:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"artifact does not exist: {path}")
        target_dir = self.root / session_id
        target_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        target = target_dir / f"{role}_{digest[:12]}{path.suffix.lower()}"
        if path != target:
            shutil.copy2(path, target)
        return {"path": str(target), "sha256": digest, "size": target.stat().st_size}

    def resolve(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact path is outside the artifact store") from exc
        return path
