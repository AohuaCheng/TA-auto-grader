"""Automate AI grading on Yuketang correcting assistant page."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from topic_detector import REQUIREMENT_TEXT, build_topic_field_from_docx
from yuketang_client import (
    DEFAULT_SESSION_FILE,
    YuketangBrowser,
    yuketang_evaluate_url,
)

GRADING_RULE = "Reflective"
PROGRESS_FILE = "grading_progress.json"
GRADING_TIMEOUT = 600
SUBMIT_TIMEOUT = 90
HISTORY_POLL_INTERVAL = 3
MAX_REGRADE_PER_JOB = 2


@dataclass
class GradeJob:
    student_id: str
    name: str
    docx_path: Path


@dataclass
class PendingSubmission:
    job: GradeJob
    submitted_at: datetime = field(default_factory=datetime.now)
    failed_hrefs: set[str] = field(default_factory=set)
    download_attempts: int = 0
    regrade_count: int = 0
    sanitize_hash: bool = False
    upload_name: str = ""


def feedback_path_for(job: GradeJob) -> Path:
    stem = job.docx_path.stem
    return job.docx_path.with_name(f"{stem}_feedback.docx")


def is_valid_feedback_file(path: Path, min_size: int = 1024) -> bool:
    """仅当磁盘上存在有效 docx（非空 zip）时才视为已有 feedback。"""
    if not path.exists() or path.stat().st_size < min_size:
        return False
    try:
        return path.read_bytes()[:2] == b"PK"
    except OSError:
        return False


def resolve_student_docx(student_dir: Path) -> Path | None:
    """按 meta.json 中的原始文件名定位该生应上传的 docx（兼容 txt→docx 转换）。"""
    docxs = sorted(
        p
        for p in student_dir.glob("*.docx")
        if not p.stem.endswith("_feedback")
    )
    meta_path = student_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            for finfo in meta.get("files", []):
                filename = (finfo.get("filename") or "").strip()
                if not filename:
                    continue
                stem = Path(filename).stem.lower()
                for docx in docxs:
                    if docx.name.lower() == filename.lower():
                        return docx
                    if docx.stem.lower() == stem:
                        return docx
        except (json.JSONDecodeError, OSError):
            pass
    return docxs[0] if docxs else None


def collect_docx_jobs(download_dir: Path) -> list[GradeJob]:
    jobs: list[GradeJob] = []
    for student_dir in sorted(p for p in download_dir.iterdir() if p.is_dir()):
        docx_path = resolve_student_docx(student_dir)
        if not docx_path:
            continue
        student_id, _, name = student_dir.name.partition("_")
        jobs.append(
            GradeJob(
                student_id=student_id,
                name=name,
                docx_path=docx_path,
            )
        )
    return jobs


def load_progress(download_dir: Path) -> dict:
    path = download_dir / PROGRESS_FILE
    if not path.exists():
        return {"completed": []}
    progress = json.loads(path.read_text(encoding="utf-8"))
    return sanitize_progress(download_dir, progress)


def save_progress(download_dir: Path, progress: dict) -> None:
    path = download_dir / PROGRESS_FILE
    path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def sanitize_progress(download_dir: Path, progress: dict) -> dict:
    """去掉 progress 里无对应 feedback 文件的残留记录。"""
    jobs = collect_docx_jobs(download_dir)
    key_to_feedback = {
        f"{job.student_id}_{job.docx_path.name}": feedback_path_for(job)
        for job in jobs
    }
    kept: list[str] = []
    for key in progress.get("completed", []):
        feedback = key_to_feedback.get(key)
        if feedback and is_valid_feedback_file(feedback):
            kept.append(key)
    progress["completed"] = kept
    return progress


def is_already_done(job: GradeJob, progress: dict, history_texts: list[str]) -> bool:
    del progress, history_texts  # 仅以磁盘上的 feedback 文件为准
    return is_valid_feedback_file(feedback_path_for(job))


def filename_has_hash(path: Path) -> bool:
    return "#" in path.name


def prepare_upload_docx(docx_path: Path, sanitize_hash: bool = False) -> Path:
    """上传用路径；文件名含 # 时自动替换为 No.（雨课堂 OSS 会 404）。"""
    if not filename_has_hash(docx_path):
        return docx_path
    if not sanitize_hash:
        print(f"  文件名含 #，自动使用无 # 副本上传: {docx_path.name}")
    safe_name = docx_path.name.replace("#", "No.")
    safe_path = docx_path.with_name(safe_name)
    if not safe_path.exists() or safe_path.stat().st_mtime < docx_path.stat().st_mtime:
        shutil.copy2(docx_path, safe_path)
        print(f"  上传副本（已去掉 #）: {safe_name}")
    return safe_path


