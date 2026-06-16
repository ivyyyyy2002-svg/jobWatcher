# jobwatch 部署教程（GitHub Actions，全程免费）

配完之后：你的电脑可以关机，GitHub 的服务器每 30 分钟自动跑一次，
有符合条件的加拿大 intern / new grad 岗位就推到你的 Discord 频道，
并显示发布时间和"多久前发布"，最新的排最前面。

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

## 常见调整

**改频率**：打开 `.github/workflows/jobwatch.yml`，改 `cron` 那行。
- `*/30 * * * *` = 每 30 分钟（默认）
- `*/15 * * * *` = 每 15 分钟
注意：数据源本身大多每小时才更新，跑太勤多半是空转，15～30 分钟最划算。
另外 GitHub 免费版的定时任务在高峰期可能延迟几分钟，属正常。

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