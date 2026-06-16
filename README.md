# jobwatch 部署教程（GitHub Actions，全程免费）

配完之后：你的电脑可以关机，GitHub 的服务器自动跑：

- **平时（每 30 分钟）**：检查最近 3 小时内发布的岗位。对有精确发布时间的来源
  （公司官网 ATS、社区聚合源），做到增量提醒——只推最近窗口内
  新发布的；没有新岗位也会发一条简短状态提示。对只提供日期、不能精确到分钟
  的来源，会在平时提醒里跳过，避免把很多小时前的岗位当成刚发布。
- **每天午夜（约凌晨 0 点，东部时间）**：发一份"当日汇总"，
  把过去 24 小时发布的岗位归总一次，方便回顾。

消息里带发布时间和"多久前发布"，最新的排最前面，每条之间有空行，链接不展开成大卡片。

当前筛选规则：
- 地点限制在加拿大；Remote 也可以，但必须能看出是 Canada / 加拿大范围。
- Intern / co-op 只要 Fall 2026 / September-December / 4-month 这类 9-12 月岗位。
  明确写 6、8、12、16 个月或一年制的实习会跳过。
- New grad / entry-level / junior 这类岗位，只要是 2026 或 9 月之后可开始即可。
- 明确要求 Canadian citizenship 的岗位会跳过。
- 硬性要求 French / bilingual French 的岗位会跳过。
- 明确硬性限制明显不相关专业（如 accounting、nursing、mechanical/civil/chemical engineering 等）的岗位会跳过。

> 关于精度：能精确到分钟的来源（ATS / 社区源）就按"最近 N 分钟"窗口推，
> 尽量做到一发布就提醒；LinkedIn 如果只给日期不给时间，没法证明它属于最近
> 3 小时，所以平时提醒会跳过这类不精确时间，避免旧岗位刷屏。
> 第一次跑会建立基线（推一小批最近的），之后每次只推真正的新增。

---

## 你会用到的 3 个文件
- `jobwatch.py`        ——主脚本
- `jobwatch.yml`       ——GitHub Actions 定时任务配置
- `requirements.txt`   ——依赖清单

---

## 第 1 步：建一个 Discord Webhook（约 2 分钟）

Webhook 就是一个"往某个频道发消息"的专属网址，不用建机器人，复制一个 URL 就行。

1. 打开 Discord。如果你还没有自己的服务器，左边点 `+` → `创建我的`
   → 随便建一个（只给自己用就行）。
2. 在你想接收岗位的那个**文字频道**上，鼠标悬停 → 点齿轮图标"编辑频道"
   （或右键频道 → `编辑频道`）。
3. 左侧选 `整合 / Integrations` → `Webhook` → `新 Webhook`。
4. 它会自动建好一个。点 `复制 Webhook URL`。
   —— 这串 URL 形如 `https://discord.com/api/webhooks/123.../abc...`，
   先复制存好，这就是你唯一需要的密钥。

记下这一样东西：**DISCORD_WEBHOOK**。

⚠️ 这个 URL 谁拿到都能往你频道发消息，别公开贴出来。所以下面用 GitHub
Secrets 存它，而不是写进代码。

---

## 第 2 步：建 GitHub 仓库

1. 登录 github.com，右上角 `+` → `New repository`。
2. 名字随便（比如 `jobwatch`），选 **Private**（私有，别人看不到）。
3. 勾选 "Add a README file"，点 `Create repository`。

---

## 第 3 步：上传文件

在仓库页面：

1. 点 `Add file` → `Upload files`，把 `jobwatch.py` 和 `requirements.txt`
   拖进去，`Commit changes`。
2. 现在要放 workflow 文件，它必须在固定目录 `.github/workflows/` 下：
   - 点 `Add file` → `Create new file`。
   - 文件名一栏输入：`.github/workflows/jobwatch.yml`
     （直接打这一串，GitHub 会自动帮你建好文件夹）
   - 把 `jobwatch.yml` 的内容整段粘贴进去。
   - `Commit changes`。

完成后你的仓库结构应该是：
```
jobwatch.py
requirements.txt
.github/workflows/jobwatch.yml
```

---

## 第 4 步：填入密钥（Discord webhook URL）

不要把 webhook URL 直接写进代码。用 GitHub 的 Secrets：

1. 仓库页 → `Settings` → 左侧 `Secrets and variables` → `Actions`。
2. 点 `New repository secret`：
   - Name: `DISCORD_WEBHOOK`
   - Secret: 你第 1 步复制的那串 webhook URL
   - `Add secret`

就这一个，完成。

---

## 第 5 步：先手动跑一次（重要！）

第一次跑会把当前所有匹配岗位当成"新"推给你，可能几十条。先手动触发一次，
把"基线"建立起来；之后每次就只推真正新增的。

1. 仓库页 → 顶部 `Actions` 标签。
2. 如果看到提示 "Workflows aren't being run on this repository"，
   点绿色按钮 `I understand my workflows, go ahead and enable them`。
3. 左侧点 `jobwatch` 这个 workflow → 右边 `Run workflow` → 再点 `Run workflow`。
4. 等 1～2 分钟，刷新页面，点进那次运行看日志。
   - 看到 `Fetched N jobs, M new` 就说明成功。
   - 同时你的 Discord 频道应该收到一大批岗位（可能分成几条消息发，正常）。

