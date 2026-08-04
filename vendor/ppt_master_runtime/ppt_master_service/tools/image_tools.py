from __future__ import annotations

from .project_tools import SCRIPT_DIR, run_script


def analyze_images(images_dir: str):
    return run_script([str(SCRIPT_DIR / "analyze_images.py"), images_dir])


def render_formulas(project_path: str):
    return run_script([str(SCRIPT_DIR / "latex_render.py"), project_path])


def generate_images_from_manifest(manifest_path: str):
    return run_script([str(SCRIPT_DIR / "image_gen.py"), "--manifest", manifest_path], timeout=1200)


def render_image_manifest_markdown(manifest_path: str):
    return run_script([str(SCRIPT_DIR / "image_gen.py"), "--render-md", manifest_path], timeout=120)
