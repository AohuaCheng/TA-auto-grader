#!/usr/bin/env python3
"""Realign misplaced feedback docx by matching content to submissions."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from paths import download_root
from topic_detector import extract_docx_text
from yuketang_grader import (
    PROGRESS_FILE,
    collect_docx_jobs,
    feedback_path_for,
    is_valid_feedback_file,
    load_progress,
    sanitize_progress,
    save_progress,
)

load_dotenv()


def resolve_download_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    root = download_root()
    if root.is_dir() and any(root.iterdir()):
        subdirs = sorted(p for p in root.iterdir() if p.is_dir())
        if len(subdirs) == 1:
            return subdirs[0]
    return root


def fingerprint(path: Path) -> str:
    return re.sub(r"\s+", " ", extract_docx_text(path).lower().strip())[:3000]


def score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a[:200] in b or b[:200] in a:
        return 1.0
    aw = {a[i : i + 80] for i in range(0, max(1, len(a) - 80), 40)}
    bw = {b[i : i + 80] for i in range(0, max(1, len(b) - 80), 40)}
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(len(aw), len(bw))


def find_best_feedback(sub_text: str, candidates: list[tuple[Path, str]]) -> tuple[Path, float]:
    best_path = Path()
    best_score = -1.0
    for path, fb_text in candidates:
        sc = score(sub_text, fb_text)
        if sc > best_score:
            best_score = sc
            best_path = path
    return best_path, best_score


def main(dry_run: bool = False, download_dir: Path | None = None) -> int:
    base = download_dir or resolve_download_dir(None)
    jobs = collect_docx_jobs(base)
    subs = {j.student_id: fingerprint(j.docx_path) for j in jobs}

    candidates: list[tuple[Path, str]] = []
    for j in jobs:
        fb = feedback_path_for(j)
        if is_valid_feedback_file(fb):
            candidates.append((fb, fingerprint(fb)))

    moves: list[tuple[Path, Path, str, float]] = []
    regrade: list[str] = []

    for j in jobs:
        dest = feedback_path_for(j)
        src, sc = find_best_feedback(subs[j.student_id], candidates)
        if sc < 0.5:
            regrade.append(f"{j.student_id}_{j.name}")
            continue
        if src.resolve() != dest.resolve():
            moves.append((src, dest, f"{j.student_id}_{j.name}", sc))

    print(f"目录: {base}")
    print(f"可移动修复: {len(moves)} 份")
    for src, dest, label, sc in moves:
        print(f"  {label}: {src.parent.name}/{src.name}")
        print(f"    -> {dest.parent.name}/{dest.name} ({sc:.2f})")

    print(f"需重新评分: {len(regrade)} 人")
    for name in regrade:
        print(f"  - {name}")

    if dry_run:
        return 0

    tmp = base / ".feedback_realign_tmp"
    tmp.mkdir(exist_ok=True)
    try:
        for idx, (_src, dest, _label, _sc) in enumerate(moves):
            staged = tmp / f"{idx:03d}.docx"
            shutil.copy2(_src, staged)
        for idx, (_src, dest, label, _sc) in enumerate(moves):
            staged = tmp / f"{idx:03d}.docx"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged, dest)
            print(f"已修复: {label}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    progress = sanitize_progress(base, load_progress(base))
    save_progress(base, progress)
    print(f"已更新 {PROGRESS_FILE}")

    if regrade:
        print("\n请运行:")
        for name in regrade:
            sid = name.split("_")[0]
            print(f"  uv run python main.py grade --regrade-only --student {sid}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="按正文相似度修复错位的 feedback")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入")
    parser.add_argument(
        "--dir",
        help="作业下载目录（默认 THU_DOWNLOAD_DIR 下唯一子目录或根目录）",
    )
    args = parser.parse_args()
    dir_path = Path(args.dir) if args.dir else None
    raise SystemExit(main(dry_run=args.dry_run, download_dir=dir_path))
