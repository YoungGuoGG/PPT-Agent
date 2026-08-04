from __future__ import annotations

import sys

from .project_tools import SCRIPT_DIR


if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from svg_repair import RepairStats as CompatibilityRepairStats  # noqa: E402
from svg_repair import repair_project, repair_svg_file  # noqa: E402


__all__ = ["CompatibilityRepairStats", "repair_project", "repair_svg_file"]
