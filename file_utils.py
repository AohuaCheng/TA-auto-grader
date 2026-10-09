"""Post-process downloaded homework files: detect type, convert to docx."""

from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

from pdf2docx import Converter

WORD_EXTENSIONS = {".doc", ".docx", ".docm", ".odt", ".rtf"}
CONVERTIBLE_EXTENSIONS = {".doc", ".pdf", ".txt"}

MAGIC_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"PK\x03\x04", ".docx", "word"),
    (b"%PDF", ".pdf", "pdf"),
    (b"\xd0\xcf\x11\xe0", ".doc", "word"),
    (b"{\\rtf", ".rtf", "word"),
]


def filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    match = re.search(
        r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?",
        header,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    from urllib.parse import unquote

    name = match.group(1) or match.group(2)
    return unquote(name.strip())


def detect_file_info(path: Path) -> tuple[str | None, str]:
    suffix = path.suffix.lower()
    if suffix in WORD_EXTENSIONS:
        return suffix, "word"
    if suffix in {".pdf", ".txt"}:
        return suffix, "other"

    with path.open("rb") as f:
        head = f.read(16)

    for magic, ext, category in MAGIC_SIGNATURES:
        if head.startswith(magic):
            return ext, category

    if head.startswith(b"PK\x03\x04"):
        return ".docx", "word"

    return suffix or None, "other"


def ensure_extension(path: Path) -> Path:
    detected_ext, _ = detect_file_info(path)
    if not detected_ext or path.suffix.lower() == detected_ext:
        return path

    target = path.with_suffix(detected_ext)
    if target.exists():
        return target
    path.rename(target)
    return target


def _find_textutil() -> str | None:
    for candidate in ("/usr/bin/textutil", shutil.which("textutil")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _find_soffice() -> str | None:
    candidates = [
        shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def convert_pdf_to_docx(path: Path, overwrite: bool = True) -> tuple[Path | None, str]:
    path = ensure_extension(path)
    if path.suffix.lower() != ".pdf":
        return None, "not_pdf"

    output = path.with_suffix(".docx")
    if output.exists() and not overwrite and output.stat().st_size > 0:
        return output, "converted_exists"

    if output.exists() and overwrite:
        output.unlink()

    try:
        converter = Converter(str(path))
        converter.convert(str(output), start=0, end=None)
        converter.close()
    except Exception as exc:
        return None, f"pdf_convert_failed:{exc}"

    if output.exists() and output.stat().st_size > 0:
        return output, "converted_pdf2docx"
    return None, "pdf_convert_failed"


def convert_txt_to_docx(path: Path, overwrite: bool = True) -> tuple[Path | None, str]:
    path = ensure_extension(path)
    if path.suffix.lower() != ".txt":
        return None, "not_txt"

    output = path.with_suffix(".docx")
    if output.exists() and not overwrite and output.stat().st_size > 0:
        return output, "converted_exists"

    if output.exists() and overwrite:
        output.unlink()

    textutil = _find_textutil()
    if not textutil:
        return None, "textutil_missing"

    result = subprocess.run(
        [textutil, "-convert", "docx", str(path), "-output", str(output)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and output.exists():
        return output, "converted_textutil"
    return None, f"txt_convert_failed:{result.stderr.strip()}"


def convert_doc_to_docx(path: Path, overwrite: bool = True) -> tuple[Path | None, str]:
    path = ensure_extension(path)
    if path.suffix.lower() == ".docx":
        return path, "already_docx"
    if path.suffix.lower() != ".doc":
        return None, f"not_doc:{path.suffix}"

    output = path.with_suffix(".docx")
    if output.exists() and not overwrite and output.stat().st_size > 0:
        return output, "converted_exists"

    if output.exists() and overwrite:
        output.unlink()

    textutil = _find_textutil()
    if textutil:
        result = subprocess.run(
            [textutil, "-convert", "docx", str(path), "-output", str(output)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and output.exists():
            return output, "converted_textutil"

    soffice = _find_soffice()
    if soffice:
        result = subprocess.run(
            [
                soffice,
                "--headless",
                "--convert-to",
                "docx",
                "--outdir",
                str(path.parent),
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and output.exists():
            return output, "converted_soffice"

    return None, "convert_failed"


def process_student_file(path: Path, overwrite: bool = True) -> dict[str, str]:
    path = ensure_extension(path)
    ext, category = detect_file_info(path)
    result = {
        "original_path": str(path),
        "extension": ext or "",
        "category": category,
        "converted_path": "",
        "status": "ok",
        "note": "",
    }

    if ext == ".pdf":
        converted, message = convert_pdf_to_docx(path, overwrite=overwrite)
        if converted:
            result["converted_path"] = str(converted)
            result["status"] = "converted"
            result["note"] = message
        else:
            result["status"] = "needs_manual"
            result["note"] = message
        return result

    if ext == ".txt":
        converted, message = convert_txt_to_docx(path, overwrite=overwrite)
        if converted:
            result["converted_path"] = str(converted)
            result["status"] = "converted"
            result["note"] = message
        else:
            result["status"] = "needs_manual"
            result["note"] = message
        return result

    if ext == ".doc":
        converted, message = convert_doc_to_docx(path, overwrite=overwrite)
        if converted:
            result["converted_path"] = str(converted)
            result["status"] = "converted"
            result["note"] = message
        else:
            result["status"] = "needs_manual"
            result["note"] = message
        return result

    if ext == ".docx":
        result["note"] = "word_ok"
        return result

    result["status"] = "needs_manual"
    result["note"] = f"unsupported:{ext or 'unknown'}"
    return result


def _iter_student_files(student_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in student_dir.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.name != "meta.json"
        and p.suffix.lower() != ".csv"
    )


def postprocess_download_dir(download_dir: Path, overwrite: bool = True) -> Path:
    report_path = download_dir / "file_format_report.csv"
    rows: list[dict[str, str]] = []

    for student_dir in sorted(p for p in download_dir.iterdir() if p.is_dir()):
        files = _iter_student_files(student_dir)
        sources = [p for p in files if p.suffix.lower() in CONVERTIBLE_EXTENSIONS]
        docx_files = [p for p in files if p.suffix.lower() == ".docx"]
        converted_stems = {p.stem for p in sources}

        student_id, _, name = student_dir.name.partition("_")

        for path in sources:
            info = process_student_file(path, overwrite=overwrite)
            rows.append(
                {
                    "学号": student_id,
                    "姓名": name,
                    "原始文件": path.name,
                    "转换文件": Path(info["converted_path"]).name
                    if info["converted_path"]
                    else "",
                    "扩展名": info["extension"],
                    "状态": info["status"],
                    "说明": info["note"],
                }
            )

        for docx in docx_files:
            if docx.stem in converted_stems:
                continue
            rows.append(
                {
                    "学号": student_id,
                    "姓名": name,
                    "原始文件": docx.name,
                    "转换文件": docx.name,
                    "扩展名": ".docx",
                    "状态": "ok",
                    "说明": "word_ok",
                }
            )

    with report_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["学号", "姓名", "原始文件", "转换文件", "扩展名", "状态", "说明"],
        )
        writer.writeheader()
        writer.writerows(rows)

    manual = [r for r in rows if r["状态"] == "needs_manual"]
    converted = [r for r in rows if r["状态"] == "converted"]
    print(
        f"后处理完成: {len(converted)} 个已转 docx（保留原文件），"
        f"{len(manual)} 个需手动处理"
    )
    print(f"格式报告: {report_path}")
    return report_path
