"""Tsinghua Web Learning client for TA homework workflows.

Teacher-side download logic adapted from learn2018-autodown:
https://github.com/Trinkle23897/learn2018-autodown
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from data import LEARN_BASE_URL
from file_utils import filename_from_content_disposition, postprocess_download_dir


def _build_url(uri: str) -> str:
    return uri if uri.startswith("http") else LEARN_BASE_URL + uri


def escape_filename(name: str) -> str:
    return (
        html.unescape(name)
        .replace(os.path.sep, "、")
        .replace(":", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .strip()
    )


def escape_dirname(name: str) -> str:
    """Sanitize assignment title for use as a folder name (e.g. 'Essay 1' -> 'Essay1')."""
    cleaned = escape_filename(name)
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned or "assignment"


def parse_assignment_url(url: str) -> tuple[str, str]:
    """Extract (wlkcid, zyid) from a teacher beforePageList URL."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    wlkcid = query.get("wlkcid", [None])[0]
    zyid = query.get("zyid", [None])[0]
    if not wlkcid or not zyid:
        raise ValueError(
            "URL 中缺少 wlkcid 或 zyid。"
            "示例: .../teacher/beforePageList?wlkcid=...&zyid=..."
        )
    return wlkcid, zyid


@dataclass
class SubmissionFile:
    file_id: str
    filename: str


