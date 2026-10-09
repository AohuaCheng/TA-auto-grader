"""Browser session helpers for pro.yuketang.cn."""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import os

from dotenv import load_dotenv

from credentials import get_credentials
from data import DEFAULT_HEADERS

load_dotenv()

YUKETANG_BASE = "https://pro.yuketang.cn"


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value or value.startswith("YOUR_"):
        raise RuntimeError(
            f"请在 .env 中设置 {name}（从雨课堂批改页 URL 获取）"
        )
    return value


def yuketang_workspace_id() -> str:
    return _require_env("YUKETANG_WORKSPACE_ID")


def yuketang_rule_id() -> str:
    return _require_env("YUKETANG_RULE_ID")


def yuketang_form_rule_id() -> str:
    return _require_env("YUKETANG_FORM_RULE_ID")


def yuketang_evaluate_url(rule_id: str | None = None) -> str:
    rid = rule_id or yuketang_rule_id()
    ws = yuketang_workspace_id()
    return (
        f"{YUKETANG_BASE}/v2/web/ai-workspace/{ws}/ai-center/correcting-assistant-evaluate"
        f"?rule_id={rid}"
    )


def yuketang_login_url() -> str:
    rid = yuketang_rule_id()
    ws = yuketang_workspace_id()
    return (
        f"{YUKETANG_BASE}/web/?next=/v2/web/ai-workspace/{ws}/ai-center/correcting-assistant-evaluate"
        f"%3Frule_id%3D{rid}"
    )


DEFAULT_SESSION_FILE = "yuketang_session.json"
CHROME_PROFILE_DIR = Path.home() / ".cache" / "yuketang-chrome-profile"

# 题目栏在 #UEditor_q_title，对应 ueditorInstant1（不是 ueditorInstant0）
TOPIC_EDITOR_JS = """
function __getTopicEditor() {
  const root = document.querySelector('#UEditor_q_title');
  if (root && window.UE && UE.instants) {
    const iframe = root.querySelector('iframe');
    if (iframe) {
      for (const key of Object.keys(UE.instants)) {
        const ed = UE.instants[key];
        const edIframe = ed.iframe
          || (ed.container && ed.container.querySelector('iframe'));
        if (edIframe && edIframe.id === iframe.id) return ed;
      }
    }
  }
  return (UE.instants && UE.instants['ueditorInstant1']) || null;
}
function __getTopicPlainText() {
  const ed = __getTopicEditor();
  if (ed && ed.getContentTxt) {
    const fromEd = (ed.getContentTxt() || '').trim();
    if (fromEd.length > 0) return fromEd;
  }
  const iframe = document.querySelector('#UEditor_q_title iframe');
  if (iframe && iframe.contentDocument && iframe.contentDocument.body) {
    return (iframe.contentDocument.body.innerText
      || iframe.contentDocument.body.textContent || '').trim();
  }
  return '';
}
"""


