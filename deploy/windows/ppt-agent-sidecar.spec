# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parents[1]
BACKEND = ROOT / "backend"
SKILLS = ROOT / "skills"
STYLE_LIBRARY = ROOT / "style-library"
PPT_MASTER = ROOT / "vendor" / "ppt_master_runtime"
SCRIPT_ROOT = PPT_MASTER / "skills" / "ppt-master" / "scripts"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PPT_MASTER))
sys.path.insert(0, str(SCRIPT_ROOT))

def filtered_tree(source, destination):
    result = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in {".scratch", "__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        if path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        result.append((str(path), str(Path(destination) / relative.parent)))
    return result


datas = filtered_tree(SKILLS, "skills") + filtered_tree(
    STYLE_LIBRARY, "style-library"
) + filtered_tree(
    PPT_MASTER / "skills", "vendor/ppt_master_runtime/skills"
) + [
    (str(PPT_MASTER / "prompts"), "vendor/ppt_master_runtime/prompts"),
    (str(PPT_MASTER / "workflows"), "vendor/ppt_master_runtime/workflows"),
    (str(PPT_MASTER / "ppt_master_service" / "prompts"), "ppt_master_service/prompts"),
]
hiddenimports = collect_submodules(
    "ppt_agent", filter=lambda name: ".tests" not in name
) + collect_submodules(
    "ppt_master_service",
    filter=lambda name: ".test_" not in name and not name.rsplit(".", 1)[-1].startswith("test_"),
)
hiddenimports += collect_submodules("pptx_to_svg") + collect_submodules("template_import")
for package in (
    "uvicorn", "fastapi", "pydantic", "pptx", "lxml", "svglib",
    "reportlab", "fitz", "mammoth", "markdownify", "openpyxl", "edge_tts",
    "PIL", "requests", "bs4", "curl_cffi",
):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
        datas += package_datas
        hiddenimports += package_hidden
    except Exception:
        pass

# NumPy's PyInstaller hook brings the compiled runtime without recursively
# collecting its test/f2py tree. Optional EPUB/Jupyter and Gemini backends are
# intentionally not bundled in the Windows MVP; PDF, DOCX, PPTX, web and the
# configured OpenAI-compatible/Qwen image paths remain self-contained.
hiddenimports += ["numpy"]

a = Analysis(
    [str(ROOT / "deploy" / "windows" / "sidecar_entry.py")],
    pathex=[str(BACKEND), str(PPT_MASTER), str(SCRIPT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "pandas", "pyarrow", "scipy", "torch",
        "torchvision", "cv2", "pygame", "sklearn", "sympy", "notebook",
        "jupyter", "IPython", "ppt_agent.tests",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    name="ppt-agent-v2-sidecar",
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ppt-agent-v2-sidecar",
)
