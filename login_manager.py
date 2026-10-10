"""Browser-based SSO login for Tsinghua Web Learning.

Adapted from learn2018-autodown (MIT):
https://github.com/Trinkle23897/learn2018-autodown
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from data import DEFAULT_HEADERS, LEARN_BASE_URL, SSO_LOGIN_URL


class BrowserLoginManager:
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        headless: bool = False,
    ):
        self.username = username
        self.password = password
        self.headless = headless
        self.driver = None
        self.session: requests.Session | None = None
        self.cookies: dict[str, str] = {}

    def _ensure_driver(self) -> None:
        if self.driver is not None:
            return
        self._init_driver()

    def _init_driver(self) -> None:
        options = ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"--user-agent={DEFAULT_HEADERS['User-Agent']}")
        try:
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(60)
            self.driver.implicitly_wait(10)
        except WebDriverException as exc:
            raise RuntimeError(
                "无法启动 Chrome。请确认已安装 Chrome 与对应版本的 ChromeDriver。"
            ) from exc

    def _log(self, message: str) -> None:
        print(message, flush=True)

    def _is_learn_logged_in_url(self, url: str) -> bool:
        lowered = url.lower()
        return "learn.tsinghua.edu.cn" in lowered and "login" not in lowered

    def _click_login_button(self, password_input=None) -> bool:
        """Click Tsinghua SSO login control (anchor with doLogin(), not submit button)."""
        selectors = (
            (By.CSS_SELECTOR, "a[onclick*='doLogin']"),
            (By.XPATH, "//a[contains(@onclick, 'doLogin')]"),
            (By.XPATH, "//a[contains(normalize-space(.), '登录')]"),
            (By.ID, "login_button"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
        )
        for selector in selectors:
            try:
                button = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable(selector)
                )
                button.click()
                self._log("已点击登录按钮。")
                return True
            except Exception:
                continue

        try:
            self.driver.execute_script("if (typeof doLogin === 'function') doLogin();")
            self._log("已通过 JavaScript 调用 doLogin()。")
            return True
        except Exception:
            pass

        if password_input is not None:
            try:
                password_input.send_keys(Keys.RETURN)
                self._log("已在密码框发送 Enter 键提交登录。")
                return True
            except Exception:
                pass

        self._log("未能自动点击登录按钮，请在浏览器中手动点击「登录」。")
        return False

    def _fill_login_form(self) -> None:
        username = (self.username or "").strip()
        password = (self.password or "").strip()
        placeholders = {"", "your_student_id", "YOUR_STUDENT_ID", "your_password_here"}

        if username in placeholders:
            username = ""
        if password in placeholders:
            password = ""

        if not username and not password:
            self._log("未配置账号密码，请在浏览器中手动登录。")
            return

        password_input = None
        try:
            if username:
                username_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "i_user"))
                )
                username_input.clear()
                username_input.send_keys(username)
                self._log(f"已自动填入用户名: {username}")

            if password:
                password_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "i_pass"))
                )
                password_input.clear()
                password_input.send_keys(password)
                self._log("已自动填入密码。")

            if username and password:
                time.sleep(0.5)
                self._click_login_button(password_input)
        except TimeoutException:
            self._log("未能自动填写登录表单，请在浏览器中手动完成。")

    def interactive_login(self, timeout: int = 300) -> bool:
        self._ensure_driver()
        self._log(
            "正在打开清华 SSO 登录页；如开启双因素认证，请在浏览器中继续确认..."
        )
        self.driver.get(SSO_LOGIN_URL)
        self._fill_login_form()

        start = time.time()
        last_url = ""
        while time.time() - start < timeout:
            current_url = self.driver.current_url
            if current_url != last_url:
                self._log(f"页面跳转: {current_url}")
                last_url = current_url

            if self._is_learn_logged_in_url(current_url):
                return self._finalize_login()

            time.sleep(2)

        self._log("等待超时，尝试直接打开网络学堂主页...")
        self.driver.get(f"{LEARN_BASE_URL}/f/wlxt/index/course/student/")
        time.sleep(3)
        if self._is_learn_logged_in_url(self.driver.current_url):
            return self._finalize_login()

        self._log("等待登录超时，请确认已在浏览器中完成 SSO 登录。")
        return False

    def _finalize_login(self) -> bool:
        if not self._is_learn_logged_in_url(self.driver.current_url):
            self.driver.get(f"{LEARN_BASE_URL}/f/wlxt/index/course/student/")
            time.sleep(2)

        if not self._build_session_from_browser():
            return False

        if self.verify_session():
            self._log("登录成功，会话验证通过。")
            return True

        self._log("已进入网络学堂，但会话验证失败，可能尚未完成登录。")
        return False

    def _build_session_from_browser(self) -> bool:
        browser_cookies = self.driver.get_cookies()
        self.cookies = {c["name"]: c["value"] for c in browser_cookies}

        session = requests.Session()
        xsrf_token = None
        for cookie in browser_cookies:
            session.cookies.set(
                name=cookie["name"],
                value=cookie["value"],
                domain=cookie.get("domain", ".tsinghua.edu.cn"),
                path=cookie.get("path", "/"),
            )
            if cookie["name"] == "XSRF-TOKEN":
                xsrf_token = cookie["value"]

        headers = dict(DEFAULT_HEADERS)
        if xsrf_token:
            headers["X-XSRF-TOKEN"] = xsrf_token
        session.headers.update(headers)
        self.session = session
        self._log(f"已提取 {len(self.cookies)} 个 cookie。")
        if len(self.cookies) < 3:
            self._log("cookie 数量过少，通常表示尚未真正登录网络学堂。")
            return False
        return True

    def save_session(self, filepath: str | Path = "session.json") -> None:
        if not self.session:
            raise RuntimeError("尚未登录，无法保存 session。")
        payload = {
            "username": self.username,
            "cookies": self.cookies,
            "timestamp": time.time(),
        }
        Path(filepath).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"会话已保存到 {filepath}")

    def load_session(self, filepath: str | Path = "session.json") -> bool:
        path = Path(filepath)
        if not path.exists():
            return False

        data = json.loads(path.read_text(encoding="utf-8"))
        self.username = data.get("username") or self.username
        self.cookies = data.get("cookies") or {}

        session = requests.Session()
        xsrf_token = self.cookies.get("XSRF-TOKEN")
        headers = dict(DEFAULT_HEADERS)
        if xsrf_token:
            headers["X-XSRF-TOKEN"] = xsrf_token
        session.headers.update(headers)

        for domain in (".tsinghua.edu.cn", "learn.tsinghua.edu.cn", "id.tsinghua.edu.cn"):
            for name, value in self.cookies.items():
                if value is not None:
                    session.cookies.set(name=name, value=value, domain=domain, path="/")

        self.session = session
        return True

    def verify_session(self) -> bool:
        if not self.session:
            return False
        resp = self.session.get(f"{LEARN_BASE_URL}/f/wlxt/index/course/student/")
        text = resp.text
        lowered = text.lower()
        if resp.status_code != 200:
            return False
        if any(marker in text for marker in ("登录超时", "您未登录", "登录失效")):
            return False
        if "login" in resp.url.lower():
            return False
        return len(text) > 1000 and (
            "课程" in text or "course" in lowered or "wlxt" in lowered
        )

    def get_session(self) -> requests.Session:
        if not self.session:
            raise RuntimeError("尚未登录。")
        return self.session

    def close(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None
        if self.session:
            self.session.close()
            self.session = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