class YuketangBrowser:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.driver: webdriver.Chrome | None = None
        self.cookies: dict[str, str] = {}
        self._cookies_in_browser = False
        self._active_rule: str | None = None
        self._form_ready = False
        self._grading_started_at: datetime | None = None
        self._known_download_hrefs: set[str] = set()
        self._downloaded_hrefs: set[str] = set()
        self._last_failed_download_href: str = ""
        self._history_panel_open = False

    def _ensure_driver(self) -> webdriver.Chrome:
        if self.driver is not None:
            return self.driver
        options = ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument(f"--user-agent={DEFAULT_HEADERS['User-Agent']}")
        options.add_argument("--window-size=1400,900")
        CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
        options.add_argument("--profile-directory=Default")
        try:
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(90)
            self.driver.implicitly_wait(2)
        except WebDriverException as exc:
            raise RuntimeError("无法启动 Chrome，请确认已安装 Chrome。") from exc
        return self.driver

    def _log(self, msg: str) -> None:
        print(msg, flush=True)

    def _driver_alive(self) -> bool:
        if not self.driver:
            return False
        try:
            _ = self.driver.current_url
            return True
        except WebDriverException:
            return False

    def _get_browser_cookies(self) -> list[dict]:
        if not self._driver_alive():
            return []
        try:
            cookies = self.driver.get_cookies()
            return cookies if cookies else []
        except WebDriverException:
            return []

    def _is_on_login_page(self) -> bool:
        driver = self._ensure_driver()
        url = (driver.current_url or "").lower()
        if "correcting-assistant-evaluate" in url:
            return False
        body = driver.execute_script("return document.body?.innerText || '';") or ""
        return (
            "/web" in url
            or "扫码登录" in body
            or "账号登录" in body
            or "微信快捷登录" in body
        )

    def _is_on_agreement_page(self) -> bool:
        driver = self._ensure_driver()
        url = (driver.current_url or "").lower()
        if "correcting-assistant-evaluate" in url:
            return False
        body = driver.execute_script("return document.body?.innerText || '';") or ""
        agreement_hints = ("用户协议", "隐私政策", "服务协议", "协议内容")
        return any(k in body for k in agreement_hints) and "批改测试" not in body

    def reset_session_state(self) -> None:
        """清除内存中的旧 cookie，避免登录循环。"""
        self.cookies = {}
        self._cookies_in_browser = False
        self._active_rule = None
        self._form_ready = False

    def _ensure_on_evaluate_page(self) -> None:
        """若误入协议页，返回批改页（不在登录页反复跳转）。"""
        driver = self._ensure_driver()
        if self._is_on_agreement_page():
            self._log("检测到协议页，正在返回批改页…")
            driver.get(yuketang_evaluate_url())

    def _tick_login_agreement_checkbox(self) -> None:
        """登录页只勾选协议复选框，不点击《用户协议》《隐私政策》链接。"""
        if not self._is_on_login_page():
            return
        driver = self._ensure_driver()
        try:
            checkbox = driver.find_element(
                By.CSS_SELECTOR, ".agreement-box input[type='checkbox']"
            )
            if not checkbox.is_selected():
                driver.execute_script("arguments[0].click();", checkbox)
        except Exception:
            pass

    def _ensure_wechat_qrcode_login(self) -> None:
        """Stay on / switch to WeChat QR login (荷塘雨课堂默认方式)."""
        driver = self._ensure_driver()
        self._tick_login_agreement_checkbox()

        # 若当前是账号密码页，点左上角切回扫码
        for selector in ("img.changeImg", ".changeImg", ".mabox img"):
            try:
                img = driver.find_element(By.CSS_SELECTOR, selector)
                alt = (img.get_attribute("alt") or "").strip()
                src = img.get_attribute("src") or ""
                # 账号登录模式下 alt 通常是「账号密码登录」，点击可切回扫码
                if "账号" in alt or "account" in alt.lower():
                    img.click()
                    time.sleep(1)
                    break
            except Exception:
                continue

        # 确保在「扫码登录」tab
        for text in ("扫码登录", "微信快捷登录"):
            if self.click_by_visible_text(text):
                time.sleep(0.5)

        self._log("请使用微信扫描 Chrome 窗口中的二维码登录雨课堂。")

    def _try_password_login(self) -> None:
        username, password = get_credentials()
        if not username or not password:
            self._log("未配置账号密码，请改用微信扫码登录。")
            return

        driver = self._ensure_driver()
        try:
            driver.find_element(By.CSS_SELECTOR, "img.changeImg").click()
            time.sleep(1)
        except Exception:
            pass

        self._tick_login_agreement_checkbox()

        try:
            user_input = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="loginname"]'))
            )
            pass_input = driver.find_element(By.CSS_SELECTOR, 'input[name="password"]')
            user_input.clear()
            user_input.send_keys(username)
            pass_input.clear()
            pass_input.send_keys(password)
            self._log("已填入账号密码，如有验证码请在浏览器中手动完成。")
            for selector in (".submit-btn.login-btn", ".login-btn"):
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, selector)
                    driver.execute_script(
                        'arguments[0].classList.remove("disabled");', btn
                    )
                    btn.click()
                    break
                except Exception:
                    continue
        except TimeoutException:
            self._log("未找到账号密码框，请改用微信扫码登录。")

    def _try_finish_login_from_evaluate_page(self) -> bool:
        """已在批改页时验证 iframe + CSRF，不跳转回登录页。"""
        if not self._driver_alive():
            return False
        driver = self._ensure_driver()
        url = driver.current_url or ""
        if "correcting-assistant-evaluate" not in url:
            driver.get(yuketang_evaluate_url())
            time.sleep(2)
        self.dismiss_page_notifications()
        if not self._can_access_evaluate_iframe():
            return False
        self._sync_cookies()
        if self.check_csrf_works():
            self._log("雨课堂登录成功，已进入批改页（CSRF 正常）。")
            return True
        return False

    def interactive_login(
        self, timeout: int = 300, login_mode: str = "wechat"
    ) -> bool:
        driver = self._ensure_driver()

        self._log("检查 Chrome 是否已有登录态…")
        if self._try_finish_login_from_evaluate_page():
            return True

        self._log("正在打开雨课堂登录页，请微信扫码…")
        driver.get(yuketang_login_url())
        time.sleep(2)

        if login_mode == "password":
            self._try_password_login()
        else:
            self._ensure_wechat_qrcode_login()

        start = time.time()
        csrf_refresh_count = 0
        qr_hint_at = 0.0

        while time.time() - start < timeout:
            if not self._driver_alive():
                self._log("Chrome 窗口已关闭，登录中止。请保持窗口打开直到终端提示成功。")
                return False

            self._sync_cookies()
            url = driver.current_url or ""

            if self._is_on_login_page():
                if time.time() - qr_hint_at > 15:
                    self._ensure_wechat_qrcode_login()
                    qr_hint_at = time.time()
                time.sleep(2)
                continue

            if self._is_on_agreement_page():
                driver.get(yuketang_evaluate_url())
                time.sleep(2)
                continue

            if self._is_logged_in_url(url) or "correcting-assistant-evaluate" in url:
                if "correcting-assistant-evaluate" not in url:
                    driver.get(yuketang_evaluate_url())
                    time.sleep(3)
                if self._try_finish_login_from_evaluate_page():
                    return True
                csrf_refresh_count += 1
                if csrf_refresh_count <= 5:
                    self._log(
                        f"已进入批改页，等待 CSRF 同步…"
                        f"（{csrf_refresh_count}/5，刷新页面）"
                    )
                    driver.refresh()
                    time.sleep(3)
                    continue
                self._log(
                    "批改页已打开但 CSRF 仍失败。"
                    "请关闭 Chrome 后重试，或在窗口内退出账号再重新扫码。"
                )
                return False

            time.sleep(2)

        self._log("雨课堂登录超时，请确认已完成微信扫码。")
        return False

    def _browser_sessionid(self) -> str:
        for cookie in self._get_browser_cookies():
            if cookie.get("name") == "sessionid":
                return cookie.get("value") or ""
        return ""

    def _has_auth_cookies(self) -> bool:
        auth_keys = {"sessionid", "csrftoken", "django_language"}
        if self.driver:
            names = {c["name"] for c in self._get_browser_cookies()}
            if "sessionid" in names and names & auth_keys:
                return len(names) >= 3
        names = set(self.cookies)
        return bool(names & auth_keys) and "sessionid" in names and len(names) >= 3

    def _can_access_evaluate_iframe(self) -> bool:
        self._switch_to_default_content()
        self.dismiss_page_notifications()
        if not self.switch_to_workspace_iframe(wait_seconds=20):
            return False
        ready = self._is_evaluate_iframe_ready()
        self._switch_to_default_content()
        return ready

    def _is_logged_in_url(self, url: str) -> bool:
        lowered = url.lower()
        if "/web/?next=" in lowered:
            return False
        if lowered.rstrip("/").endswith("/web"):
            return False
        if "login" in lowered and "ai-workspace" not in lowered:
            return False
        return "yuketang.cn" in lowered and (
            "ai-workspace" in lowered or "ai-center" in lowered or "/v2/web/" in lowered
        )

    def _sync_cookies(self) -> None:
        if not self._driver_alive():
            return
        self.cookies = {
            c["name"]: c["value"] for c in self._get_browser_cookies() if c.get("name")
        }

    def save_session(self, path: str | Path = DEFAULT_SESSION_FILE) -> None:
        self._sync_cookies()
        if not self._has_auth_cookies():
            raise RuntimeError("会话 cookie 不完整，请重新登录。")
        Path(path).write_text(
            json.dumps({"cookies": self.cookies, "timestamp": time.time()}, indent=2),
            encoding="utf-8",
        )
        self._log(f"雨课堂会话已保存: {path}")

    def load_session(self, path: str | Path = DEFAULT_SESSION_FILE) -> bool:
        session_path = Path(path)
        if not session_path.exists():
            return False
        data = json.loads(session_path.read_text(encoding="utf-8"))
        self.cookies = data.get("cookies") or {}
        return self._has_auth_cookies()

    def apply_session_to_browser(self, force: bool = False) -> None:
        """将文件中的 cookie 覆盖写入浏览器（不删除 profile 里其他 cookie）。"""
        if self._cookies_in_browser and not force:
            return
        driver = self._ensure_driver()
        driver.get(YUKETANG_BASE)
        time.sleep(0.5)
        for name, value in self.cookies.items():
            for domain in (".yuketang.cn", "pro.yuketang.cn"):
                try:
                    driver.add_cookie(
                        {"name": name, "value": value, "domain": domain, "path": "/"}
                    )
                except Exception:
                    pass
        driver.get(YUKETANG_BASE)
        time.sleep(0.5)
        self._sync_cookies()
        self._cookies_in_browser = True

    def ensure_csrf_sync(self, leave_iframe: bool = True) -> bool:
        """让 iframe 内 SPA 的 axios 使用当前 csrftoken cookie。"""
        self._switch_to_default_content()
        self._sync_cookies()
        csrf = self.cookies.get("csrftoken", "")
        if not csrf:
            return False
        if not self.switch_to_workspace_iframe(wait_seconds=10):
            return False
        driver = self._ensure_driver()
        synced = driver.execute_script(
            """
            function getCookie(name) {
              const m = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
              return m ? m[2] : '';
            }
            const token = getCookie('csrftoken') || arguments[0];
            if (!token) return { ok: false, reason: 'no token' };
            const setHeader = (client) => {
              if (!client || !client.defaults) return;
              client.defaults.headers = client.defaults.headers || {};
              client.defaults.headers.common = client.defaults.headers.common || {};
              client.defaults.headers.common['X-CSRFToken'] = token;
            };
            setHeader(window.axios);
            if (window.$ && window.$.ajaxSetup) {
              window.$.ajaxSetup({ headers: { 'X-CSRFToken': token } });
            }
            return { ok: true, token: token.slice(0, 8) + '...' };
            """,
            csrf,
        )
        if leave_iframe:
            self._switch_to_default_content()
        return bool(synced and synced.get("ok"))

    def check_csrf_works(self) -> bool:
        """POST 探针：401 表示 csrftoken 与 sessionid 不同步（GET 能打开页面但无法评分）。"""
        self._sync_cookies()
        csrf = self.cookies.get("csrftoken", "")
        if not csrf or not self.cookies.get("sessionid"):
            return False
        self._switch_to_default_content()
        self.ensure_csrf_sync()
        driver = self._ensure_driver()
        if not self.switch_to_workspace_iframe(wait_seconds=10):
            return False
        try:
            result = driver.execute_async_script(
                """
                const callback = arguments[arguments.length - 1];
                const tok = arguments[0];
                fetch('/c27/online_courseware/ykt/agent/ai_correction_problem/16/modify_edit_mode/?uv_id=2598&platform_id=3', {
                  method: 'POST',
                  headers: { 'X-CSRFToken': tok, 'Content-Type': 'application/json' },
                  credentials: 'same-origin',
                  body: '{}',
                })
                  .then(r => r.text().then(t => callback({
                    status: r.status,
                    csrfOk: r.status !== 401,
                    body: t.slice(0, 120),
                  })))
                  .catch(e => callback({ csrfOk: false, error: String(e) }));
                """,
                csrf,
            )
        except WebDriverException:
            result = None
        self._switch_to_default_content()
        return bool(result and result.get("csrfOk"))

    _RULE_SELECTOR_JS = (
        ".rule-select-cmp .res-text .text, "
        ".rule-select-cmp .text, "
        ".top-rule-box .text"
    )

    def _read_grading_rule_js(self) -> str:
        sel = self._RULE_SELECTOR_JS
        return (
            f"((document.querySelector('{sel}')?.innerText || "
            f"document.querySelector('{sel}')?.textContent || '').trim())"
        )

    def _is_evaluate_iframe_ready(self, require_rule: bool = False) -> bool:
        driver = self._ensure_driver()
        return bool(
            driver.execute_script(
                """
                const requireRule = arguments[0];
                const url = location.href || '';
                const body = document.body?.innerText || '';
                if (body.includes('扫码登录') || body.includes('账号登录')) return false;
                if (!url.includes('correcting-assistant-evaluate')) return false;
                const ruleEl = document.querySelector(arguments[1]);
                const ruleText = (ruleEl?.innerText || ruleEl?.textContent || '').trim();
                if (requireRule) return ruleText.length > 0;
                const title = document.querySelector('.evaluation-header .title');
                if (title && (title.innerText || '').includes('批改测试')) return true;
                return ruleText.length > 0;
                """,
                require_rule,
                self._RULE_SELECTOR_JS,
            )
        )

    def verify_session(self, inject_cookies: bool = False) -> bool:
        """验证 Chrome 当前登录态。默认只用 profile，不注入 yuketang_session.json。"""
        if not self._driver_alive():
            self._ensure_driver()
        if inject_cookies:
            if not self._has_auth_cookies():
                return False
            self.apply_session_to_browser()
        return self._try_finish_login_from_evaluate_page()

    def _switch_to_default_content(self) -> None:
        if self.driver:
            self.driver.switch_to.default_content()

    def dismiss_page_notifications(self) -> None:
        """关闭左下角「教学智友」等功能通知，避免遮挡操作。"""
        self._switch_to_default_content()
        if self.click_by_visible_text("我知道了", exact=True):
            self._log("已关闭页面通知。")

    def switch_to_workspace_iframe(
        self, wait_seconds: int = 15, require_rule: bool = False
    ) -> bool:
        self._switch_to_default_content()
        driver = self._ensure_driver()
        try:
            iframe = WebDriverWait(driver, wait_seconds).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "#ai-workspace-spa, iframe.ai-workspace-spa")
                )
            )
            driver.switch_to.frame(iframe)
            WebDriverWait(driver, wait_seconds).until(
                lambda d: self._is_evaluate_iframe_ready(require_rule=require_rule)
            )
            return True
        except TimeoutException:
            return False

    def get_form_readiness(self) -> dict:
        driver = self._ensure_driver()
        return (
            driver.execute_script(
                """
                """ + TOPIC_EDITOR_JS + """
                const ed = __getTopicEditor();
                const hasUE = !!(ed && ed.body && ed.body.ownerDocument);
                const hasIframe = !!document.querySelector('#UEditor_q_title iframe');
                const uploadCount = document.querySelectorAll(
                  'input[type="file"], input.el-upload__input'
                ).length;
                const hasUploadWrapper = !!document.querySelector('.upload-attachment-wrapper');
                const attachText = (
                  document.querySelector('.test-input-wrapper .attachment-preview .name-txt')?.innerText || ''
                ).trim();
                const hasUploadedAttachment = /\\.docx/i.test(attachText);
                const hasBtn = !!document.querySelector('.btn-confirm');
                const uploadReady = uploadCount > 0 || hasUploadWrapper || hasUploadedAttachment;
                return {
                  hasUE, hasIframe, uploadCount, hasUploadWrapper, hasBtn,
                  hasUploadedAttachment, uploadReady,
                  ready: (hasUE || hasIframe) && hasBtn && uploadReady,
                };
                """
            )
            or {}
        )

    def wait_for_evaluate_form(self, timeout: int = 45) -> bool:
        driver = self._ensure_driver()
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: bool(self.get_form_readiness().get("ready"))
            )
            return True
        except TimeoutException:
            return False

    def get_page_mode(self) -> str:
        """editing=可填题上传；grading=评分中；result_view=结果页需点编辑。"""
        btn_text = self.get_submit_button_state().get("text", "")
        if btn_text == "评分中":
            return "grading"
        if "开始评分" in btn_text:
            return "editing"
        if btn_text in {"编辑", "重新编辑", "重新评分"}:
            return "result_view"
        attach = (self.get_upload_state().get("attach") or "")
        if "可上传" in attach:
            return "editing"
        return "unknown"

    def summarize_page_mode(self) -> str:
        btn = self.get_submit_button_state()
        upload = self.get_upload_state()
        return (
            f"模式={self.get_page_mode()}, "
            f"按钮={btn.get('text')}, "
            f"附件={(upload.get('attach') or '')[:50]}"
        )

    def is_edit_form_ready(self) -> bool:
        mode = self.get_page_mode()
        if mode == "result_view":
            return False
        if mode in {"editing", "grading"}:
            return True
        attach = (self.get_upload_state().get("attach") or "")
        return "可上传" in attach

    def _form_is_empty(self) -> bool:
        return self._attachment_cleared() and len(
            self.get_topic_field_text().strip()
        ) < 10

    def is_edit_form_visible(self) -> bool:
        return self.is_edit_form_ready()

    def wait_for_edit_form_ready(self, timeout: int = 30) -> bool:
        driver = self._ensure_driver()
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: self.is_edit_form_ready()
            )
            return True
        except TimeoutException:
            return False

    def _click_enter_edit_mode(self) -> str | None:
        """点击底部主按钮或明确的「编辑/重新编辑/重新评分」进入填表。"""
        driver = self._ensure_driver()
        return driver.execute_script(
            """
            const allowed = new Set(['编辑', '重新编辑', '重新评分']);
            const readText = (el) => (el.querySelector('span')?.innerText || el.innerText || '').trim();
            const tryClick = (el) => {
              if (!el || el.offsetParent === null) return null;
              if (el.classList.contains('disabled') || el.classList.contains('is-disabled')) {
                return null;
              }
              const txt = readText(el);
              if (!allowed.has(txt)) return null;
              (el.querySelector('span') || el).click();
              return txt;
            };

            const confirm = document.querySelector('.bottom-btn-box .btn-confirm, .btn-confirm');
            const fromConfirm = tryClick(confirm);
            if (fromConfirm) return fromConfirm;

            for (const el of document.querySelectorAll('button, a, span, div, label')) {
              const txt = readText(el);
              if (!allowed.has(txt)) continue;
              if (el.offsetParent === null) continue;
              if ((el.closest('.ai-display-result-entry-item')?.innerText || '').includes('编辑成果')) {
                continue;
              }
              if ((el.innerText || '').includes('编辑封面')) continue;
              (el.querySelector('span') || el).click();
              return txt;
            }
            return null;
            """
        )

    def _request_edit_mode_via_api(self) -> bool:
        self._sync_cookies()
        csrf = self.cookies.get("csrftoken", "")
        if not csrf:
            return False
        driver = self._ensure_driver()
        try:
            result = driver.execute_async_script(
                """
                const callback = arguments[arguments.length - 1];
                const tok = arguments[0];
                fetch('/c27/online_courseware/ykt/agent/ai_correction_problem/16/modify_edit_mode/?uv_id=2598&platform_id=3', {
                  method: 'POST',
                  headers: { 'X-CSRFToken': tok, 'Content-Type': 'application/json' },
                  credentials: 'same-origin',
                  body: '{}',
                })
                  .then(r => r.text().then(t => callback({ ok: r.status !== 401, status: r.status })))
                  .catch(e => callback({ ok: false }));
                """,
                csrf,
            )
        except WebDriverException:
            return False
        return bool(result and result.get("ok"))

    def _has_attachment_preview(self) -> bool:
        driver = self._ensure_driver()
        return bool(
            driver.execute_script(
                """
                return !!document.querySelector(
                  '.test-input-wrapper .attachment-preview, .text-input-content .attachment-preview'
                );
                """
            )
        )

    def _attachment_cleared(self) -> bool:
        upload = self.get_upload_state()
        attach = (upload.get("attach") or "").strip()
        if "可上传" in attach:
            return True
        # 上传卡在 0% 时 preview 可能尚未出现，但 attach 里已有文件名+删除
        if upload.get("uploading") or "0%" in attach or "删除" in attach:
            return False
        if re.search(r"\.docx", attach, re.I):
            return False
        return not upload.get("has_attachment") and not self._has_attachment_preview()

    def _dismiss_confirm_dialog(self) -> None:
        driver = self._ensure_driver()
        driver.execute_script(
            """
            for (const el of document.querySelectorAll('button, span, div, a')) {
              const txt = (el.innerText || el.textContent || '').trim();
              if (!['确定', '确认', '是', 'OK'].includes(txt)) continue;
              if (el.offsetParent === null) continue;
              el.click();
              return true;
            }
            return false;
            """
        )

    def _click_delete_attachment(self) -> bool:
        """仅点击左侧作答区的「删除」，不碰右侧智能批注。"""
        if self._click_delete_on_upload_wrapper():
            return True
        driver = self._ensure_driver()
        selectors = [
            ".test-input-wrapper .attachment-preview .btn",
            ".test-input-wrapper .attachment-preview span",
            ".text-input-content .attachment-preview .btn",
            ".text-input-content .attachment-preview span",
            ".upload-attachment-wrapper .btn",
        ]
        for sel in selectors:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if (el.text or "").strip() != "删除":
                    continue
                if not el.is_displayed():
                    continue
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", el
                    )
                    time.sleep(0.2)
                    ActionChains(driver).move_to_element(el).pause(0.1).click(
                        el
                    ).perform()
                    return True
                except WebDriverException:
                    continue

        return bool(
            driver.execute_script(
                """
                const roots = document.querySelectorAll(
                  '.test-input-wrapper, .text-input-content'
                );
                for (const root of roots) {
                  for (const btn of root.querySelectorAll('.btn, span, div')) {
                    const txt = (btn.innerText || btn.textContent || '').trim();
                    if (txt !== '删除') continue;
                    if (btn.offsetParent === null) continue;
                    btn.click();
                    return true;
                  }
                }
                return false;
                """
            )
        )

    def _clear_previous_attachment(self) -> bool:
        """点「删除」并确认附件区已清空。"""
        if self._attachment_cleared():
            return True
        if not self.is_edit_form_ready():
            return False

        before = self.get_submission_attachment_name() or (
            self.get_upload_state().get("attach") or ""
        )
        if not self._click_delete_attachment():
            return False

        time.sleep(0.5)
        self._dismiss_confirm_dialog()

        deadline = time.time() + 10
        while time.time() < deadline:
            time.sleep(0.5)
            if self._attachment_cleared():
                label = before[:50] if before else "旧附件"
                self._log(f"  已删除附件「{label}」，恢复上传入口。")
                return True

        after = self.get_submission_attachment_name() or (
            self.get_upload_state().get("attach") or ""
        )
        self._log(f"  已点击删除，但附件仍在: {after[:50]}")
        return False

    def _clear_topic_field(self) -> bool:
        driver = self._ensure_driver()
        return bool(
            driver.execute_script(
                TOPIC_EDITOR_JS
                + """
                const ed = __getTopicEditor();
                if (!ed) return false;
                ed.setContent('', false);
                ed.fireEvent('contentChange');
                ed.fireEvent('blur');
                return __getTopicPlainText().length < 5;
                """
            )
        )

    def clear_submission_form(self) -> None:
        """Reflective 会载入上次 case：先编辑 → 删附件 → 清题目 → 验证为空。"""
        if self.get_page_mode() == "result_view":
            self._log("  当前为结果页（Reflective 上次 case），先点「编辑」…")

        if not self.ensure_edit_form(timeout=35):
            raise RuntimeError(
                "无法进入编辑模式。"
                f" {self.summarize_page_mode()}"
            )

        if not self._attachment_cleared():
            before = self.get_submission_attachment_name() or (
                self.get_upload_state().get("attach") or ""
            )
            self._log(f"  正在删除旧附件「{before[:50]}」…")
            for attempt in range(1, 4):
                if self._clear_previous_attachment():
                    break
                time.sleep(1)
            if not self._attachment_cleared():
                self._log("  删除未生效，尝试点「取消」后重新进入编辑…")
                driver = self._ensure_driver()
                driver.execute_script(
                    """
                    const btn = document.querySelector('.bottom-btn-box .btn-cancel');
                    if (btn && btn.offsetParent !== null) btn.click();
                    """
                )
                time.sleep(1.5)
                if not self.ensure_edit_form(timeout=20):
                    raise RuntimeError(
                        f"取消后无法重新进入编辑。"
                        f" {self.summarize_page_mode()}"
                    )
                if not self._attachment_cleared() and not self._clear_previous_attachment():
                    raise RuntimeError(
                        f"无法删除旧附件「{before[:50]}」。"
                        f" {self.summarize_page_mode()}"
                    )

        if len(self.get_topic_field_text().strip()) > 0:
            if self._clear_topic_field():
                self._log("  已清空题目栏。")
            time.sleep(0.5)

        if not self._form_is_empty():
            raise RuntimeError(
                "删除后表单仍未空。"
                f" {self.summarize_page_mode()}"
            )
        self._log("  旧附件与题目已清除。")

    def ensure_edit_form(self, timeout: int = 30) -> bool:
        """进入可编辑上传状态；仅在底部按钮变为「开始评分」等后才算成功。"""
        if self.is_edit_form_ready():
            return True

        if self.is_edit_form_ready() and not self._attachment_cleared():
            if self._clear_previous_attachment():
                return True

        self._log(f"  当前 {self.summarize_page_mode()}，正在点击「编辑」…")
        clicked = self._click_enter_edit_mode()
        if not clicked:
            self._log("  未找到可点击的「编辑」按钮。")
            return self.is_edit_form_ready()

        if not self.wait_for_edit_form_ready(timeout=timeout):
            self._log(
                f"  点击「{clicked}」后编辑表单未就绪（{self.summarize_page_mode()}）。"
            )
            return False

        self._log(
            f"  编辑表单已就绪（{self.summarize_page_mode()}）。"
        )
        if not self._attachment_cleared():
            self._clear_previous_attachment()
        return self.is_edit_form_ready()

    def get_current_grading_rule(self) -> str:
        driver = self._ensure_driver()
        return driver.execute_script(f"return ({self._read_grading_rule_js()});") or ""

    def wait_for_rule_selector(self, timeout: int = 30) -> bool:
        driver = self._ensure_driver()
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: bool(
                    d.execute_script(f"return ({self._read_grading_rule_js()});")
                )
            )
            return True
        except TimeoutException:
            return False

    def _ensure_evaluate_form_context(self, rule_id: str | None = None) -> None:
        """进入批改 iframe，并等待规则选择器与 CSRF 就绪。"""
        rule_id = rule_id or yuketang_form_rule_id()
        self._switch_to_default_content()
        driver = self._ensure_driver()
        target_url = yuketang_evaluate_url(rule_id)
        url = driver.current_url or ""
        on_evaluate = (
            "correcting-assistant-evaluate" in url and f"rule_id={rule_id}" in url
        )

        if not on_evaluate:
            driver.get(target_url)
            time.sleep(2)
        else:
            self._log("  已在批改页，跳过整页刷新。")

        self._ensure_on_evaluate_page()
        self.dismiss_page_notifications()
        if self._is_on_agreement_page() or self._is_on_login_page():
            raise RuntimeError(
                "当前在登录页/协议页，未进入 AI 批改页。"
                f"请手动打开: {yuketang_evaluate_url()}"
            )
        if not self.switch_to_workspace_iframe(wait_seconds=25, require_rule=True):
            raise RuntimeError("无法进入雨课堂 AI 批改页（可能未登录或 URL 不正确）。")
        if not self.ensure_csrf_sync(leave_iframe=False):
            raise RuntimeError(
                "CSRF token 未就绪，无法提交评分。"
                "请运行: uv run python main.py yuketang-login"
            )
        if not self.wait_for_rule_selector(timeout=30):
            rule_hint = self.get_current_grading_rule() or "(空)"
            raise RuntimeError(
                f"批改规则选择器未加载（当前规则文本: {rule_hint}）。"
                "请确认 Chrome 中批改页已完全加载。"
            )
        # 后续填表/上传操作均在 iframe 内进行

    def prepare_reflective_edit_form(self, rule_name: str = "Reflective") -> None:
        """从 Scientific(859) 空白表单进入，再切 Reflective，避免 860 载入他人历史 case。"""
        self._active_rule = None
        self._form_ready = False
        self._switch_to_default_content()
        driver = self._ensure_driver()
        driver.get(yuketang_evaluate_url(yuketang_form_rule_id()))
        self._history_panel_open = False
        time.sleep(2)
        self.dismiss_page_notifications()
        if self._is_on_login_page():
            raise RuntimeError("雨课堂未登录，请先运行 yuketang-login。")
        if not self.switch_to_workspace_iframe(wait_seconds=25, require_rule=True):
            raise RuntimeError("无法进入雨课堂 AI 批改页。")
        if not self.ensure_csrf_sync(leave_iframe=False):
            raise RuntimeError("CSRF 未就绪，请重新运行 yuketang-login。")
        if not self.wait_for_rule_selector(timeout=30):
            raise RuntimeError("批改规则选择器未加载。")

        current = self.get_current_grading_rule()
        if current != rule_name:
            self._log(f"  [1/4] 切换批改规则: {current or '未知'} -> {rule_name}")
            if not self.ensure_grading_rule(rule_name):
                raise RuntimeError(f"无法切换到批改规则: {rule_name}")
        else:
            self._log(f"  [1/4] 批改规则已是: {rule_name}")

        time.sleep(2)
        self._log(
            "  [2/4] Reflective 会载入上次 case，"
            "接下来先「编辑」并删除旧附件/题目…"
        )

        self._form_ready = True
        self._active_rule = rule_name
        self.clear_submission_form()
        self._log("  [3/4] 空白表单就绪，填写当前学生题目与附件。")

    def prepare_upload_form(self) -> None:
        """兼容：优先 Reflective + 编辑；Scientific 仅作 fallback。"""
        self.prepare_reflective_edit_form("Reflective")

    def prepare_evaluate_form(self, rule_name: str) -> None:
        self.prepare_reflective_edit_form(rule_name)

    def open_evaluate_page(self) -> None:
        self.prepare_reflective_edit_form("Reflective")

    def get_page_text(self) -> str:
        return self.driver.page_source if self.driver else ""

    def click_by_visible_text(self, text: str, exact: bool = False) -> bool:
        if not self._driver_alive():
            return False
        driver = self._ensure_driver()
        script = """
        const target = arguments[0].toLowerCase();
        const exact = arguments[1];
        const nodes = [...document.querySelectorAll('button, a, span, div, label, li, p')];
        for (const node of nodes) {
          const txt = (node.innerText || node.textContent || '').trim();
          if (!txt) continue;
          const ok = exact ? txt.toLowerCase() === target : txt.toLowerCase().includes(target);
          if (!ok) continue;
          if (node.offsetParent === null) continue;
          node.click();
          return true;
        }
        return false;
        """
        return bool(driver.execute_script(script, text, exact))

    def ensure_grading_rule(self, rule_name: str) -> bool:
        if self._active_rule and self._active_rule != rule_name:
            self._log("  警告：表单已填写，不能再次切换批改规则。")
            return False
        if self.get_current_grading_rule() == rule_name:
            return True
        driver = self._ensure_driver()
        opened = driver.execute_script(
            "const sel = document.querySelector('.rule-select-cmp .res-text, .top-rule-box .res-text');"
            "if (sel) { sel.click(); return true; } return false;"
        )
        if not opened:
            return False
        clicked = bool(
            driver.execute_script(
                """
                const rule = arguments[0];
                for (const el of document.querySelectorAll('.rule-name')) {
                  if ((el.innerText || '').trim() === rule) { el.click(); return true; }
                }
                return false;
                """,
                rule_name,
            )
        )
        if not clicked:
            return False
        try:
            WebDriverWait(driver, 12).until(
                lambda d: self.get_current_grading_rule() == rule_name
            )
        except TimeoutException:
            return False
        return True

    def get_topic_field_text(self) -> str:
        driver = self._ensure_driver()
        return (
            driver.execute_script(TOPIC_EDITOR_JS + "return __getTopicPlainText();")
            or ""
        )

    def _wait_for_ueditor_ready(self, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready = self.get_form_readiness()
            if ready.get("hasUE") or ready.get("hasIframe"):
                return True
            time.sleep(0.5)
        return False

    def _set_topic_via_ueditor(self, value: str) -> bool:
        driver = self._ensure_driver()
        return bool(
            driver.execute_script(
                TOPIC_EDITOR_JS
                + """
                const text = arguments[0];
                const html = text.split('\\n').map(line => '<p>' + line + '</p>').join('');
                const ed = __getTopicEditor();
                if (!ed) return false;
                ed.focus();
                ed.setContent(html, false);
                ed.fireEvent('contentChange');
                ed.fireEvent('blur');
                return __getTopicPlainText().length > 50;
                """,
                value,
            )
        )

    def _paste_topic_via_clipboard(self, value: str) -> bool:
        """填写 UEditor 题目栏；优先 setContent，失败再剪贴板粘贴。"""
        driver = self._ensure_driver()
        if not self._wait_for_ueditor_ready(timeout=30):
            self._log("  题目编辑器未就绪。")
            return False

        for attempt in range(1, 4):
            if self._set_topic_via_ueditor(value):
                time.sleep(0.5)
                if len(self.get_topic_field_text().strip()) > 50:
                    return True
            time.sleep(0.8)

        subprocess.run(["pbcopy"], input=value.encode("utf-8"), check=True)
        for attempt in range(1, 4):
            try:
                driver.execute_script(
                    TOPIC_EDITOR_JS
                    + """
                    const ed = __getTopicEditor();
                    if (ed) { ed.focus(); ed.setContent('', false); }
                    """
                )
                pasted = driver.execute_script(
                    TOPIC_EDITOR_JS
                    + """
                    const iframe = document.querySelector('#UEditor_q_title iframe');
                    if (!iframe || !iframe.contentDocument) return false;
                    const body = iframe.contentDocument.body;
                    body.focus();
                    body.click();
                    return true;
                    """
                )
                if not pasted:
                    continue
                editor_iframe = driver.find_element(
                    By.CSS_SELECTOR, "#UEditor_q_title iframe"
                )
                driver.switch_to.frame(editor_iframe)
                ActionChains(driver).key_down(Keys.COMMAND).send_keys("v").key_up(
                    Keys.COMMAND
                ).perform()
                driver.switch_to.parent_frame()
                driver.execute_script(
                    TOPIC_EDITOR_JS
                    + """
                    const ed = __getTopicEditor();
                    if (ed) {
                      ed.fireEvent('contentChange');
                      ed.fireEvent('blur');
                    }
                    """
                )
                time.sleep(0.5)
                if len(self.get_topic_field_text().strip()) > 50:
                    return True
            except StaleElementReferenceException:
                driver.switch_to.default_content()
                if not self.switch_to_workspace_iframe(wait_seconds=10):
                    return False
                time.sleep(0.5)
        return len(self.get_topic_field_text().strip()) > 50

    def set_topic_field(self, value: str) -> bool:
        """题目栏使用 UEditor 富文本，格式为「题目标题 + requirement 全文」。"""
        if not self._form_ready:
            self._log("  错误：请先调用 prepare_upload_form 打开上传表单。")
            return False

        if not self._wait_for_ueditor_ready(timeout=20):
            if len(self.get_topic_field_text().strip()) <= 50:
                return False

        if not self._paste_topic_via_clipboard(value):
            actual = len(self.get_topic_field_text().strip())
            if actual > 50:
                self._log(f"  粘贴未确认，但题目栏已有内容（{actual} 字），继续。")
                return True
            return False
        actual = len(self.get_topic_field_text().strip())
        if actual <= 50:
            return False
        self._log(f"  [3/4] 已填写题目（{actual} 字）。")
        return True

    def _sync_form_validation(self) -> None:
        driver = self._ensure_driver()
        driver.execute_script(
            TOPIC_EDITOR_JS
            + """
            const ed = __getTopicEditor();
            if (ed) {
              ed.fireEvent('contentChange');
              ed.fireEvent('blur');
            }
            """
        )
        time.sleep(0.5)

    def _wait_for_submit_enabled(self, timeout: int = 90) -> bool:
        driver = self._ensure_driver()
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: self._submit_button_enabled()
            )
            return True
        except TimeoutException:
            return False

    def _topic_is_filled(self, min_len: int = 50) -> bool:
        return len(self.get_topic_field_text().strip()) >= min_len

    def _ensure_topic_filled(self, topic_text: str) -> bool:
        if self._topic_is_filled():
            return True
        self._log("  题目栏为空，正在填写…")
        self._wait_for_ueditor_ready(timeout=30)
        if not self.set_topic_field(topic_text):
            if not self._topic_is_filled():
                ready = self.get_form_readiness()
                self._log(
                    f"  题目填写失败（UE={ready.get('hasUE')}, "
                    f"iframe={ready.get('hasIframe')}）。"
                )
                return False
        self._sync_form_validation()
        if self._topic_is_filled():
            self._log(f"  题目已填写（{len(self.get_topic_field_text().strip())} 字）。")
            return True
        return False

    def fill_submission(self, topic_text: str, file_path: Path) -> bool:
        """先上传附件，再填题目（上传常会清空 UEditor 题目栏）。"""
        if not self.ensure_edit_form(timeout=25):
            self._log(f"  无法进入编辑模式: {self.summarize_page_mode()}")
            return False

        if not self.upload_submission_file(file_path):
            if self._submit_button_enabled() and self._topic_is_filled():
                attach = self.get_upload_state().get("attach") or ""
                if file_path.name.lower() in attach.lower():
                    self._log("  上传步骤未确认，但表单已就绪，继续。")
                else:
                    return False
            else:
                return False

        shown = self.get_submission_attachment_name() or (
            self.get_upload_state().get("attach") or ""
        )
        self._log(f"  已上传 {file_path.name}（页面显示: {shown[:50]}）")

        if not self._ensure_topic_filled(topic_text):
            self._log(
                f"  题目填写失败，当前 {len(self.get_topic_field_text().strip())} 字。"
            )
            return False

        self._sync_form_validation()
        if self._wait_for_submit_enabled(timeout=90):
            self._log("  「开始评分」按钮已可用。")
            return True
        btn = self.get_submit_button_state()
        topic_len = len(self.get_topic_field_text().strip())
        self._log(
            f"  「开始评分」仍不可用: {btn}，题目长度={topic_len}，"
            f"附件={self.get_upload_state().get('attach', '')[:40]}"
        )
        return False

    def get_upload_state(self) -> dict:
        driver = self._ensure_driver()
        return (
            driver.execute_script(
                """
                const slot = (document.querySelector('.upload-attachment-wrapper')?.innerText || '').trim();
                const previewName = (
                  document.querySelector('.test-input-wrapper .attachment-preview .name-txt')?.innerText || ''
                ).trim();
                const attach = slot || previewName;
                const hasDocx = /\\.docx/i.test(attach) || /\\.docx/i.test(previewName);
                const uploading = attach.includes('0%') || attach.includes('上传中');
                const emptySlot = attach.includes('可上传');
                const hasDelete = attach.includes('删除') || !!previewName;
                const uploaded = hasDocx && hasDelete && !uploading && !emptySlot;
                return {
                  attach: attach.slice(0, 160),
                  uploaded: uploaded,
                  uploading: uploading,
                  has_attachment: hasDocx && !emptySlot && !uploading,
                };
                """
            )
            or {}
        )

    def _upload_slot_ready(self) -> bool:
        attach = (self.get_upload_state().get("attach") or "").strip()
        return "可上传" in attach

    def _wait_upload_slot_ready(self, timeout: int = 15) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._upload_slot_ready() or self._attachment_cleared():
                return True
            time.sleep(0.5)
        return self._upload_slot_ready()

    def _find_submission_file_input(self):
        """仅定位左侧作答区 upload wrapper 内的 file input。"""
        driver = self._ensure_driver()
        el = driver.execute_script(
            """
            const roots = document.querySelectorAll(
              '.test-input-wrapper, .text-input-content'
            );
            for (const root of roots) {
              const wrapper = root.querySelector('.upload-attachment-wrapper');
              if (!wrapper) continue;
              const input = wrapper.querySelector('input[type="file"]');
              if (input) return input;
            }
            const fallback = document.querySelector(
              '.upload-attachment-wrapper input[type="file"]'
            );
            return fallback || null;
            """
        )
        if el:
            return el
        selectors = [
            ".test-input-wrapper input[type='file']",
            ".text-input-content input[type='file']",
            "input.el-upload__input",
        ]
        for sel in selectors:
            for candidate in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if candidate.is_enabled():
                        return candidate
                except StaleElementReferenceException:
                    continue
        return None

    def _click_delete_on_upload_wrapper(self) -> bool:
        driver = self._ensure_driver()
        return bool(
            driver.execute_script(
                """
                const wrappers = document.querySelectorAll(
                  '.test-input-wrapper .upload-attachment-wrapper,'
                  + '.text-input-content .upload-attachment-wrapper,'
                  + '.upload-attachment-wrapper'
                );
                for (const wrapper of wrappers) {
                  for (const el of wrapper.querySelectorAll('.btn, span, a, div')) {
                    const txt = (el.innerText || el.textContent || '').trim();
                    if (txt !== '删除') continue;
                    if (el.offsetParent === null) continue;
                    el.click();
                    return true;
                  }
                }
                return false;
                """
            )
        )

    def _recover_edit_form(self) -> bool:
        driver = self._ensure_driver()
        driver.execute_script(
            """
            const btn = document.querySelector('.bottom-btn-box .btn-cancel');
            if (btn && btn.offsetParent !== null) btn.click();
            """
        )
        time.sleep(1.5)
        return self.ensure_edit_form(timeout=25)

    def _clear_file_inputs(self) -> None:
        driver = self._ensure_driver()
        driver.execute_script(
            """
            for (const input of document.querySelectorAll(
              '.upload-attachment-wrapper input[type="file"],'
              + '.test-input-wrapper input[type="file"],'
              + 'input.el-upload__input'
            )) {
              input.value = '';
            }
            """
        )

    def _reset_upload_slot(self) -> bool:
        """点「删除」直到出现「可上传」，再清空 file input。"""
        self.ensure_edit_form(timeout=15)
        attach = self.get_submission_attachment_name() or (
            self.get_upload_state().get("attach") or ""
        )
        if self._upload_slot_ready():
            self._clear_file_inputs()
            return True

        self._log(f"  重置上传区：点击删除「{attach[:50]}」…")
        for attempt in range(1, 5):
            clicked = (
                self._click_delete_on_upload_wrapper()
                or self._click_delete_attachment()
            )
            if not clicked:
                self._log(f"  第 {attempt} 次未点到「删除」按钮。")
            else:
                time.sleep(0.6)
                self._dismiss_confirm_dialog()
            if self._wait_upload_slot_ready(timeout=8):
                self._log("  已删除卡住的上传，恢复「可上传」入口。")
                self._clear_file_inputs()
                return True
            time.sleep(0.8)

        self._log("  删除后仍未恢复上传入口，尝试「取消」后重新编辑…")
        if self._recover_edit_form() and self._wait_upload_slot_ready(timeout=10):
            self._clear_file_inputs()
            return True
        return self._upload_slot_ready()

    def _send_file_to_input(self, file_path: Path, reset_input: bool = False) -> bool:
        driver = self._ensure_driver()
        abs_path = str(file_path.resolve())
        if not self._wait_upload_slot_ready(timeout=15):
            self._log("  上传入口未就绪（未出现「可上传」）。")
            return False

        try:
            file_input = WebDriverWait(driver, 15).until(
                lambda d: self._find_submission_file_input()
            )
        except TimeoutException:
            return False

        driver.execute_script(
            """
            const input = arguments[0];
            const reset = arguments[1];
            if (reset) input.value = '';
            input.removeAttribute('hidden');
            """,
            file_input,
            reset_input,
        )
        file_input = self._find_submission_file_input()
        if not file_input:
            return False
        file_input.send_keys(abs_path)
        driver.execute_script(
            """
            const input = arguments[0];
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            try {
              input.dispatchEvent(new InputEvent('input', { bubbles: true }));
            } catch (e) {}
            """,
            file_input,
        )
        return True

    def upload_submission_file(self, file_path: Path, max_attempts: int = 4) -> bool:
        expected_name = file_path.name
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self._log(f"  第 {attempt}/{max_attempts} 次重新上传…")
                if not self._reset_upload_slot():
                    self._log("  无法恢复「可上传」入口。")
                    continue
                time.sleep(0.8)

            if not self.ensure_edit_form(timeout=15):
                self._log("  未处于编辑模式，无法上传。")
                continue
            if not self._send_file_to_input(file_path, reset_input=attempt > 1):
                self._log("  未找到附件上传 input 或入口未就绪。")
                continue

            self._log(f"  已通过隐藏 input 提交文件: {expected_name}")
            if self.wait_for_upload_complete(
                expected_name, timeout=90, stuck_timeout=40
            ):
                return True
            if self._submit_button_enabled():
                attach = self.get_upload_state().get("attach") or ""
                if expected_name.lower() in attach.lower():
                    self._log("  「开始评分」按钮已可用（附件应已上传）。")
                    return True

        state = self.get_upload_state()
        self._log(
            f"  附件上传失败（已重试 {max_attempts} 次）: "
            f"{state.get('attach', '')[:80]}"
        )
        return False

    def wait_for_upload_complete(
        self,
        expected_name: str = "",
        timeout: int = 90,
        stuck_timeout: int = 40,
    ) -> bool:
        start = time.time()
        last_log = 0.0
        wrong_name_since: float | None = None
        stuck_since: float | None = None
        while time.time() - start < timeout:
            state = self.get_upload_state()
            attach = state.get("attach") or ""
            name_ok = (
                not expected_name
                or expected_name.lower() in attach.lower()
            )
            if (
                expected_name
                and state.get("has_attachment")
                and not name_ok
                and not state.get("uploading")
            ):
                if wrong_name_since is None:
                    wrong_name_since = time.time()
                elif time.time() - wrong_name_since > 25:
                    self._log(
                        f"  附件仍为旧文件「{attach[:40]}」，"
                        f"期望「{expected_name[:40]}」。"
                    )
                    return False
            else:
                wrong_name_since = None

            if state.get("uploaded") and name_ok:
                self._log(f"  附件上传完成: {attach[:60]}")
                return True

            is_stuck = (
                state.get("uploading")
                and ("0%" in attach or "上传中" in attach)
            )
            if is_stuck:
                if stuck_since is None:
                    stuck_since = time.time()
                elif time.time() - stuck_since >= stuck_timeout:
                    self._log(
                        f"  上传卡在 0% 超过 {stuck_timeout}s，"
                        f"将删除并重新上传…"
                    )
                    return False
            else:
                stuck_since = None

            if state.get("uploading") and time.time() - last_log > 10:
                self._log(f"  附件上传中… {attach[:40]}")
                last_log = time.time()
            time.sleep(1)
        state = self.get_upload_state()
        self._log(f"  附件上传未完成: {state.get('attach', '')[:80]}")
        return False

    def _submit_button_enabled(self) -> bool:
        btn = self.get_submit_button_state()
        text = btn.get("text") or ""
        return (
            btn.get("found")
            and "开始评分" in text
            and not btn.get("disabled")
        )

    def get_submit_button_state(self) -> dict:
        driver = self._ensure_driver()
        return driver.execute_script(
            """
            const btn = document.querySelector('.bottom-btn-box .btn-confirm, .btn-confirm');
            if (!btn) return {found: false};
            const text = (btn.querySelector('span')?.innerText || btn.innerText || '').trim();
            const disabled = btn.classList.contains('disabled')
              || btn.classList.contains('is-disabled')
              || btn.getAttribute('aria-disabled') === 'true';
            return { found: true, text, disabled };
            """
        ) or {}

    def wait_until_ready_to_grade(self, timeout: int = 30) -> bool:
        driver = self._ensure_driver()
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: self._is_ready_to_grade()
            )
            return True
        except TimeoutException:
            return False

    def _is_ready_to_grade(self) -> bool:
        """以页面按钮状态为准：雨课堂仅在题目+附件就绪时才启用「开始评分」。"""
        if self._submit_button_enabled():
            return True
        topic_ok = len(self.get_topic_field_text().strip()) > 50
        upload_ok = bool(
            self.get_upload_state().get("uploaded")
            or self.get_upload_state().get("has_attachment")
        )
        return topic_ok and upload_ok

    def get_form_state(self) -> dict:
        return {
            "topic_len": len(self.get_topic_field_text().strip()),
            "upload": self.get_upload_state(),
            "button": self.get_submit_button_state(),
            "ready": self._is_ready_to_grade(),
        }

    def click_start_grading(self) -> bool:
        if not self._submit_button_enabled():
            self._log("  等待「开始评分」按钮变为可点…")
            if not self._wait_for_submit_enabled(timeout=60):
                self._log(f"  无法点击「开始评分」，当前状态: {self.get_form_state()}")
                return False

        driver = self._ensure_driver()
        self._grading_started_at = datetime.now()
        self._known_download_hrefs = self._collect_result_download_hrefs()

        for attempt in range(1, 4):
            self._log(f"  正在点击「开始评分」（第 {attempt}/3 次）…")
            clicked = False
            try:
                btn_el = driver.find_element(
                    By.CSS_SELECTOR, ".bottom-btn-box .btn-confirm, .btn-confirm"
                )
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", btn_el
                )
                time.sleep(0.3)
                if btn_el.get_attribute("class") and "disabled" in btn_el.get_attribute(
                    "class"
                ):
                    self._log("  「开始评分」按钮不可用。")
                    return False
                ActionChains(driver).move_to_element(btn_el).pause(0.2).click(
                    btn_el
                ).perform()
                clicked = True
            except WebDriverException:
                clicked = bool(
                    driver.execute_script(
                        """
                        const btn = document.querySelector(
                          '.bottom-btn-box .btn-confirm, .btn-confirm'
                        );
                        if (!btn || btn.classList.contains('disabled')) return false;
                        const span = btn.querySelector('span');
                        (span || btn).click();
                        return true;
                        """
                    )
                )

            if not clicked:
                self._log(f"  点击「开始评分」失败（第 {attempt}/3 次）。")
                time.sleep(1.5)
                continue

            for _ in range(8):
                time.sleep(1)
                btn_text = self.get_submit_button_state().get("text", "")
                if btn_text == "评分中":
                    self._log("  「开始评分」已受理，按钮变为「评分中」。")
                    return True
                panel = self.get_result_panel_state()
                if panel.get("score") and panel.get("hasHeader"):
                    self._log(
                        f"  「开始评分」已生效，右侧出分 {panel.get('score')}。"
                    )
                    return True

            self._log(f"  第 {attempt}/3 次点击后按钮仍为「开始评分」，重试…")

        self._log("  「开始评分」多次点击后仍未受理。")
        return False

    def wait_for_grading_submitted(self, timeout: int = 90) -> bool:
        """等待提交被受理（「评分中」或右侧已出分），即可继续下一份。"""
        start = time.time()
        while time.time() - start < timeout:
            btn_text = self.get_submit_button_state().get("text", "")
            if btn_text == "评分中":
                self._log("  提交已受理（评分中），继续下一份。")
                return True

            panel = self.get_result_panel_state()
            if panel.get("score") and panel.get("hasHeader"):
                self._log(
                    f"  右侧已出分（{panel.get('score')}），继续下一份。"
                )
                return True
            if panel.get("ready"):
                self._log("  右侧结果已就绪，继续下一份。")
                return True

            time.sleep(1.5)

        state = self.get_submit_button_state()
        self._log(f"  等待提交受理超时，按钮: {state.get('text')}")
        return False

    _RESULT_PANEL_JS = """
        const box = document.querySelector('.ai-correction-box');
        if (!box) return { ready: false, reason: 'no panel' };
        const headerText = (
          box.querySelector('.result-tip .header-title')?.innerText
          || box.querySelector('.result-tip')?.innerText
          || ''
        ).trim();
        const hasHeader = headerText.includes('为您生成以下结果');
        const score = (box.querySelector('.get-score')?.innerText || '').trim();
        const inHistoryMode = !hasHeader && !!(
          box.querySelector('.top-close-btn')?.offsetParent
        );
        const items = [];
        const seen = new Set();
        const pushItem = (root) => {
          if (!root) return;
          const text = (root.innerText || '').trim();
          const inAnno = !!root.closest('.ai-correction-annotation');
          if (!text.includes('智能批注') && !inAnno) return;
          const timeMatch = text.match(/\\d{4}-\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}:\\d{2}/);
          const name = (root.querySelector('.name-txt')?.innerText || '').trim();
          for (const a of root.querySelectorAll('a[href], a[download]')) {
            const label = (a.innerText || a.textContent || '').trim();
            const href = a.href || '';
            if (!href) continue;
            if (!label.includes('下载') && !/\\.docx/i.test(href)) continue;
            if (seen.has(href)) continue;
            seen.add(href);
            items.push({
              name: name || label,
              href,
              timestamp: timeMatch ? timeMatch[0] : '',
              label: label || '下载',
            });
          }
        };
        for (const li of box.querySelectorAll('.anno-list.ai-correction-annotation li')) {
          pushItem(li);
        }
        for (const section of box.querySelectorAll('.correction-section')) {
          const title = (section.querySelector('.title')?.innerText || '').trim();
          if (!title.includes('智能批注')) continue;
          for (const li of section.querySelectorAll('li')) pushItem(li);
          for (const preview of section.querySelectorAll('.attachment-preview')) {
            pushItem(preview);
          }
        }
        const hasDownload = items.length > 0;
        return {
          ready: hasDownload && !inHistoryMode,
          hasHeader,
          hasDownload,
          inHistoryMode,
          score,
          items,
          headerText: headerText.slice(0, 80),
        };
    """

    def _collect_result_download_hrefs(self) -> set[str]:
        panel = self.get_result_panel_state()
        return {item["href"] for item in panel.get("items", []) if item.get("href")}

    def _ensure_panel_context(self) -> bool:
        return self.switch_to_workspace_iframe(wait_seconds=10)

    def get_result_panel_state(self) -> dict:
        self._ensure_panel_context()
        driver = self._ensure_driver()
        script = f"return (function() {{{self._RESULT_PANEL_JS}}})()"
        return driver.execute_script(script) or {}

    def _panel_is_downloadable(self, panel: dict) -> bool:
        if panel.get("inHistoryMode"):
            return False
        if panel.get("hasDownload") or panel.get("items"):
            return True
        return bool(panel.get("hasHeader"))

    def _parse_panel_timestamp(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _select_feedback_download(
        self,
        panel: dict,
        since: datetime | None = None,
        expected_name: str = "",
        exclude_hrefs: set[str] | None = None,
    ) -> dict | None:
        exclude = exclude_hrefs or set()
        items = [
            item
            for item in (panel.get("items") or [])
            if item.get("href") and item["href"] not in exclude
        ]
        if not items:
            return None

        if expected_name:
            exp = expected_name.lower()
            exp_stem = Path(expected_name).stem.lower()
            alt = expected_name.replace("#", "No.").lower()
            alt_stem = (
                Path(expected_name.replace("#", "No.")).stem.lower()
                if "#" in expected_name
                else ""
            )
            named = [
                item
                for item in items
                if exp in (item.get("name") or "").lower()
                or exp_stem in (item.get("name") or "").lower()
                or (alt and alt in (item.get("name") or "").lower())
                or (alt_stem and alt_stem in (item.get("name") or "").lower())
            ]
            if named:
                items = named
            else:
                return None

        if len(items) == 1:
            return items[0]

        cutoff_base = since or self._grading_started_at or datetime.now()
        cutoff = cutoff_base - timedelta(minutes=2)
        timed_matches: list[tuple[datetime, dict]] = []
        new_items: list[dict] = []

        for item in items:
            href = item.get("href") or ""
            ts = self._parse_panel_timestamp(item.get("timestamp") or "")
            if ts and ts >= cutoff:
                timed_matches.append((ts, item))
            elif href and href not in self._known_download_hrefs:
                new_items.append(item)

        if timed_matches:
            timed_matches.sort(key=lambda pair: pair[0], reverse=True)
            return timed_matches[0][1]
        if new_items:
            return new_items[0]
        return items[0]

    def _is_history_panel_open(self) -> bool:
        panel = self.get_result_panel_state()
        if panel.get("hasHeader"):
            return False
        return bool(panel.get("inHistoryMode"))

    def _close_history_panel(self) -> bool:
        if not self._is_history_panel_open():
            self._history_panel_open = False
            return True
        driver = self._ensure_driver()
        closed = driver.execute_script(
            """
            for (const sel of [
              '.top-close-btn.pointer',
              '.top-close-btn',
              '.drawer-header-custom .icon-cuowu',
              '.drawer-header-custom span.text-btn-hover',
              '.el-drawer__close-btn',
            ]) {
              const el = document.querySelector(sel);
              if (!el || el.offsetParent === null) continue;
              const clickTarget = el.closest('.top-close-btn, span, button, div') || el;
              clickTarget.click();
              return true;
            }
            return false;
            """
        )
        if closed:
            self._history_panel_open = False
            time.sleep(1)
            self._log("  已关闭历史批改，回到本次结果。")
        return closed or not self._is_history_panel_open()

    def _ensure_current_result_panel(self, timeout: int = 15) -> bool:
        """关闭历史批改浮层，确保右侧本次结果区可下载。"""
        self._ensure_panel_context()
        if self._close_history_panel():
            deadline = time.time() + timeout
            while time.time() < deadline:
                panel = self.get_result_panel_state()
                if self._panel_is_downloadable(panel):
                    return True
                time.sleep(0.5)
        panel = self.get_result_panel_state()
        return self._panel_is_downloadable(panel)

    def _open_history_panel(self) -> bool:
        if self._history_panel_open:
            return True
        driver = self._ensure_driver()
        opened = driver.execute_script(
            """
            const box = document.querySelector('.ai-correction-box');
            if (!box) return false;
            for (const el of box.querySelectorAll('span, p, div')) {
              if ((el.innerText || '').trim() === '历史批改' && el.offsetParent !== null) {
                el.click();
                return true;
              }
            }
            return false;
            """
        )
        if opened:
            self._history_panel_open = True
            time.sleep(1.5)
        return bool(opened)

    _HISTORY_ENTRIES_JS = """
        const box = document.querySelector('.ai-correction-box');
        if (!box) return [];
        const entries = [];
        const seen = new Set();
        const pushEntry = (root) => {
          if (!root) return;
          const text = (root.innerText || '').trim();
          if (!text.includes('智能批注') && !root.closest('.ai-correction-annotation')) {
            return;
          }
          const docxName = (root.querySelector('.name-txt')?.innerText || '').trim();
          const timeMatch = text.match(/\\d{4}-\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}:\\d{2}/);
          const score = (root.querySelector('.get-score')?.innerText || '').trim();
          for (const a of root.querySelectorAll('a[href]')) {
            const label = (a.innerText || a.textContent || '').trim();
            if (!label.includes('下载')) continue;
            const href = a.href || '';
            if (!href) continue;
            const key = href + '|' + docxName;
            if (seen.has(key)) continue;
            seen.add(key);
            entries.push({
              docx_name: docxName,
              href,
              timestamp: timeMatch ? timeMatch[0] : '',
              score,
              is_annotation: true,
            });
          }
        };
        for (const li of box.querySelectorAll('.anno-list.ai-correction-annotation li')) {
          pushEntry(li);
        }
        for (const section of box.querySelectorAll('.correction-section')) {
          const title = (section.querySelector('.title')?.innerText || '').trim();
          if (!title.includes('智能批注')) continue;
          for (const li of section.querySelectorAll('li')) {
            pushEntry(li);
          }
        }
        return entries;
    """

    def get_history_entries(self) -> list[dict]:
        self._open_history_panel()
        driver = self._ensure_driver()
        script = f"return (function() {{{self._HISTORY_ENTRIES_JS}}})()"
        return driver.execute_script(script) or []

    def get_submission_attachment_name(self) -> str:
        """左侧作答区显示的上传文件名（与右侧智能批注下载名可能不同）。"""
        driver = self._ensure_driver()
        return (
            driver.execute_script(
                """
                return (
                  document.querySelector(
                    '.test-input-wrapper .attachment-preview .name-txt,'
                    + ' .text-input-content .attachment-preview .name-txt'
                  )?.innerText || ''
                ).trim();
                """
            )
            or ""
        )

    def _select_history_entry(
        self,
        entries: list[dict],
        since: datetime | None = None,
        exclude_hrefs: set[str] | None = None,
    ) -> dict | None:
        """按提交时间选取智能批注下载项（下载名常为 essay1.docx 等，不能靠原文件名匹配）。"""
        exclude = exclude_hrefs or set()
        cutoff = since - timedelta(minutes=2) if since else None
        matches: list[tuple[datetime, dict]] = []
        for entry in entries:
            href = entry.get("href") or ""
            if not href or href in exclude:
                continue
            ts = self._parse_panel_timestamp(entry.get("timestamp") or "")
            if cutoff and ts and ts < cutoff:
                continue
            matches.append((ts or datetime.min, entry))
        if not matches:
            return None
        matches.sort(key=lambda pair: pair[0], reverse=True)
        return matches[0][1]

    @staticmethod
    def _is_oss_error_file(path: Path) -> bool:
        if not path.exists() or path.stat().st_size > 8192:
            return False
        try:
            text = path.read_bytes()[:512].decode("utf-8", errors="ignore")
        except OSError:
            return False
        return "NoSuchKey" in text or (
            text.lstrip().startswith("<?xml") and "<Error>" in text
        )

    @staticmethod
    def _is_valid_docx_file(path: Path, min_size: int = 1024) -> bool:
        if not path.exists() or path.stat().st_size < min_size:
            return False
        if YuketangBrowser._is_oss_error_file(path):
            return False
        try:
            return path.read_bytes()[:2] == b"PK"
        except OSError:
            return False

    def download_feedback(
        self,
        dest_path: Path,
        since: datetime | None = None,
        exclude_hrefs: set[str] | None = None,
        skip_local_names: set[str] | None = None,
        expected_source_name: str = "",
    ) -> tuple[bool, str]:
        """从右侧「本次结果」下载 feedback（不进入历史批改列表）。"""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        exclude = exclude_hrefs if exclude_hrefs is not None else self._downloaded_hrefs
        skip = skip_local_names or set()

        panel = self.get_result_panel_state()
        if not self._panel_is_downloadable(panel):
            if not self._ensure_current_result_panel():
                panel = self.get_result_panel_state()
            if not self._panel_is_downloadable(panel):
                if self._is_history_panel_open():
                    self._log("  仍在历史批改列表，无法下载本次结果。")
                else:
                    self._log(
                        f"  右侧暂无可下载项（header={panel.get('hasHeader')}, "
                        f"items={len(panel.get('items') or [])}, "
                        f"reason={panel.get('reason', '')}）。"
                    )
                return False, "no_panel"

        panel = self.get_result_panel_state()
        merged_exclude = set(exclude)
        download = self._select_feedback_download(
            panel,
            since=since,
            expected_name=expected_source_name,
            exclude_hrefs=merged_exclude,
        )
        if not download or not download.get("href"):
            if expected_source_name:
                self._log(
                    f"  右侧未找到与「{expected_source_name[:40]}」匹配的下载项。"
                )
            else:
                self._log(f"  本次结果区未找到可下载项: {panel}")
            return False, "no_match"

        href = download["href"]
        remote_name = download.get("name") or "(未知文件名)"
        self._log(
            f"  正在下载本次智能批注: {remote_name[:50]}"
            f" -> {dest_path.name}"
            f" @ {download.get('timestamp') or '(无时间戳)'}"
        )
        if href in exclude:
            return False, "excluded"
        ok, reason = self._save_download_href(href, dest_path, skip_names=skip)
        if ok:
            self._downloaded_hrefs.add(href)
            self._last_failed_download_href = ""
            return True, ""
        if reason in {"no_such_key", "http_error"}:
            self._last_failed_download_href = href
            self._downloaded_hrefs.add(href)
        return False, reason

    def _save_download_href(
        self, href: str, dest_path: Path, skip_names: set[str] | None = None
    ) -> tuple[bool, str]:
        skip = {n.lower() for n in (skip_names or set()) if n}
        deadline = time.time() + 120
        last_retry = 0.0

        while time.time() < deadline:
            dl_ok, dl_reason = self._download_url_with_cookies(href, dest_path)
            if dl_ok:
                if self._is_valid_docx_file(dest_path):
                    self._log(f"  已保存 feedback: {dest_path.name}")
                    return True, ""
                if self._is_oss_error_file(dest_path):
                    dest_path.unlink(missing_ok=True)
                    self._log(
                        "  OSS 返回 NoSuchKey（链接无效，需重新评分生成 feedback）。"
                    )
                    return False, "no_such_key"
                dest_path.unlink(missing_ok=True)
                if time.time() - last_retry > 8:
                    self._log("  下载链接已响应但 docx 尚未就绪，继续等待…")
                    last_retry = time.time()
                time.sleep(3)
                continue
            if dl_reason == "no_such_key":
                return False, "no_such_key"
            break

        self._set_download_dir(dest_path.parent)
        driver = self._ensure_driver()
        before = set(dest_path.parent.glob("*.docx"))
        clicked = driver.execute_script(
            """
            const box = document.querySelector('.ai-correction-box');
            const hasHeader = (box?.querySelector('.result-tip')?.innerText || '')
              .includes('为您生成以下结果');
            if (!hasHeader && box?.querySelector('.top-close-btn')?.offsetParent) {
              return false;
            }
            const target = arguments[0];
            for (const a of document.querySelectorAll(
              '.ai-correction-box .anno-list.ai-correction-annotation a[href],'
              + ' .ai-correction-box .correction-section a[href]'
            )) {
              if (a.href === target) { a.click(); return true; }
            }
            return false;
            """,
            href,
        )
        if not clicked:
            return False, "click_failed"

        click_deadline = time.time() + 120
        last_retry = 0.0
        while time.time() < click_deadline:
            after = set(dest_path.parent.glob("*.docx"))
            new_files = [
                p
                for p in after - before
                if p.name.lower() not in skip and p.name != dest_path.name
            ]
            if dest_path.exists() and dest_path not in before:
                new_files.append(dest_path)
            if new_files:
                newest = max(new_files, key=lambda p: p.stat().st_mtime)
                if newest != dest_path:
                    newest.replace(dest_path)
                if self._is_valid_docx_file(dest_path):
                    self._log(f"  已保存 feedback: {dest_path.name}")
                    return True, ""
                if self._is_oss_error_file(dest_path):
                    dest_path.unlink(missing_ok=True)
                    self._log(
                        "  OSS 返回 NoSuchKey（链接无效，需重新评分生成 feedback）。"
                    )
                    return False, "no_such_key"
                dest_path.unlink(missing_ok=True)
                if time.time() - last_retry > 8:
                    self._log("  浏览器已下载但 docx 尚未就绪，继续等待…")
                    last_retry = time.time()
            time.sleep(2)
        if self._is_valid_docx_file(dest_path):
            return True, ""
        if dest_path.exists() and self._is_oss_error_file(dest_path):
            dest_path.unlink(missing_ok=True)
            return False, "no_such_key"
        return False, "timeout"

    def wait_for_grading_result(self, timeout: int = 600) -> bool:
        """等待右侧栏出现「为您生成以下结果」且含可下载 feedback。"""
        start = time.time()
        grading_since: float | None = None

        while time.time() - start < timeout:
            btn_text = self.get_submit_button_state().get("text", "")
            if btn_text == "评分中":
                if grading_since is None:
                    grading_since = time.time()
                elif time.time() - grading_since > 120:
                    if not self.check_csrf_works():
                        self._log("  「评分中」超过 120 秒且 CSRF 异常。")
                    else:
                        self._log("  「评分中」超过 120 秒，继续等待右侧结果…")
            else:
                grading_since = None

            if self._is_history_panel_open():
                self._close_history_panel()

            panel = self.get_result_panel_state()
            if self._panel_is_downloadable(panel):
                download = self._select_feedback_download(panel)
                if download:
                    ts = download.get("timestamp") or "(无时间戳，判定为新结果)"
                    self._log(
                        f"  右侧已生成结果（评分 {panel.get('score')}），"
                        f"可下载: {download.get('name', '')[:40]} @ {ts}"
                    )
                    return True

            time.sleep(2)

        panel = self.get_result_panel_state()
        self._log(f"  等待结果超时，右侧栏状态: {panel}")
        return False

    def _set_download_dir(self, directory: Path) -> None:
        driver = self._ensure_driver()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            driver.execute_cdp_cmd(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": str(directory.resolve())},
            )
        except Exception:
            pass

    def _download_url_with_cookies(self, url: str, dest_path: Path) -> tuple[bool, str]:
        self._sync_cookies()
        cookie_header = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        req = urllib.request.Request(
            url,
            headers={
                "Cookie": cookie_header,
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            dest_path.write_bytes(data)
            if dest_path.exists() and self._is_oss_error_file(dest_path):
                dest_path.unlink(missing_ok=True)
                self._log("  直链返回 OSS NoSuchKey XML，链接已失效。")
                return False, "no_such_key"
            ok = dest_path.exists() and dest_path.stat().st_size > 0
            return ok, "" if ok else "empty"
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 404}:
                self._log(
                    f"  直链返回 HTTP {exc.code}（OSS 链接无效，需重新评分）。"
                )
                return False, "no_such_key"
            self._log(f"  直链下载失败: HTTP Error {exc.code}: {exc.reason}")
            return False, "http_error"
        except Exception as exc:
            self._log(f"  直链下载失败: {exc}")
            return False, "failed"

    def download_feedback_docx(self, dest_path: Path) -> bool:
        """点击右侧栏「智能批注」下的下载链接，保存 feedback docx。"""
        driver = self._ensure_driver()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_current_result_panel()
        panel = self.get_result_panel_state()
        download = self._select_feedback_download(panel)
        if not download or not download.get("href"):
            self._log(f"  未找到可下载 feedback，右侧栏: {panel}")
            return False

        href = download["href"]
        self._log(f"  正在下载 feedback: {download.get('name', '')[:50]}")

        dl_ok, _ = self._download_url_with_cookies(href, dest_path)
        if dl_ok and self._is_valid_docx_file(dest_path):
            self._log(f"  已保存 feedback: {dest_path.name}")
            return True

        self._set_download_dir(dest_path.parent)
        before = set(dest_path.parent.glob("*.docx"))
        clicked = driver.execute_script(
            """
            const box = document.querySelector('.ai-correction-box');
            const hasHeader = (box?.querySelector('.result-tip')?.innerText || '')
              .includes('为您生成以下结果');
            if (!hasHeader && box?.querySelector('.top-close-btn')?.offsetParent) {
              return false;
            }
            const target = arguments[0];
            for (const a of document.querySelectorAll(
              '.ai-correction-box .anno-list.ai-correction-annotation a[href],'
              + ' .ai-correction-box .correction-section a[href]'
            )) {
              if (a.href === target) { a.click(); return true; }
            }
            return false;
            """,
            href,
        )
        if not clicked:
            self._log("  未能点击右侧「本次结果」下载按钮（可能仍在历史批改列表）。")
            return False

        deadline = time.time() + 120
        while time.time() < deadline:
            after = set(dest_path.parent.glob("*.docx"))
            new_files = [p for p in after - before if p.name != dest_path.name]
            if new_files:
                newest = max(new_files, key=lambda p: p.stat().st_mtime)
                if newest != dest_path:
                    newest.replace(dest_path)
                return dest_path.exists() and dest_path.stat().st_size > 0
            time.sleep(2)
        return dest_path.exists() and dest_path.stat().st_size > 0

    def get_history_texts(self) -> list[str]:
        driver = self._ensure_driver()
        script = """
        const texts = [];
        for (const node of document.querySelectorAll('*')) {
          const txt = (node.innerText || '').trim();
          if (!txt || txt.length > 200) continue;
          if (/\\.docx|\\.pdf|Essay|essay|作业|feedback/i.test(txt)) texts.push(txt);
        }
        return texts.slice(0, 200);
        """
        return driver.execute_script(script) or []

    def close(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None
