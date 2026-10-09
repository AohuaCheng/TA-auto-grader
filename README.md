# TA-auto-grader

面向**助教/教师**的自动化工具：批量下载学生作业、雨课堂 AI 批改、分数汇总与回传。

基于 [learn2018-autodown](https://github.com/Trinkle23897/learn2018-autodown) 的浏览器登录与助教端 API 改造。

## 目录结构

代码仓库与课程数据分离，推荐布局：

```
Essays/                      # 工作区根目录（非 git 仓库）
├── TA-auto-grader/          # 本仓库（git clone 到此）
│   ├── main.py
│   ├── .env                 # 本地配置（git 忽略）
│   └── ...
├── downloads/               # 学生作业与 feedback（git 忽略）
│   └── Essay1/
└── templates/               # 分数汇总等课程模板（git 忽略）
```

所有命令在 `TA-auto-grader/` 目录下运行；未设置环境变量时，程序默认读写上级目录的 `downloads/` 与 `templates/`。

## 功能

| 模块 | 说明 |
|------|------|
| 网络学堂 | 批量下载提交、查看名单、CSV 批量回传成绩 |
| 雨课堂 | Reflective 规则 AI 批改，自动下载 feedback docx |
| 修复工具 | 批量批改后 feedback 错位时，按正文相似度重新对齐 |

## 安装

推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
git clone <repo-url>
cd TA-auto-grader
uv venv && uv sync
```

需要本机已安装 **Google Chrome**。

## 配置

```bash
cp .env.example .env
cp credentials.local.env.example credentials.local.env
```

**1. 登录凭据** — 编辑 `credentials.local.env`（已被 git 忽略）：

```env
THU_USERNAME=你的学工号
THU_PASSWORD=你的密码
```

**2. 作业与雨课堂** — 编辑 `.env`，填入你从浏览器复制的 URL 参数：

```env
THU_ASSIGNMENT_URL=https://learn.tsinghua.edu.cn/f/wlxt/kczy/xszy/teacher/beforePageList?wlkcid=...&zyid=...
THU_DOWNLOAD_DIR=../downloads
THU_TEMPLATES_DIR=../templates
YUKETANG_WORKSPACE_ID=...
YUKETANG_RULE_ID=...
YUKETANG_FORM_RULE_ID=...
```

**3. 课程模板（可选，仅本地）** — 将分数汇总 xlsx 等放入工作区根目录的 `templates/`（与仓库同级，不在 git 中）。

## 使用

### 登录

```bash
uv run python main.py login
uv run python main.py yuketang-login    # 雨课堂（微信扫码）
```

### 下载学生作业

```bash
uv run python main.py download
```

下载结构：`../downloads/<作业名>/{学号}_{姓名}/` + `submissions.csv`

### 雨课堂 AI 批改

```bash
uv run python main.py grade                              # 全量（跳过已有 feedback）
uv run python main.py grade --regrade-only               # 仅补评
uv run python main.py grade --regrade-only --student ID  # 指定学号
uv run python main.py grade --dry-run --limit 3          # 预览
```

feedback 保存为 `{原稿名}_feedback.docx`。文件名含 `#` 的 docx 上传时会自动复制为 `No.` 版本。

若 feedback 错位：

```bash
uv run python fix_feedback_alignment.py --dry-run
uv run python fix_feedback_alignment.py --dir ../downloads/<作业名>
```

### 回传成绩

```bash
uv run python main.py upload --grades grades.csv
uv run python main.py upload --grades grades.csv --confirm
```

CSV 需含 `xszyid`（来自 `submissions.csv`），可选 `成绩`、`评语`、`附件` 列。

## 安全说明

- 第三方工具均为非官方软件，请仅在自己的账号、自己的课程上使用。
- `credentials.local.env`、`session.json`、`yuketang_session.json` 含敏感信息，已加入 `.gitignore`。
- 请勿将 `.env` 或凭据文件提交到 git 或分享给他人。

## 已知限制

1. 批改上传依赖网页表单解析，平台改版后可能需要调整。
2. 首次上传建议 dry-run 并对 1–2 名学生试传验证。
3. 工具针对单次作业，不做全课程镜像。