def submit_one(
    browser: YuketangBrowser,
    job: GradeJob,
    *,
    sanitize_hash: bool = False,
) -> tuple[bool, str]:
    topic_id, topic_text = build_topic_field_from_docx(job.docx_path)
    upload_path = prepare_upload_docx(job.docx_path, sanitize_hash=sanitize_hash)

    browser.prepare_reflective_edit_form(GRADING_RULE)

    if REQUIREMENT_TEXT not in topic_text:
        return False, "题目文本缺少 requirement 段落"

    if not browser.fill_submission(topic_text, upload_path):
        return False, f"填写/上传未完成: {browser.get_form_state()}"

    if not browser.click_start_grading():
        return False, f"无法点击「开始评分」: {browser.get_form_state()}"

    if not browser.wait_for_grading_submitted(timeout=SUBMIT_TIMEOUT):
        return False, "提交未进入「评分中」或右侧未出分"

    uploaded = upload_path.name
    if sanitize_hash and uploaded != job.docx_path.name:
        uploaded = f"{uploaded} (原文件: {job.docx_path.name})"
    return True, f"topic_{topic_id}, submitted {uploaded}"


def regrade_job(
    browser: YuketangBrowser,
    job: GradeJob,
    *,
    sanitize_hash: bool = False,
) -> PendingSubmission | None:
    """重新提交评分（用于 OSS NoSuchKey / 下载链接失效）。"""
    label = "去掉文件名中的 #" if sanitize_hash else "保持原文件名"
    print(f"  重新评分 {job.student_id}_{job.name}（{label}）…")
    ok, note = submit_one(browser, job, sanitize_hash=sanitize_hash)
    if not ok:
        print(f"  重新评分失败: {note}")
        return None
    print(f"  重新评分已提交: {note}")
    upload_path = prepare_upload_docx(job.docx_path, sanitize_hash=sanitize_hash)
    return PendingSubmission(
        job=job,
        sanitize_hash=sanitize_hash or filename_has_hash(job.docx_path),
        upload_name=upload_path.name,
        regrade_count=1,
    )


def drain_pending_downloads(
    browser: YuketangBrowser,
    pending: list[PendingSubmission],
    progress: dict,
    download_dir: Path,
) -> int:
    """从右侧本次结果下载已完成的 feedback，返回本次成功数。"""
    if not pending:
        return 0

    downloaded = 0
    remaining: list[PendingSubmission] = []

    for item in sorted(pending, key=lambda p: p.submitted_at):
        job = item.job
        feedback_path = feedback_path_for(job)
        if is_valid_feedback_file(feedback_path):
            downloaded += 1
            continue

        item.download_attempts += 1
        expected_name = item.upload_name or job.docx_path.name
        if not item.upload_name and filename_has_hash(job.docx_path):
            expected_name = job.docx_path.name.replace("#", "No.")
        downloaded_ok, reason = browser.download_feedback(
            feedback_path,
            since=item.submitted_at,
            skip_local_names={job.docx_path.name, expected_name},
            expected_source_name=expected_name,
            exclude_hrefs=item.failed_hrefs,
        )
        if downloaded_ok and is_valid_feedback_file(feedback_path):
            key = f"{job.student_id}_{job.docx_path.name}"
            if key not in progress.get("completed", []):
                progress.setdefault("completed", []).append(key)
            save_progress(download_dir, progress)
            print(f"  ↓ 已下载 {job.student_id}_{job.name} -> {feedback_path.name}")
            downloaded += 1
        else:
            if feedback_path.exists():
                feedback_path.unlink(missing_ok=True)
            if browser._last_failed_download_href:
                item.failed_hrefs.add(browser._last_failed_download_href)
            should_regrade = (
                reason in {"no_such_key", "http_error"}
                and item.regrade_count < MAX_REGRADE_PER_JOB
            )
            if should_regrade:
                use_sanitize = filename_has_hash(job.docx_path)
                retried = regrade_job(browser, job, sanitize_hash=use_sanitize)
                if retried:
                    retried.regrade_count = item.regrade_count + 1
                    retried.sanitize_hash = use_sanitize
                    remaining.append(retried)
                    continue
            remaining.append(item)

    pending[:] = remaining
    return downloaded


