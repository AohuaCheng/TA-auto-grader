"""Record a manual Yuketang grading session for later automation."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from yuketang_client import (
    DEFAULT_SESSION_FILE,
    YuketangBrowser,
    yuketang_evaluate_url,
)

RECORDINGS_DIR = Path("recordings")
MONITOR_JS = """
if (!window.__yktRecord) {
  window.__yktRecord = { clicks: [], xhr: [], fetch: [] };
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!t) return;
    window.__yktRecord.clicks.push({
      tag: t.tagName,
      text: (t.innerText || t.textContent || '').trim().slice(0, 120),
      cls: (t.className || '').toString().slice(0, 120),
      id: t.id || '',
      ts: Date.now(),
    });
  }, true);
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__yktUrl = url;
    this.__yktMethod = method;
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(body) {
    this.addEventListener('load', () => {
      const url = String(this.__yktUrl || '');
      if (/upload|attach|file|correction|grade|evaluate/i.test(url)) {
        window.__yktRecord.xhr.push({
          method: this.__yktMethod,
          url,
          status: this.status,
          body: (this.responseText || '').slice(0, 400),
          ts: Date.now(),
        });
      }
    });
    return send.call(this, body);
  };
}
"""


def _snapshot(browser: YuketangBrowser) -> dict:
    driver = browser.driver
    if not driver:
        return {"error": "no driver"}

    browser._switch_to_default_content()
    parent_url = driver.current_url
    parent_title = driver.title

    iframe_ok = False
    iframe_url = ""
    form: dict = {}
    try:
        if browser.switch_to_workspace_iframe(wait_seconds=3):
            iframe_ok = True
            iframe_url = driver.current_url
            driver.execute_script(MONITOR_JS)
            form = browser.get_form_state()
            form["rule"] = browser.get_current_grading_rule()
            form["topic_preview"] = browser.get_topic_field_text()[:120]
            clicks = driver.execute_script(
                "return (window.__yktRecord && window.__yktRecord.clicks) || []"
            )
            xhr = driver.execute_script(
                "return (window.__yktRecord && window.__yktRecord.xhr) || []"
            )
            form["recent_clicks"] = clicks[-5:] if clicks else []
            form["recent_xhr"] = xhr[-5:] if xhr else []
            browser._switch_to_default_content()
    except Exception as exc:
        form = {"iframe_error": str(exc)}

    return {
        "parent_url": parent_url,
        "parent_title": parent_title,
        "iframe_ok": iframe_ok,
        "iframe_url": iframe_url,
        "form": form,
    }


def run_recording_session(
    session_path: Path = Path(DEFAULT_SESSION_FILE),
    output: Path | None = None,
    poll_seconds: float = 3.0,
    use_saved_session: bool = False,
) -> Path:
    RECORDINGS_DIR.mkdir(exist_ok=True)
    if output is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = RECORDINGS_DIR / f"yuketang_manual_{stamp}.jsonl"

    browser = YuketangBrowser(headless=False)
    last_click_count = 0
    last_xhr_count = 0

    print("=" * 60)
    print("雨课堂操作录制模式")
    print("=" * 60)
    print(f"目标批改页: {yuketang_evaluate_url()}")
    print()
    print("请在弹出的 Chrome 窗口中手动完成一整次批改，建议顺序：")
    print("  1. 微信扫码登录（若跳到登录页，必须在本 Chrome 窗口内扫码）")
    print("  2. 确认进入 Reflective 批改页")
    print("  3. 填写题目 + 上传 docx")
    print("  4. 点击「开始评分」，等待结果并下载 feedback")
    print()
    print("重要：请勿依赖旧的 yuketang_session.json 注入 cookie。")
    print("      Safari 能评分而 Chrome 不能，通常是 CSRF token 与 session 不同步。")
    print()
    print("本程序会每隔几秒记录页面状态和你的点击/上传请求。")
    print("全部完成后，回到此终端按 Enter 结束录制。")
    print("=" * 60)

    try:
        driver = browser._ensure_driver()
        if use_saved_session and browser.load_session(session_path):
            print(f"尝试注入已保存会话: {session_path}")
            if browser.verify_session(inject_cookies=True):
                print("已保存会话可用（含 CSRF 校验）。")
            else:
                print("已保存会话 CSRF 无效，请在 Chrome 中重新微信扫码登录。")
                browser.reset_session_state()
                driver.get(yuketang_evaluate_url())
        else:
            print("使用 Chrome 持久化 profile，请在浏览器中完成微信扫码登录（若需要）。")
            driver.get(yuketang_evaluate_url())

        browser.dismiss_page_notifications()

        with output.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "session_start",
                        "ts": time.time(),
                        "evaluate_url": yuketang_evaluate_url(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

            print(f"录制中… 输出文件: {output}")
            print("（完成后回到此终端按 Enter 结束录制）")

            done = threading.Event()

            def _wait_enter() -> None:
                input()
                done.set()

            threading.Thread(target=_wait_enter, daemon=True).start()

            start = time.time()
            while not done.is_set():
                snap = _snapshot(browser)
                snap["type"] = "snapshot"
                snap["ts"] = time.time()
                snap["elapsed_s"] = round(time.time() - start, 1)
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
                f.flush()

                form = snap.get("form") or {}
                btn = form.get("button") or {}
                rule = form.get("rule", "")
                topic_len = form.get("topic_len", 0)
                upload = (form.get("upload") or {}).get("attach", "")[:40]
                print(
                    f"  [{snap['elapsed_s']:5.0f}s] "
                    f"规则={rule or '-'} 题目={topic_len}字 "
                    f"附件={upload or '-'} "
                    f"按钮={btn.get('text', '-')} "
                    f"{'(可点)' if btn.get('disabled') is False else ''}"
                )

                # 记录新点击/请求
                if browser.switch_to_workspace_iframe(wait_seconds=2):
                    clicks = driver.execute_script(
                        "return (window.__yktRecord && window.__yktRecord.clicks) || []"
                    )
                    xhr = driver.execute_script(
                        "return (window.__yktRecord && window.__yktRecord.xhr) || []"
                    )
                    browser._switch_to_default_content()
                    if len(clicks) > last_click_count:
                        for c in clicks[last_click_count:]:
                            f.write(
                                json.dumps(
                                    {"type": "click", "ts": time.time(), "data": c},
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                        last_click_count = len(clicks)
                    if len(xhr) > last_xhr_count:
                        for x in xhr[last_xhr_count:]:
                            f.write(
                                json.dumps(
                                    {"type": "xhr", "ts": time.time(), "data": x},
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                            if (
                                x.get("method") == "POST"
                                and x.get("status") == 401
                                and "CSRF" in (x.get("body") or "")
                            ):
                                print(
                                    "  ⚠ POST 401 CSRF 失败 — 请在本 Chrome 窗口重新微信扫码登录"
                                )
                        last_xhr_count = len(xhr)
                    f.flush()

                time.sleep(poll_seconds)

            # 保存会话供后续批量使用
            try:
                browser._sync_cookies()
                if browser._has_auth_cookies():
                    browser.save_session(session_path)
                    print(f"已保存雨课堂会话: {session_path}")
            except Exception as exc:
                print(f"保存会话失败: {exc}")

            f.write(
                json.dumps({"type": "session_end", "ts": time.time()}, ensure_ascii=False)
                + "\n"
            )

        print(f"录制已保存: {output}")
        return output
    finally:
        browser.close()
