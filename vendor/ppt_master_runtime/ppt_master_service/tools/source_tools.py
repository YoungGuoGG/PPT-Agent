from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlparse

from .project_tools import SCRIPT_DIR, ToolExecutionError, run_script


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"}


def _safe_stem(value: str, fallback: str = "source") -> str:
    stem = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value).strip("._")
    return stem[:120] or fallback


def _output_path(project_path: str, stem: str) -> Path:
    sources_dir = Path(project_path) / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    candidate = sources_dir / f"{_safe_stem(stem)}.md"
    counter = 2
    while candidate.exists():
        candidate = sources_dir / f"{_safe_stem(stem)}_{counter}.md"
        counter += 1
    return candidate


def pdf_to_md(pdf_file: str, output: str) -> str:
    run_script([str(SCRIPT_DIR / "source_to_md" / "pdf_to_md.py"), pdf_file, "-o", output], timeout=900)
    return output


def doc_to_md(doc_file: str, output: str) -> str:
    run_script([str(SCRIPT_DIR / "source_to_md" / "doc_to_md.py"), doc_file, "-o", output], timeout=900)
    return output


def excel_to_md(excel_file: str, output: str) -> str:
    suffix = Path(excel_file).suffix.lower()
    if suffix == ".xls":
        raise ToolExecutionError("legacy .xls is not supported by the deterministic wrapper; resave as .xlsx")
    run_script([str(SCRIPT_DIR / "source_to_md" / "excel_to_md.py"), excel_file, "-o", output], timeout=900)
    return output


def ppt_to_md(ppt_file: str, output: str) -> str:
    run_script([str(SCRIPT_DIR / "source_to_md" / "ppt_to_md.py"), ppt_file, "-o", output], timeout=900)
    return output


def web_to_md(url: str, output: str) -> str:
    run_script([str(SCRIPT_DIR / "source_to_md" / "web_to_md.py"), url, "-o", output], timeout=900)
    return output


def image_to_md(image_file: str, project_path: str, output: str) -> str:
    source = Path(image_file)
    target_dir = Path(project_path) / "images" / "user"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    Path(output).write_text(
        "\n".join(
            [
                "# Supplied Image Asset",
                "",
                f"- Filename: {source.name}",
                f"- Project path: {target}",
                "",
                "Use this image as visual source material when it is relevant to the requested presentation.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return output


def convert_sources_to_markdown(
    source_files: list[str],
    source_urls: list[str],
    project_path: str,
) -> list[str]:
    outputs: list[str] = []
    sources_dir = Path(project_path) / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    for source in source_files:
        path = Path(source)
        if not path.exists() or not path.is_file():
            raise ToolExecutionError(f"source file does not exist: {source}")
        suffix = path.suffix.lower()
        output = _output_path(project_path, path.stem)
        if suffix in {".md", ".markdown"}:
            target = sources_dir / path.name
            shutil.copy2(path, target)
            outputs.append(str(target))
        elif suffix == ".txt":
            target = output
            target.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            outputs.append(str(target))
        elif suffix == ".pdf":
            outputs.append(pdf_to_md(str(path), str(output)))
        elif suffix in {".docx", ".doc", ".odt", ".rtf", ".epub", ".html", ".htm", ".tex", ".latex", ".rst", ".org", ".ipynb", ".typ"}:
            outputs.append(doc_to_md(str(path), str(output)))
        elif suffix in {".xlsx", ".xlsm", ".xls"}:
            outputs.append(excel_to_md(str(path), str(output)))
        elif suffix in {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}:
            outputs.append(ppt_to_md(str(path), str(output)))
        elif suffix in {".csv", ".tsv"}:
            target = sources_dir / path.name
            shutil.copy2(path, target)
            outputs.append(str(target))
        elif suffix in IMAGE_EXTENSIONS:
            outputs.append(image_to_md(str(path), project_path, str(output)))
        else:
            raise ToolExecutionError(f"unsupported source file type for markdown conversion: {suffix}")

    for index, url in enumerate(source_urls, start=1):
        parsed = urlparse(url)
        stem = _safe_stem("_".join(part for part in (parsed.netloc, parsed.path.strip("/").replace("/", "_")) if part), f"url_{index}")
        output = _output_path(project_path, stem)
        outputs.append(web_to_md(url, str(output)))

    return outputs
