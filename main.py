#!/usr/bin/env python3
"""Tsinghua Web Learning TA tool: batch download & upload homework grades."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from credentials import get_credentials, load_credentials
from file_utils import postprocess_download_dir
from learn_client import LearnTAClient, parse_assignment_url
from paths import download_root
from login_manager import BrowserLoginManager
from yuketang_client import DEFAULT_SESSION_FILE, YuketangBrowser
from yuketang_grader import run_grading
from yuketang_recorder import run_recording_session


def load_env() -> None:
    load_dotenv()
    cred_path = load_credentials()
    if cred_path:
        print(f"已加载登录凭据: {cred_path}")
    elif not Path("credentials.local.env").exists():
        print(
            "提示: 可复制 credentials.local.env.example 为 credentials.local.env 并填入账号密码。"
        )


def get_session(session_path: Path, relogin: bool = False):
    username, password = get_credentials()
    manager = BrowserLoginManager(
        username=username,
        password=password,
        headless=False,
    )

    if not relogin and manager.load_session(session_path) and manager.verify_session():
        print(f"已复用会话: {session_path}")
        return manager.get_session(), manager

    if not manager.interactive_login():
        manager.close()
        print("登录失败。请在弹出的 Chrome 窗口中完成清华 SSO 登录后重试。")
        sys.exit(1)

    if not manager.verify_session():
        manager.close()
        print("登录后会话验证失败，请重试。")
        sys.exit(1)

    manager.save_session(session_path)
    manager.close()

    if not manager.load_session(session_path) or not manager.verify_session():
        print("保存会话后验证失败，请重试。")
        sys.exit(1)

    return manager.get_session(), manager


def cmd_login(args: argparse.Namespace) -> None:
    session_path = Path(args.session)
    _, manager = get_session(session_path, relogin=True)
    manager.close()
    print("登录完成。")


def cmd_download(args: argparse.Namespace) -> None:
    assignment_url = args.url or os.getenv("THU_ASSIGNMENT_URL")
    if not assignment_url:
        print("请通过 --url 或环境变量 THU_ASSIGNMENT_URL 指定作业页面。")
        sys.exit(1)

    output_root = Path(args.output) if args.output else download_root()

    session, manager = get_session(Path(args.session), relogin=args.relogin)
    try:
        client = LearnTAClient(session)
        output_dir = client.resolve_download_dir(output_root, assignment_url)
        print(f"作业目录: {output_dir.name}")
        client.download_assignment_submissions(
            assignment_url,
            output_dir,
            include_unsubmitted=args.include_unsubmitted,
            overwrite=args.overwrite,
        )
        print(f"全部下载完成，目录: {output_dir}")
    finally:
        manager.close()


def cmd_upload(args: argparse.Namespace) -> None:
    grades_csv = Path(args.grades)
    if not grades_csv.exists():
        print(f"找不到批改文件: {grades_csv}")
        sys.exit(1)

    session, manager = get_session(Path(args.session), relogin=args.relogin)
    try:
        client = LearnTAClient(session)
        default_wlkcid = args.wlkcid
        if not default_wlkcid and os.getenv("THU_ASSIGNMENT_URL"):
            default_wlkcid, _ = parse_assignment_url(os.getenv("THU_ASSIGNMENT_URL"))

        results = client.batch_upload_grades(
            grades_csv,
            dry_run=not args.confirm,
            default_wlkcid=default_wlkcid,
        )

        if args.confirm:
            failed = [r for r in results if not r.get("success")]
            if failed:
                print(f"有 {len(failed)} 条上传失败，请检查响应。")
                sys.exit(1)
        else:
            print("当前为 dry-run 模式，未实际上传。加上 --confirm 才会提交。")
    finally:
        manager.close()


def cmd_yuketang_login(args: argparse.Namespace) -> None:
    session_path = Path(args.session)
    login_mode = "password" if args.password else "wechat"
    browser = YuketangBrowser(headless=False)
    try:
        print("提示：请保持弹出的 Chrome 窗口打开，直到终端显示「登录完成」。")
        if not browser.interactive_login(login_mode=login_mode):
            print("雨课堂登录失败。")
            sys.exit(1)
        browser.save_session(session_path)
        print(f"雨课堂登录完成，会话已保存到 {session_path}。")
        print("可运行: uv run python main.py grade --limit 1")
    finally:
        browser.close()


def cmd_yuketang_record(args: argparse.Namespace) -> None:
    output = Path(args.output) if args.output else None
    run_recording_session(
        session_path=Path(args.session),
        output=output,
        poll_seconds=args.interval,
        use_saved_session=args.use_session,
    )


def cmd_grade_probe(args: argparse.Namespace) -> None:
    session_path = Path(args.session)
    browser = YuketangBrowser(headless=False)
    try:
        if not browser.load_session(session_path) or not browser.verify_session():
            print("会话无效，请先运行: uv run python main.py yuketang-login")
            sys.exit(1)
        browser.open_evaluate_page()
        out = Path("debug_yuketang_evaluate.html")
        out.write_text(browser.get_page_text(), encoding="utf-8")
        history = browser.get_history_texts()
        print(f"当前页面: {browser.driver.current_url}")
        print(f"页面已保存: {out}")
        print(f"检测到历史记录片段 {len(history)} 条（前5条）:")
        for line in history[:5]:
            print(f"  - {line[:100]}")
    finally:
        browser.close()


def _resolve_assignment_dir(
    download_root: Path,
    explicit: str | None,
    url: str | None,
    session_path: Path,
) -> Path:
    if explicit:
        return Path(explicit)
    if url or os.getenv("THU_ASSIGNMENT_URL"):
        assignment_url = url or os.getenv("THU_ASSIGNMENT_URL")
        session, manager = get_session(session_path, relogin=False)
        try:
            client = LearnTAClient(session)
            return client.resolve_download_dir(download_root, assignment_url)
        finally:
            manager.close()
    subdirs = sorted(p for p in download_root.iterdir() if p.is_dir())
    if len(subdirs) == 1:
        return subdirs[0]
    return download_root


def cmd_grade(args: argparse.Namespace) -> None:
    download_dir = _resolve_assignment_dir(
        download_root(), args.dir, args.url, Path(args.session_learn)
    )

    if not download_dir.exists():
        print(f"找不到目录: {download_dir}")
        sys.exit(1)

    run_grading(
        download_dir,
        session_path=Path(args.session),
        relogin=args.relogin,
        limit=args.limit,
        dry_run=args.dry_run,
        student=args.student or "",
        regrade_only=args.regrade_only,
        sanitize_hash=args.sanitize_hash,
    )


def cmd_postprocess(args: argparse.Namespace) -> None:
    if args.dir:
        download_root = Path(args.dir)
    else:
        assignment_url = args.url or os.getenv("THU_ASSIGNMENT_URL")
        if not assignment_url:
            print("请通过 --dir 或 --url / THU_ASSIGNMENT_URL 指定下载目录。")
            sys.exit(1)
        output_root = download_root()
        session, manager = get_session(Path(args.session), relogin=args.relogin)
        try:
            client = LearnTAClient(session)
            download_root = client.resolve_download_dir(output_root, assignment_url)
        finally:
            manager.close()
    if not download_root.exists():
        print(f"找不到下载目录: {download_root}")
        sys.exit(1)
    postprocess_download_dir(download_root, overwrite=not args.keep_existing)


def cmd_list(args: argparse.Namespace) -> None:
    assignment_url = args.url or os.getenv("THU_ASSIGNMENT_URL")
    if not assignment_url:
        print("请通过 --url 或环境变量 THU_ASSIGNMENT_URL 指定作业页面。")
        sys.exit(1)

    wlkcid, zyid = parse_assignment_url(assignment_url)
    session, manager = get_session(Path(args.session), relogin=args.relogin)
    try:
        client = LearnTAClient(session)
        submitted = client.list_submitted_students(wlkcid, zyid)
        unsubmitted = client.list_unsubmitted_students(wlkcid, zyid)
        print(f"已提交: {len(submitted)}")
        for row in submitted:
            print(
                f"  {row.get('xh')} {row.get('xm')} | "
                f"状态={row.get('zt')} 成绩={row.get('cj')} xszyid={row.get('xszyid')}"
            )
        print(f"未提交: {len(unsubmitted)}")
        for row in unsubmitted:
            print(f"  {row.get('xh')} {row.get('xm')} | 状态={row.get('zt')}")
    finally:
        manager.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清华网络学堂助教工具：批量下载学生作业、批量上传批改结果"
    )
    parser.add_argument(
        "--session",
        default="session.json",
        help="会话缓存文件路径（默认 session.json）",
    )
    parser.add_argument(
        "--relogin",
        action="store_true",
        help="忽略已有 session，重新浏览器登录",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="浏览器登录并保存 session")
    p_login.set_defaults(func=cmd_login)

    p_download = sub.add_parser("download", help="批量下载某次作业的全部提交")
    p_download.add_argument("--url", help="助教作业列表页 URL（beforePageList）")
    p_download.add_argument(
        "--output",
        help="下载目录（默认 ./downloads/<wlkcid>_<zyid>）",
    )
    p_download.add_argument(
        "--include-unsubmitted",
        action="store_true",
        help="额外导出未提交学生名单",
    )
    p_download.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已存在的文件",
    )
    p_download.set_defaults(func=cmd_download)

    p_list = sub.add_parser("list", help="列出已提交/未提交学生")
    p_list.add_argument("--url", help="助教作业列表页 URL")
    p_list.set_defaults(func=cmd_list)

    p_post = sub.add_parser(
        "postprocess", help="将 pdf/txt/doc 转为 docx（保留原文件）"
    )
    p_post.add_argument("--dir", help="下载目录（默认 ./downloads/<作业名>）")
    p_post.add_argument("--url", help="助教作业列表页 URL，用于定位下载目录")
    p_post.add_argument(
        "--keep-existing",
        action="store_true",
        help="若目标 docx 已存在则跳过，不覆盖",
    )
    p_post.set_defaults(func=cmd_postprocess)

    p_ykt_login = sub.add_parser(
        "yuketang-login",
        help="登录雨课堂（默认微信扫码）并保存会话",
    )
    p_ykt_login.add_argument(
        "--session",
        default=DEFAULT_SESSION_FILE,
        help="雨课堂会话文件（默认 yuketang_session.json）",
    )
    p_ykt_login.add_argument(
        "--password",
        action="store_true",
        help="改用账号密码登录（默认使用微信扫码）",
    )
    p_ykt_login.set_defaults(func=cmd_yuketang_login)

    p_record = sub.add_parser(
        "yuketang-record",
        help="打开可监控的浏览器，录制你手动完成的一次批改流程",
    )
    p_record.add_argument(
        "--session",
        default=DEFAULT_SESSION_FILE,
        help="雨课堂会话文件（录制结束时会尝试保存）",
    )
    p_record.add_argument(
        "--output",
        help="录制输出 jsonl 路径（默认 recordings/yuketang_manual_时间戳.jsonl）",
    )
    p_record.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="快照间隔秒数（默认 3）",
    )
    p_record.add_argument(
        "--use-session",
        action="store_true",
        help="启动时注入 yuketang_session.json（默认不注入，避免 CSRF 401）",
    )
    p_record.set_defaults(func=cmd_yuketang_record)

    p_grade = sub.add_parser("grade", help="雨课堂 AI 自动批改（逐个上传 docx）")
    p_grade.add_argument(
        "--dir",
        help="作业目录，默认 downloads 下唯一子目录或通过 --url 解析",
    )
    p_grade.add_argument("--url", help="网络学堂作业 URL，用于定位下载目录")
    p_grade.add_argument(
        "--session",
        default=DEFAULT_SESSION_FILE,
        help="雨课堂会话文件",
    )
    p_grade.add_argument(
        "--session-learn",
        default="session.json",
        help="网络学堂 session（仅在使用 --url 定位目录时需要）",
    )
    p_grade.add_argument("--relogin", action="store_true", help="重新登录雨课堂")
    p_grade.add_argument("--limit", type=int, help="仅处理前 N 份")
    p_grade.add_argument(
        "--dry-run",
        action="store_true",
        help="只识别题目并打印，不实际上传",
    )
    p_grade.add_argument(
        "--student",
        help="仅处理学号或姓名包含该字符串的学生",
    )
    p_grade.add_argument(
        "--regrade-only",
        action="store_true",
        help="仅处理尚无有效 feedback 的作业",
    )
    p_grade.add_argument(
        "--sanitize-hash",
        action="store_true",
        help="上传时将文件名中的 # 替换为 No.（规避 OSS NoSuchKey）",
    )
    p_grade.set_defaults(func=cmd_grade)

    p_probe = sub.add_parser("grade-probe", help="检查雨课堂批改页并保存 HTML")
    p_probe.add_argument(
        "--session",
        default=DEFAULT_SESSION_FILE,
        help="雨课堂会话文件",
    )
    p_probe.set_defaults(func=cmd_grade_probe)

    p_upload = sub.add_parser("upload", help="从 CSV 批量上传批改结果")
    p_upload.add_argument(
        "--grades",
        required=True,
        help="批改 CSV，需含 xszyid；可选 成绩/评语/附件",
    )
    p_upload.add_argument(
        "--wlkcid",
        help="默认 wlkcid（若 CSV 中未提供）",
    )
    p_upload.add_argument(
        "--confirm",
        action="store_true",
        help="确认实际上传（默认 dry-run）",
    )
    p_upload.set_defaults(func=cmd_upload)

    return parser


def main() -> None:
    load_env()
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