def filter_jobs(
    jobs: list[GradeJob],
    *,
    student: str = "",
    regrade_only: bool = False,
) -> list[GradeJob]:
    filtered = jobs
    if student:
        needle = student.strip().lower()
        filtered = [
            j
            for j in filtered
            if needle in j.student_id.lower() or needle in j.name.lower()
        ]
    if regrade_only:
        filtered = [j for j in filtered if not is_already_done(j, {}, [])]
    return filtered


def run_grading(
    download_dir: Path,
    session_path: Path = Path(DEFAULT_SESSION_FILE),
    relogin: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    student: str = "",
    regrade_only: bool = False,
    sanitize_hash: bool = False,
) -> None:
    jobs = filter_jobs(
        collect_docx_jobs(download_dir),
        student=student,
        regrade_only=regrade_only,
    )
    if limit:
        jobs = jobs[:limit]

    browser = YuketangBrowser(headless=False)
    pending: list[PendingSubmission] = []

    try:
        logged_in = False
        if not relogin and browser.verify_session(inject_cookies=False):
            print("雨课堂已登录（Chrome profile 有效）。")
            logged_in = True
        if not logged_in:
            if not browser.interactive_login(login_mode="wechat"):
                raise SystemExit(
                    "雨课堂登录失败。请保持 Chrome 窗口打开，完成微信扫码后再试。"
                )
            browser.save_session(session_path)
            if not browser.verify_session(inject_cookies=False):
                raise SystemExit("登录后无法打开批改页面或 CSRF 无效，请重试")

        progress = load_progress(download_dir)
        history: list[str] = []

        print(f"待处理 {len(jobs)} 份 docx，批改规则: {GRADING_RULE}")
        print(f"页面: {yuketang_evaluate_url()}")
        print("模式: 流水线（清空表单→提交→按时间下载 feedback）")
        failed: list[tuple[GradeJob, str]] = []

        for idx, job in enumerate(jobs, start=1):
            if is_already_done(job, progress, history):
                print(
                    f"[{idx}/{len(jobs)}] 跳过 {job.student_id}_{job.name}"
                    "（已有 feedback）"
                )
                continue

            drain_pending_downloads(browser, pending, progress, download_dir)

            topic_id, topic_text = build_topic_field_from_docx(job.docx_path)
            print(
                f"[{idx}/{len(jobs)}] {job.student_id}_{job.name}"
                f" | 题目{topic_id} | 上传: {job.docx_path.name}"
                f" | 路径: {job.docx_path}"
            )

            if dry_run:
                print(f"  题目栏完整内容:\n  {topic_text}")
                print(f"  feedback 将保存为: {feedback_path_for(job).name}")
                continue

            upload_path = prepare_upload_docx(
                job.docx_path, sanitize_hash=sanitize_hash
            )
            ok, note = submit_one(
                browser, job, sanitize_hash=sanitize_hash
            )
            if not ok:
                print(f"  失败: {note}")
                failed.append((job, note))
                continue

            pending.append(
                PendingSubmission(
                    job=job,
                    sanitize_hash=sanitize_hash or filename_has_hash(job.docx_path),
                    upload_name=upload_path.name,
                )
            )
            print(f"  已提交 ({note})，队列待下载 {len(pending)} 份")

            if drain_pending_downloads(browser, pending, progress, download_dir):
                print(f"  队列剩余 {len(pending)} 份")

        if pending:
            print(f"\n等待 {len(pending)} 份批改完成并从右侧结果下载…")
            deadline = time.time() + GRADING_TIMEOUT
            while pending and time.time() < deadline:
                if drain_pending_downloads(browser, pending, progress, download_dir):
                    print(f"  队列剩余 {len(pending)} 份")
                elif pending:
                    time.sleep(HISTORY_POLL_INTERVAL)

        if pending:
            print("\n以下作业未能自动下载 feedback：")
            for item in pending:
                job = item.job
                print(f"  - {job.student_id}_{job.name} ({job.docx_path.name})")
            failed.extend((item.job, "feedback 未下载") for item in pending)

        if failed:
            print(f"\n共 {len(failed)} 份未成功：")
            for job, note in failed:
                print(f"  - {job.student_id}_{job.name}: {note}")
            raise SystemExit("部分作业未完成，请检查后重跑 grade（已完成的会自动跳过）。")

        print("\n全部完成。")
    finally:
        browser.close()
