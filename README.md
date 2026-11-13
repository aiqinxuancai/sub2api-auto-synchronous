<div align="center">

<img src="relaydesk/static/mark.svg" width="88" alt="Flow logo">

# Flow · Sub2API 同步工作台

**一个 Web 面板：把上游的健康渠道自动同步到你自己的 Sub2API，并管理员工账号和每月余额。**

[![Release](https://img.shields.io/github/v/release/aiqinxuancai/sub2api-auto-synchronous?include_prereleases&style=flat-square&color=204f40)](https://github.com/aiqinxuancai/sub2api-auto-synchronous/releases)
[![Build](https://img.shields.io/github/actions/workflow/status/aiqinxuancai/sub2api-auto-synchronous/release.yml?style=flat-square&label=build)](https://github.com/aiqinxuancai/sub2api-auto-synchronous/actions/workflows/release.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-amd64%20%7C%20arm64-2496ed?style=flat-square&logo=docker&logoColor=white)

[快速开始](#-快速开始) · [功能特性](#-功能特性) · [同步规则](#-同步规则) · [数据与备份](#-数据与备份) · [常见问题](#-常见问题)

</div>

---

## ✨ 功能特性

| | |
| --- | --- |
| 🔄 **自动同步分组** | 按 providers 排名定时同步健康分组、倍率、模型和转发账号，范围变化时自动停用或恢复 |
| 💰 **优选分组 · 最低价** | 为任意平台添加，对排名前 N 名按价格、评分、人气、缓存、延迟、成功率综合评分择优，排名下降或失效时自动切换，员工 Key 无需重绑 |
| 🏆 **优选分组 · 最优源站** | 为任意平台添加，跟随 providers 默认排序第 1 名，不跌出前 N 名就保持不换，失效时立即切换 |
| 👥 **员工批量管理** | 按名单或序号一次创建最多 200 个账号，自动生成密码并导出 CSV |
| 📅 **每月余额重置** | 每月 1 日按时区自动重置余额，防重复、防误覆盖已消费额度；重置前记录每人余额、用量与主要模型 |
| 🔐 **安全存储** | 密码与密钥加密保存，原子写入，页面不回显敏感信息 |
| 🪶 **轻量部署** | 单个 Docker 容器，支持 amd64 / arm64；前端无需 Node，不依赖外部 CDN |

> [!NOTE]
> 所有设置都在 Web 页面完成并保存到本地 `config.json`，**不需要** `.env` 或任何环境变量。

---

## 🚀 快速开始

### 方式一：使用 GHCR 镜像（推荐）

镜像地址：`ghcr.io/aiqinxuancai/sub2api-auto-synchronous`（支持 linux/amd64、linux/arm64）

1. 新建一个空目录，在其中创建 `docker-compose.yml`，内容如下：

   ```yaml
   name: sub2api-flow

   services:
     flow:
       image: ghcr.io/aiqinxuancai/sub2api-auto-synchronous:latest
       ports:
         - "127.0.0.1:8787:8787"   # 局域网访问改为 "8787:8787"
       volumes:
         - ./data/web:/app/data     # 所有配置与数据，升级时请保留
       restart: unless-stopped
       init: true
       stop_grace_period: 2m
       logging:
         driver: json-file
         options:
           max-size: "10m"
           max-file: "3"
   ```

2. 在该目录执行：

   ```sh
   docker compose up -d
   ```

3. 浏览器打开 **http://127.0.0.1:8787**。

无需下载源码，也不需要 `.env`。如需固定版本，把 `latest` 换成具体版本号，例如 `0.4.0`。

<details>
<summary>常用运维命令</summary>

```sh
docker compose ps                      # 查看状态
docker compose logs -f                 # 查看日志
docker compose pull && docker compose up -d   # 升级到最新镜像
docker compose down                    # 停止
```

如果 GHCR 包是私有的，请先执行 `docker login ghcr.io`。

</details>

> [!TIP]
> 每个 [Release](https://github.com/aiqinxuancai/sub2api-auto-synchronous/releases) 也附带一份固定了镜像摘要的 `docker-compose.yml` 和 `DEPLOY.md`，适合需要严格锁定版本的环境。

### 方式二：从源码构建

```sh
git clone https://github.com/aiqinxuancai/sub2api-auto-synchronous.git
cd sub2api-auto-synchronous
docker compose up -d --build
```

### 方式三：Windows / Python 直接运行

需要 Python 3.10 及以上版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\manage_web.ps1 -Action Start    # 后台启动
.\manage_web.ps1 -Action Status   # 查看状态
.\manage_web.ps1 -Action Stop     # 停止

# 或者前台运行
.\.venv\Scripts\python.exe -X utf8 web_app.py
```

<details>
<summary>启动参数</summary>

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址，局域网访问改为 `0.0.0.0` |
| `--port` | `8787` | 监听端口 |
| `--data-dir` | `data/web` | 数据目录，例如 `D:\flow-data` |
| `--no-scheduler` | — | 只启动页面，不运行定时任务 |

进程日志位于 `data/web.log`，业务日志在网页中查看。`manage_web.ps1` 会自动停止旧版 CLI 后台任务。定时任务需要服务持续运行。

</details>

---

## 🧭 首次使用

1. **设置工作台密码** —— 首次打开页面时设置。
2. **填写连接配置** —— 在「连接配置」中填写上游账号和本地 Sub2API 的管理员密钥。
   > 管理员密钥位于本地 Sub2API 的 **系统设置 → 安全与认证 → 管理员访问密钥**，需要具备分组、转发账号和员工管理权限。
3. **测试连接** —— 确认上游和本地都能正常连通。
4. **开启功能** —— 新安装默认**关闭**自动同步、没有优选分组，测试通过后按需开启或添加。

> [!TIP]
> Docker 默认只允许本机访问。需要局域网访问时，把 `docker-compose.yml`（源码构建为 `compose.yaml`）中的 `127.0.0.1:8787:8787` 改为 `8787:8787`。端口和数据目录都直接在 yml 中修改。

---

## 📐 同步规则

### 普通分组同步

- **频率**：默认每 30 分钟一次。
- **平台与数量**：默认只同步 OpenAI；每个勾选的平台单独设置同步排名前 N 个分组（0 表示不限）。
- **倍率**：优先使用上游给你的专属倍率（包括 0），否则使用分组倍率，再乘以你设置的系数。
- **模型**：只同步上游声明且健康的模型。
- **限流**：每轮最多新增 50 个 Key，剩余分组在后续轮次继续处理。
- **模型价格**：勾选的平台会按上游大厅的模型单价计费（见下文「模型价格同步」），默认 DeepSeek、智谱、Kimi。
- **大厅指标**：本地分组备注的最后一行写入上游大厅近 1 小时的指标，例如 `供应商大厅 1h：延迟 3.2s · 缓存 81% · TPS 38 · 成功率 99%`，没有数据的项显示「—」。优选分组同样会写入，并且原地更新、不中断路由。指标每轮同步都会刷新，只有指标变化时，日志显示「分组指标已刷新」，以便和真正的配置变更区分。

<details>
<summary>排序与安全细节</summary>

- 采用 providers 的 1h 默认排序（自动故障转移、健康度、综合评分、ID），不使用个人收藏或自定义权重。
- 先按排名截取，再检查访问权限和健康有效期；被过滤的分组**不会**由第 N+1 名补位。
- 排名靠前的分组优先处理，并同步更新本地排序。
- 分组超出范围时停用，重新符合条件时恢复；只管理带来源标记的资源，Key 会持续保留复用。
- 探测过期、快照不完整或来源标记重复时，会停止相应的写入以保护数据。
- 大厅返回 `stale: true` 时，等待 2 秒后重新读取一次；重试通过校验则继续本轮任务，仍过期或校验失败则保留现有配置并报错。自动同步和优选分组检测共用此重试。
- 上游套餐、用户余额及内部路由不在同步范围内；基准倍率不代表所有特殊计费规则。

</details>

### 模型价格同步

本地 Sub2API 默认按内置的官方**美元**价表计费，而 AIHub 对 DeepSeek、智谱、Kimi 直接使用官方**人民币**价目的数字（例如 DeepSeek Flash 输入 ¥1 → $1），比官方美元价高约 5–13 倍。倍率相同时，本地扣费会远低于上游实际成本。

在「同步规则 → 模型价格」中勾选的平台，同步分组时会把上游大厅里每个模型的价格写入本地分组的「模型价格」（Sub2API 分组定价，优先级高于渠道与内置价表），本地按与上游相同的单价计费，倍率照常生效：

- 价格相同的模型合并为一张价卡；上游未提供完整输入 / 输出价的模型保留本地内置价。
- 价格未变化时不会重复写入；只改价格时优选分组原地更新，不中断路由。
- 分组定价不会再叠加 DeepSeek 高峰翻倍，与 AIHub 口径一致。
- 取消勾选后，工具写入的价格会在下次同步时清除；从未由工具定价的分组（例如手动设置过价格的分组）不受影响。
- 默认勾选 DeepSeek、智谱、Kimi；OpenAI、Gemini、Grok 在 AIHub 上即为官方美元价，一般无需勾选。

> [!NOTE]
> Sub2API 内置价表中找不到的模型会按 **0 费用**记录（日志 `pricing_missing_record_zero_cost`）。开启价格同步后，这类模型只要上游提供了价格就会正常计费。

### 优选分组

在「连接配置 → 优选分组」中选择**平台**和**策略**后添加。每个优选分组是一个**固定 ID** 的本地分组，渠道切换时员工 Key 无需重新绑定：

- 支持任意平台（OpenAI、DeepSeek、智谱、Kimi、Anthropic、Grok、Gemini），每个平台的同一策略只能添加一次。
- 各分组独立启用、独立排程，各自拥有本地分组和转发账号（本地分组名称需不同）；选中同一上游渠道时复用同一个上游 Key。
- 需开启「同步转发账号」；与「同步哪些平台」的勾选和数量无关，候选池也不受普通数量限制影响。
- 删除后，对应本地分组会在下次检测时停用，上游 Key 继续保留；重新添加时复用原分组与 Key。
- 旧版本的「最低价 OpenAI」「最优源站 OpenAI」设置会在升级后自动变成两个优选分组，沿用原有本地分组、转发账号和 Key。

#### 最低价（综合评分择优）

对该平台默认排名**前 N 名**做**以价格为主的综合评分**，始终指向得分最高的可用渠道：

- **候选**：前 N 名（默认 10，可设 1–100）中可访问、探测有效、有健康模型的渠道（默认还要求模型检测通过）；名称命中**排除关键字**的渠道不参与评分。
- **评分**：每项指标先在候选中归一化为 0–1，再按权重合成 0–100 分；缺少数据的项按 0.5 计算。权重可在 Web 中调整，概览页会显示每个候选的得分构成。

  | 指标 | 默认权重 | 计算方式 |
  | --- | --- | --- |
  | 价格倍率 | 40 | 以候选中最便宜者为满分，按价格比例递减 |
  | 用户评分 | 15 | 大厅星级 ÷ 5 |
  | 评价数 / 人气 | 15 | 近 1 小时使用占比，按对数与候选中最高者比较 |
  | 缓存命中率 | 10 | 50% 为 0 分，100% 为满分 |
  | 首字延迟 | 10 | 以候选中最快者为满分 |
  | 成功率 | 10 | 近 1 小时，80% 为 0 分，100% 为满分 |

- **评分检测**（默认每 10 分钟）：重新评分；当前渠道跌出前「1 + 允许下降名数」名（默认允许下降 2 名，即跌出前 3 名）、或离开前 N 名时，更换为第 1 名。
- **可用性检测**（默认每 5 分钟）：只看当前渠道是否仍可用；分组关闭、无法访问、模型不健康或命中排除关键字时**立即更换**。
- 没有合格候选时停用；切换时先停止路由，再更新凭据、倍率和模型，**员工 Key 无需重新绑定**。

| 设置项 | 默认值 | 可调范围 |
| --- | --- | --- |
| 候选范围 | 前 10 名 | 前 1 – 100 名 |
| 评分检测间隔 | 10 分钟 | 1 – 1440 分钟 |
| 允许排名下降 | 2 名 | 0 – 50 名（0 表示始终使用第 1 名） |
| 可用性检测间隔 | 5 分钟 | 1 – 1440 分钟 |
| 失败重试间隔 | 1 分钟 | 1 – 1440 分钟 |
| 评分权重 | 见上表 | 每项 0 – 100，至少一项大于 0；价格设为 100、其余为 0 即等同于纯最低价 |


<details>
<summary>调度说明</summary>

以上设置保存后立即重新排程。连接失败或等待 Key 时使用重试间隔，成功后恢复正常间隔。调度器每 5 秒检查一次计划，远端任务串行执行，繁忙时会等待当前任务完成。

</details>

#### 最优源站（跟随默认排序）

直接跟随该平台 providers 大厅的**默认排序第 1 名**，追求稳定、少切换：

- 首次启用或需要更换时，选用默认排序中**第一个可用**的渠道（第 1 名不可访问或不健康时依次往后）。
- 每次检测（默认每 10 分钟）只要当前渠道仍在默认排序**前 N 名**（默认 10）且可用，就**保持不换**，即使它已不是第 1 名。
- 当前渠道跌出前 N 名，或关闭、无法访问、不健康时，更换为当时默认排序第一个可用的渠道；前 N 名都不可用时停用。

| 设置项 | 默认值 | 可调范围 |
| --- | --- | --- |
| 检测间隔 | 10 分钟 | 1 – 1440 分钟（同时检查排名与可用性） |
| 保持范围 | 前 10 名 | 前 1 – 100 名 |
| 失败重试间隔 | 1 分钟 | 1 – 1440 分钟 |

---

## 👥 员工与每月余额

**批量创建员工**

- 支持按名单或按序号批量创建，每批最多 200 人。
- 已存在的邮箱自动跳过；不填密码则为每人生成独立随机密码。
- 可设置初始余额、并发数和允许使用的分组，并下载凭据 CSV。

**每月自动重置余额**

- 每月 1 日 00:00（按配置时区，默认 `Asia/Shanghai`）将成员余额设为固定金额（可为 0）。适用范围三选一：左侧选择的成员、所有成员（含以后新增）、所有成员（含以后新增，不含管理员）。
- 首次开启从下个月开始生效；服务停机后恢复，只补执行当月。
- 已成功的记录不会重复执行；网络超时、5xx 或重启导致结果未确认时标记为「待核对」，避免覆盖员工已消费的余额。
- 支持手动立即重置（需在页面二次确认）。确认窗口会列出每位成员的当前余额。

**重置用量总结**

每次重置（自动或手动）在改余额前，会逐一记录每位成员的：

- **重置前余额**：任务开始时读取的剩余额度。
- **本期用量**：实际扣费、请求数、Tokens（含输入/输出/缓存明细）。
- **主要模型**：按扣费排序的模型和占比，每人保留前 20 个，其余合并为「其他」。

统计区间按配置时区的自然日计算：从这位成员上次成功重置那天到重置当天；没有重置记录时，每月任务统计上个月，手动重置统计本月（在 1 日手动重置则统计上个月）；最长回溯 92 天。

在「成员与额度」页的「重置用量总结」或任务详情中点击「查看总结」，可以看到团队合计、主要模型分布和成员明细，支持按成员或模型搜索、导出 CSV。

用量来自本地站点的 `/admin/dashboard/models` 接口（按成员筛选），每位成员多一次只读请求。读取失败不会影响重置：站点不支持该接口时只记录重置前余额，并在日志中提示一次。

---

## 💾 数据与备份

所有数据都在 `data/web` 目录中（Docker 下挂载到容器内的 `/app/data`）：

| 路径 | 内容 |
| --- | --- |
| `config.json` | 连接信息、同步规则、优选分组、每月余额规则 |
| `flow.key` | 加密配置凭据和导出密码的密钥 |
| `flow.sqlite3` | 工作台密码哈希、登录、任务、日志、缓存、额度执行记录、重置用量总结 |
| `profiles/` | 同步映射、上游 Key ID、会话续期信息 |

> [!WARNING]
> **备份时请复制整个 `data/web` 目录。** `config.json` 与 `flow.key` 必须配套保存，否则加密的凭据将无法解密。

- 密码和密钥以 `{"encrypted": "..."}` 形式加密存储，页面中留空表示保持原值。
- 保存采用原子写入，失败时保留旧文件。
- 如需手动修改普通字段：先停止服务 → 修改 → 重启。凭据请始终通过 Web 修改。

### 升级

- **Docker（GHCR 镜像）**：在部署目录执行 `docker compose pull && docker compose up -d`，保留 `data/web` 目录即可；固定版本的用户修改 `image` 中的版本号后再执行。
- **从旧 Web 版升级**：自动迁移旧 SQLite 配置，保留原工作台密码、任务和 Key 映射；迁移完成后以配置文件为准，不再读取 `.env`。
- **从旧 CLI 版升级**：请在 Web 中重新保存站点信息，并保留原数据备份。

---

## ❓ 常见问题

<details>
<summary><b>为什么同步没有自动运行？</b></summary>

新安装默认关闭自动同步、没有优选分组。请先在「连接配置」测试连接，再手动开启或添加。另外请确认服务在持续运行。

</details>

<details>
<summary><b>任务执行很久，怎么知道它在做什么？</b></summary>

点击页面顶部的「查看任务」，或在运行日志中点开任务，可以看到：

- 当前步骤，例如「同步分组「S040-Plus-3」#517」，以及处理进度 `12 / 30`。
- 正在等待哪个接口响应、已经等了多久，例如 `等待响应 · 本地 PUT /admin/groups/470 · 已等待 6 秒`。
- 本任务自己的执行日志，随执行实时追加。

已完成的任务同样可以查看完整执行日志和总用时。

</details>

<details>
<summary><b>可以同时运行多个实例吗？</b></summary>

不建议。请不要同时运行多个 Web 调度进程，否则可能产生重复写入。如需额外实例，可用 `--no-scheduler` 启动，它不会运行定时任务。

</details>

<details>
<summary><b>拉取镜像时提示无权限？</b></summary>

GHCR 包为私有时需要先执行 `docker login ghcr.io`。若希望匿名拉取，仓库所有者需在 GitHub Packages 中将包设为 Public。

</details>

<details>
<summary><b>局域网其他设备访问不了？</b></summary>

Docker 部署请把端口映射 `127.0.0.1:8787:8787` 改为 `8787:8787`；Python 运行请加 `--host 0.0.0.0`。

</details>

---

## 🛠️ 开发者

<details>
<summary><b>命令行工具</b></summary>

CLI 读取与 Web 相同的数据文件，**默认只预览不写入**：

```sh
python sync_sub2api.py --data-dir data/web --once --dry-run     # 预览
python sync_sub2api.py --data-dir data/web --once --apply       # 实际执行
python sync_sub2api.py --data-dir data/web --inspect-upstream   # 只读取上游
```

其他参数：`--daemon` 按配置间隔持续运行，`--group-id <ID>` 只处理指定上游分组（可重复），`--report <路径>` 指定报告文件。

</details>

<details>
<summary><b>运行测试</b></summary>

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 tests/browser_smoke.py
```

测试使用隔离的模拟站点，覆盖配置文件、迁移、加密、原子保存、周期重排、筛选排序、最低价切换、Key 复用、员工与额度操作、认证和响应式 UI。浏览器测试需要本机安装 Chrome，截图输出到 `.cache/browser/`。

</details>

<details>
<summary><b>发布流程（GitHub Actions / GHCR）</b></summary>

1. 提交并推送要发布的代码（包含最新工作流）。
2. 创建并推送版本 tag，例如：

   ```sh
   git tag v0.4.0
   git push origin v0.4.0
   ```

`.github/workflows/release.yml` 会依次：

- 从 tag 自动写入构建用的 Python 包版本和 Web 版本，无需手动修改 `pyproject.toml`；
- 在 Python 3.10 / 3.12 / 3.14 上安装依赖并运行测试；
- 构建容器，验证首次设置和重启后配置保留；
- 推送 linux/amd64 + linux/arm64 镜像到 `ghcr.io/<owner>/<repository>`（自动转小写）；
- 构建 Python 包和源码 ZIP，发布 GitHub Release，附带部署 yml、`DEPLOY.md`、镜像摘要和 `SHA256SUMS`。

**镜像标签**：正式版发布 `v0.4.0`、`0.4.0`、`0.4` 和 `latest`；预发布版本（如 `v0.4.0-rc.1`）不会更新 `latest`。Python 包、Web 页脚和源码 ZIP 中的版本会与 tag 一致。同一 tag 可重新运行并覆盖附件。

**修复失败发布**：已运行的 tag 固定在原提交上，仅点击 Re-run 不会使用之后提交的修复。将修复推送到仓库后，从新提交创建下一个版本 tag（例如已有 `v0.0.1`，则推送 `v0.0.2`）。

工作流使用内置 `GITHUB_TOKEN`（已声明 `contents: write` 与 `packages: write`），无需额外 PAT，但仓库或组织需允许 Actions 发布包。参考：[GitHub 官方 GHCR 发布文档](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)。

</details>
