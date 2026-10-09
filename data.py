LEARN_BASE_URL = "https://learn.tsinghua.edu.cn"
SSO_LOGIN_URL = (
    "https://id.tsinghua.edu.cn/do/off/ui/auth/login/form/"
    "bb5df85216504820be7bba2b0ae1535b/0"
)

SUCCESS_INDICATORS = [
    "learn.tsinghua.edu.cn",
    "myCourse",
    "semesterCourseList",
    "退出登录",
    "注销",
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": f"{LEARN_BASE_URL}/",
}

TEST_URLS = [
    f"{LEARN_BASE_URL}/f/wlxt/index/course/student/",
    f"{LEARN_BASE_URL}/b/kc/zhjw_v_code_xnxq/getCurrentAndNextSemester",
]