@dataclass
class StudentSubmission:
    xszyid: str
    wlkcid: str
    zyid: str
    student_id: str
    name: str
    department: str
    class_name: str
    submitted_at: str
    status: str
    grade: str
    grader: str
    files: list[SubmissionFile] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class LearnTAClient:
    def __init__(self, session: requests.Session):
        self.session = session

    def _post_json(self, path: str, ao_data: list[dict[str, Any]]) -> dict[str, Any]:
        resp = self.session.post(
            _build_url(path),
            data={"aoData": json.dumps(ao_data, ensure_ascii=False)},
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        resp.raise_for_status()
        return resp.json()

    def _get_html(self, path: str) -> str:
        resp = self.session.get(_build_url(path))
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def get_assignment_title(self, wlkcid: str, zyid: str) -> str:
        data = self._post_json(
            "/b/wlxt/kczy/zy/teacher/pageList",
            [{"name": "wlkcid", "value": wlkcid}],
        )
        for row in data.get("object", {}).get("aaData", []):
            if row.get("zyid") == zyid:
                return row.get("bt") or zyid[:8]
        raise ValueError(f"未找到作业 zyid={zyid}")

    def resolve_download_dir(self, base_dir: Path, assignment_url: str) -> Path:
        wlkcid, zyid = parse_assignment_url(assignment_url)
        title = self.get_assignment_title(wlkcid, zyid)
        return base_dir / escape_dirname(title)

    def _list_payload(self, wlkcid: str, zyid: str) -> list[dict[str, Any]]:
        return [
            {"name": "wlkcid", "value": wlkcid},
            {"name": "zyid", "value": zyid},
            {"name": "iDisplayStart", "value": "0"},
            {"name": "iDisplayLength", "value": "-1"},
        ]

    def list_submitted_students(self, wlkcid: str, zyid: str) -> list[dict[str, Any]]:
        data = self._post_json(
            "/b/wlxt/kczy/xszy/teacher/getDoneInfo",
            self._list_payload(wlkcid, zyid),
        )
        return data.get("object", {}).get("aaData", [])

    def list_unsubmitted_students(self, wlkcid: str, zyid: str) -> list[dict[str, Any]]:
        data = self._post_json(
            "/b/wlxt/kczy/xszy/teacher/getUndoInfo",
            self._list_payload(wlkcid, zyid),
        )
        return data.get("object", {}).get("aaData", [])

    def _parse_submission_files(self, page_html: str) -> list[SubmissionFile]:
        soup = BeautifulSoup(page_html, "html.parser")
        files: list[SubmissionFile] = []
        seen: set[str] = set()
        skip_link_texts = {"下载", "在线批注作业"}

        attachment_blocks = soup.select(".list.fujian")
        if not attachment_blocks:
            return files

        for block in attachment_blocks:
            for node in block.select(".wdhere"):
                filename = None
                file_id = None

                for anchor in node.find_all("a"):
                    text = anchor.get_text(strip=True)
                    onclick = anchor.get("onclick") or ""
                    if text == "下载" and "downloadZyFile" in onclick:
                        match = re.search(
                            r"downloadZyFile\('([^']+)'\)", onclick
                        )
                        if match:
                            file_id = match.group(1)
                    elif (
                        text
                        and text not in skip_link_texts
                        and "." in text
                        and "格式文件支持" not in text
                    ):
                        filename = text

                if not file_id or file_id in seen:
                    continue
                seen.add(file_id)
                files.append(
                    SubmissionFile(
                        file_id=file_id,
                        filename=escape_filename(filename or file_id),
                    )
                )

        return files

    def get_submission_detail(
        self, wlkcid: str, zyid: str, row: dict[str, Any]
    ) -> StudentSubmission:
        xszyid = row["xszyid"]
        page_html = self._get_html(
            f"/f/wlxt/kczy/xszy/teacher/beforePiYue?wlkcid={wlkcid}&xszyid={xszyid}"
        )
        files = self._parse_submission_files(page_html)
        return StudentSubmission(
            xszyid=xszyid,
            wlkcid=wlkcid,
            zyid=zyid,
            student_id=row.get("xh", ""),
            name=row.get("xm", ""),
            department=row.get("dwmc", ""),
            class_name=row.get("bm", ""),
            submitted_at=row.get("scsjStr", ""),
            status=row.get("zt", ""),
            grade=str(row.get("cj", "")),
            grader=row.get("jsm", ""),
            files=files,
            raw=row,
        )

    def download_submission_file(
        self,
        wlkcid: str,
        file_id: str,
        target_path: Path,
        overwrite: bool = False,
        preferred_name: str | None = None,
    ) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and not overwrite and target_path.stat().st_size > 0:
            return target_path

        url = _build_url(f"/b/wlxt/kczy/xszy/teacher/downloadFile/{wlkcid}/{file_id}")
        resp = self.session.get(url, stream=True)
        resp.raise_for_status()

        if preferred_name:
            target_path = target_path.with_name(escape_filename(preferred_name))
        else:
            server_name = filename_from_content_disposition(
                resp.headers.get("content-disposition")
            )
            if server_name:
                target_path = target_path.with_name(escape_filename(server_name))

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return target_path

    def download_assignment_submissions(
        self,
        assignment_url: str,
        output_dir: Path,
        include_unsubmitted: bool = False,
        overwrite: bool = False,
    ) -> list[StudentSubmission]:
        wlkcid, zyid = parse_assignment_url(assignment_url)
        output_dir.mkdir(parents=True, exist_ok=True)

        submitted_rows = self.list_submitted_students(wlkcid, zyid)
        print(f"已提交 {len(submitted_rows)} 份作业。")

        submissions: list[StudentSubmission] = []
        for row in tqdm(submitted_rows, desc="下载作业"):
            submission = self.get_submission_detail(wlkcid, zyid, row)
            student_dir = output_dir / f"{submission.student_id}_{submission.name}"
            student_dir.mkdir(parents=True, exist_ok=True)

            if overwrite:
                for old_file in student_dir.iterdir():
                    if old_file.is_file() and old_file.name != "meta.json":
                        old_file.unlink()

            for idx, file_info in enumerate(submission.files, start=1):
                filename = file_info.filename
                if len(submission.files) > 1:
                    filename = f"{idx:02d}_{filename}"
                target = student_dir / filename
                saved = self.download_submission_file(
                    wlkcid,
                    file_info.file_id,
                    target,
                    overwrite=overwrite,
                    preferred_name=file_info.filename,
                )
                file_info.filename = saved.name

            meta_path = student_dir / "meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "xszyid": submission.xszyid,
                        "wlkcid": submission.wlkcid,
                        "zyid": submission.zyid,
                        "student_id": submission.student_id,
                        "name": submission.name,
                        "submitted_at": submission.submitted_at,
                        "status": submission.status,
                        "grade": submission.grade,
                        "files": [
                            {"file_id": f.file_id, "filename": f.filename}
                            for f in submission.files
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            submissions.append(submission)

        self._write_manifest(output_dir, submissions)

        postprocess_download_dir(output_dir)

        if include_unsubmitted:
            unsubmitted = self.list_unsubmitted_students(wlkcid, zyid)
            unsubmitted_path = output_dir / "unsubmitted.csv"
            with unsubmitted_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["学号", "姓名", "院系", "班级", "状态"])
                for row in unsubmitted:
                    writer.writerow(
                        [
                            row.get("xh", ""),
                            row.get("xm", ""),
                            row.get("dwmc", ""),
                            row.get("bm", ""),
                            row.get("zt", ""),
                        ]
                    )
            print(f"未提交 {len(unsubmitted)} 人，清单已写入 {unsubmitted_path}")

        return submissions

    def _write_manifest(
        self, output_dir: Path, submissions: list[StudentSubmission]
    ) -> None:
        manifest_path = output_dir / "submissions.csv"
        with manifest_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "学号",
                    "姓名",
                    "院系",
                    "班级",
                    "上交时间",
                    "状态",
                    "成绩",
                    "批阅老师",
                    "xszyid",
                    "文件数",
                ]
            )
            for s in submissions:
                writer.writerow(
                    [
                        s.student_id,
                        s.name,
                        s.department,
                        s.class_name,
                        s.submitted_at,
                        s.status,
                        s.grade,
                        s.grader,
                        s.xszyid,
                        len(s.files),
                    ]
                )
        print(f"提交清单已写入 {manifest_path}")

    def _extract_grade_form(
        self, page_html: str
    ) -> tuple[str, dict[str, str], list[str], dict[str, str]]:
        soup = BeautifulSoup(page_html, "html.parser")
        form = soup.find("form")
        if not form:
            raise RuntimeError("批改页面未找到表单，可能 session 已失效。")

        action = form.get("action") or ""
        if action.startswith("/"):
            action = LEARN_BASE_URL + action
        elif action and not action.startswith("http"):
            action = urllib.parse.urljoin(f"{LEARN_BASE_URL}/", action)

        fields: dict[str, str] = {}
        file_fields: list[str] = []
        checkbox_fields: dict[str, str] = {}
        for inp in form.find_all(["input", "textarea", "select"]):
            name = inp.get("name")
            if not name:
                continue
            tag = inp.name.lower()
            input_type = (inp.get("type") or "").lower()

            if tag == "textarea":
                fields[name] = inp.get_text()
            elif tag == "select":
                selected = inp.find("option", selected=True) or inp.find("option")
                fields[name] = selected.get("value", "") if selected else ""
            elif input_type == "checkbox":
                checkbox_fields[name] = inp.get("value", "1")
            elif input_type in ("submit", "button", "file", "reset"):
                if input_type == "file":
                    file_fields.append(name)
                continue
            else:
                fields[name] = inp.get("value", "")

        if not action:
            action = _build_url("/b/wlxt/kczy/xszy/teacher/piYue")
        return action, fields, file_fields, checkbox_fields

    def _detect_excellent_field(self, page_html: str) -> tuple[str, str] | None:
        """Return (field_name, checked_value) for the excellent-homework control."""
        soup = BeautifulSoup(page_html, "html.parser")

        # MINC / wlxt grading form: checkbox name=ktzt, value=X ("设为优秀作业").
        ktzt = soup.find("input", {"name": "ktzt", "type": "checkbox"})
        if ktzt is not None:
            return "ktzt", ktzt.get("value", "X")

        for node in soup.find_all(string=re.compile(r"优秀作业")):
            parent = node.find_parent(["tr", "div", "label", "li", "td", "span"])
            if not parent:
                continue
            for inp in parent.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                input_type = (inp.get("type") or "").lower()
                if input_type == "checkbox":
                    return name, inp.get("value", "X")
        return None

    def upload_grade(
        self,
        wlkcid: str,
        xszyid: str,
        *,
        score: str | None = None,
        comment: str | None = None,
        attachment: Path | None = None,
        excellent: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        page_html = self._get_html(
            f"/f/wlxt/kczy/xszy/teacher/beforePiYue?wlkcid={wlkcid}&xszyid={xszyid}"
        )
        action, fields, file_fields, checkbox_fields = self._extract_grade_form(page_html)

        score_keys = ["cj", "zycj", "score"]
        comment_keys = ["pynr", "py", "bz", "comment", "zynr"]

        if score is not None:
            for key in score_keys:
                if key in fields:
                    fields[key] = str(score)
                    break
            else:
                fields["cj"] = str(score)

        if comment is not None:
            for key in comment_keys:
                if key in fields:
                    fields[key] = comment
                    break
            else:
                fields["pynr"] = comment

        excellent_field = self._detect_excellent_field(page_html)
        if excellent:
            if excellent_field:
                field_name, field_value = excellent_field
                fields[field_name] = field_value
            elif checkbox_fields:
                field_name = next(iter(checkbox_fields))
                fields[field_name] = checkbox_fields[field_name]
            # Server-side flag toggled by the UI when marking excellent homework.
            fields["sfyx"] = "是"
            soup = BeautifulSoup(page_html, "html.parser")
            initmxdx = soup.find("input", id="initmxdx")
            initmxdxmc = soup.find("input", id="initmxdxmc")
            if initmxdx and initmxdx.get("value"):
                fields.setdefault("yxzsfw", initmxdx["value"])
                fields.setdefault("mxdx", initmxdx["value"])
            if initmxdxmc and initmxdxmc.get("value"):
                fields.setdefault("mxdxmc", initmxdxmc["value"])
            fields.setdefault("sfzm", "是")

        files = None
        if attachment and attachment.exists():
            if not file_fields:
                file_fields = ["fileupload"]
            files = {file_fields[0]: (attachment.name, attachment.read_bytes())}

        if dry_run:
            return {
                "dry_run": True,
                "action": action,
                "fields": fields,
                "files": list(files.keys()) if files else [],
                "excellent_field": excellent_field,
            }

        if files:
            resp = self.session.post(action, data=fields, files=files)
        else:
            resp = self.session.post(action, data=fields)

        resp.encoding = resp.apparent_encoding or "utf-8"
        success = resp.status_code == 200 and "失败" not in resp.text
        return {
            "dry_run": False,
            "status_code": resp.status_code,
            "success": success,
            "response_preview": resp.text[:500],
            "excellent_field": excellent_field,
        }

    def batch_upload_grades(
        self,
        grades_csv: Path,
        *,
        dry_run: bool = True,
        default_wlkcid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Upload grades from CSV.

        Required columns: 学号 or student_id, xszyid
        Optional columns: 成绩/score, 评语/comment, 附件/attachment, wlkcid
        """
        results: list[dict[str, Any]] = []
        with grades_csv.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        for row in tqdm(rows, desc="上传批改"):
            xszyid = row.get("xszyid") or row.get("XSZYID")
            if not xszyid:
                results.append({"row": row, "success": False, "error": "缺少 xszyid"})
                continue

            wlkcid = row.get("wlkcid") or default_wlkcid
            if not wlkcid:
                results.append({"row": row, "success": False, "error": "缺少 wlkcid"})
                continue

            score = row.get("成绩") or row.get("score")
            comment = row.get("评语") or row.get("comment")
            attachment_raw = row.get("附件") or row.get("attachment")
            attachment = Path(attachment_raw) if attachment_raw else None
            excellent_raw = row.get("优秀作业") or row.get("excellent") or ""
            excellent = str(excellent_raw).strip() in {"1", "true", "True", "yes", "Y"}

            result = self.upload_grade(
                wlkcid=wlkcid,
                xszyid=xszyid,
                score=score,
                comment=comment,
                attachment=attachment,
                excellent=excellent,
                dry_run=dry_run,
            )
            student_id = row.get("学号") or row.get("student_id") or ""
            result["student_id"] = student_id
            results.append(result)

        ok = sum(1 for r in results if r.get("success", r.get("dry_run")))
        print(f"批改处理完成: {ok}/{len(results)}")
        return results