跑完后，仓库里会自动多出一个 `seen_jobs.db` 文件——这是"已推送记录"，
脚本靠它去重。不要手动删它。

---

## 第 6 步：确认自动运行

做完上面，就已经在自动跑了。`jobwatch.yml` 里设的是每 30 分钟一次。
你什么都不用做，电脑也可以关。

想验证：等半小时看 Actions 里有没有新的自动运行记录，
或者随时回到 Actions 手动 `Run workflow` 测试。

---

## 备用方案：用 cron-job.org 触发 GitHub workflow

如果 GitHub 自带的 `schedule` 不稳定，可以让外部定时器每 30 分钟调用
GitHub API。效果等同于自动帮你点 `Run workflow`，仍然是 GitHub Actions
在跑 `jobwatch.py`。

### 1. 先创建 GitHub token

1. GitHub 右上角头像 → `Settings`。
2. 左侧 `Developer settings` → `Personal access tokens` → `Fine-grained tokens`。
3. 点 `Generate new token`。
4. Repository access 选 `Only select repositories`，只选你的 `jobWatcher` 仓库。
5. Repository permissions 里把 `Actions` 设成 `Read and write`。
6. 生成后复制 token。只复制这一次，别贴进代码、README、Discord 或公开地方。

### 2. 在 cron-job.org 新建定时任务

1. 打开 `https://cron-job.org`，注册并登录。
2. 点 `Create cronjob`。
3. Schedule 选每 30 分钟一次。
4. URL 填：

```text
https://api.github.com/repos/ivyyyyy2002-svg/jobWatcher/actions/workflows/jobwatch.yml/dispatches
```

5. Request method 选 `POST`。
6. Request body / Body 填：

```json
{"ref":"main","inputs":{"mode":"alert"}}
```

7. Headers 添加这几项：

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_GITHUB_TOKEN
X-GitHub-Api-Version: 2026-03-10
Content-Type: application/json
```

把 `YOUR_GITHUB_TOKEN` 换成你刚刚复制的 token。

### 3. 保存后测试

保存 cronjob 后点一次手动执行/测试。成功时 GitHub Actions 页面会出现一条新的
`workflow_dispatch` 运行记录。以后 cron-job.org 每 30 分钟触发一次，即使
GitHub 自带 `schedule` 没触发，也能继续自动检查岗位。

---

## 常见调整

**改"时间窗口"**（平时只推最近多少分钟内发布的）：打开 `jobwatch.py`，
改 `ALERT_WINDOW_MINUTES`（默认 `180`，也就是 3 小时）。去重数据库会兜底防重复，所以即使之后
你把窗口调大一点，也不会重复推送已经发过的岗位。
（注意：此窗口只对有精确时间的来源生效；没有分钟级发布时间的岗位会被平时提醒跳过。）

**改每日汇总时间**：打开 `.github/workflows/jobwatch.yml`，
找 `- cron: "0 4 * * *"` 那行。`4` 是 UTC 小时，对应东部时间凌晨 0 点（夏令时）。
想换成别的时间，把 4 改成 `(你想要的东部小时 + 4) % 24`。
汇总回看多久也能改：`jobwatch.py` 里的 `DIGEST_LOOKBACK_HOURS`（默认 24）。

**改提醒频率**：同一个 yml 文件，改第一条 `- cron: "*/30 * * * *"`。
- `*/30 * * * *` = 每 30 分钟（默认）
- `*/15 * * * *` = 每 15 分钟
注意：数据源本身大多每小时才更新，跑太勤多半是空转，15～30 分钟最划算。
另外 GitHub 免费版的定时任务在高峰期可能延迟几分钟，属正常。

**手动测某个模式**：Actions 页 → `Run workflow`，会有个下拉框让你选
`alert` 还是 `digest`，方便随时测试。

**加要盯的公司**（想抢某家大厂，直连最快）：
打开 `jobwatch.py`，把公司加到对应列表：
- Greenhouse：`GREENHOUSE_COMPANIES`，填招聘页 URL 里的 slug
  （`boards.greenhouse.io/公司名` 的"公司名"）
- Lever：`LEVER_COMPANIES`，同理填 `jobs.lever.co/公司名` 的"公司名"
- Workday：`WORKDAY_COMPANIES`，格式见文件里的注释

**改关键词 / 地点**：脚本顶部 `Config` 区，
`ROLE_RE`（职位类型）、`LOCATION_INCLUDE`（地点白名单，现在是加拿大）都能改。

**临时不想收消息**：Actions 页面右上角可以 `Disable workflow`，想恢复再 `Enable`。

---

## 出问题怎么排查

- Discord 没收到消息：
  - 确认 Secrets 里名字拼写完全是 `DISCORD_WEBHOOK`，值是完整的 webhook URL。
  - 确认那个 webhook 对应的频道没被删、webhook 没被撤销。
  - 去 Actions 看那次运行日志，搜 `[discord]` 看有没有报错。
- 日志里某些来源报错（比如 linkedin 限流）：正常，不影响其它来源，
  脚本不会崩，照样出结果。
- 想看脚本到底抓了啥但不想发 Discord：本地把 `NOTIFY = "discord"`
  改成 `NOTIFY = "print"`，命令行跑 `python3 jobwatch.py` 即可。
