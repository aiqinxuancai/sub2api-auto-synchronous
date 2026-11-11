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
| 💰 **最低价 OpenAI 分组** | 从排名前 10 中自动挑选倍率最低且可用的渠道，失效时自动切换，员工 Key 无需重绑 |
| 👥 **员工批量管理** | 按名单或序号一次创建最多 200 个账号，自动生成密码并导出 CSV |
| 📅 **每月余额重置** | 每月 1 日按时区自动重置余额，防重复、防误覆盖已消费额度 |
| 🔐 **安全存储** | 密码与密钥加密保存，原子写入，页面不回显敏感信息 |
| 🪶 **轻量部署** | 单个 Docker 容器，支持 amd64 / arm64；前端无需 Node，不依赖外部 CDN |

> [!NOTE]
> 所有设置都在 Web 页面完成并保存到本地 `config.json`，**不需要** `.env` 或任何环境变量。

---

## 🚀 快速开始

### 方式一：使用 Release 镜像（推荐）

1. 打开 [Releases](https://github.com/aiqinxuancai/sub2api-auto-synchronous/releases)，下载最新版本附带的 `docker-compose.yml`，放到一个空目录。
2. 在该目录执行：

   ```sh
   docker compose -f docker-compose.yml up -d
   ```

3. 浏览器打开 **http://127.0.0.1:8787**。

无需下载源码。部署文件已固定多架构镜像摘要，完整说明见同一 Release 中的 `DEPLOY.md`。

<details>
<summary>常用运维命令</summary>

```sh
docker compose -f docker-compose.yml ps        # 查看状态
docker compose -f docker-compose.yml logs -f   # 查看日志
docker compose -f docker-compose.yml down      # 停止
```

如果 GHCR 包是私有的，请先执行 `docker login ghcr.io`。

</details>

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
4. **开启功能** —— 新安装默认**关闭**自动同步和最低价分组，测试通过后按需开启。

> [!TIP]
> Docker 默认只允许本机访问。需要局域网访问时，把 `compose.yaml` 中的 `127.0.0.1:8787:8787` 改为 `8787:8787`。端口和数据目录都直接在 yml 中修改。

---

## 📐 同步规则

### 普通分组同步

- **频率**：默认每 30 分钟一次。
- **平台**：默认只同步 OpenAI，可勾选其他平台。
- **数量**：同步排名前 N 个分组（0 表示不限），可选择「每个平台各取 N 个」或「所选平台合计 N 个」。
- **倍率**：优先使用上游给你的专属倍率（包括 0），否则使用分组倍率，再乘以你设置的系数。
- **模型**：只同步上游声明且健康的模型。
- **限流**：每轮最多新增 50 个 Key，剩余分组在后续轮次继续处理。

<details>
<summary>排序与安全细节</summary>

- 采用 providers 的 1h 默认排序（自动故障转移、健康度、综合评分、ID），不使用个人收藏或自定义权重。
- 先按排名截取，再检查访问权限和健康有效期；被过滤的分组**不会**由第 N+1 名补位。
- 排名靠前的分组优先处理，并同步更新本地排序。
- 分组超出范围时停用，重新符合条件时恢复；只管理带来源标记的资源，Key 会持续保留复用。
- 探测过期、快照不完整或来源标记重复时，会停止相应的写入以保护数据。
- 上游套餐、用户余额及内部路由不在同步范围内；基准倍率不代表所有特殊计费规则。

</details>

### 最低价 OpenAI 分组

工作台会维护一个**固定 ID** 的本地分组，始终指向当前最便宜的可用渠道：

- 从 OpenAI 默认排名前 10 中，选出可访问、探测有效、有健康模型、且倍率最低的渠道（默认还要求模型检测通过）。
- 当前渠道仍在前 10 且可用时保持不变；跌出前 10 或检测不可用时重新选择；没有合格候选时停用。
- 切换时先停止路由，再更新凭据、倍率和模型，**员工 Key 无需重新绑定**。

| 设置项 | 默认值 | 可调范围 |
| --- | --- | --- |
| 排名检测间隔 | 1 小时 | 1 – 720 小时 |
| 可用性检测间隔 | 5 分钟 | 1 – 1440 分钟 |
| 失败重试间隔 | 1 分钟 | 1 – 1440 分钟 |

> [!IMPORTANT]
> 使用最低价分组需要选中 OpenAI 平台并开启转发账号同步。它的计划独立于普通同步开关，候选池也不受普通数量限制影响。

<details>
<summary>调度说明</summary>

以上设置保存后立即重新排程。连接失败或等待 Key 时使用重试间隔，成功后恢复正常间隔。调度器每 5 秒检查一次计划，远端任务串行执行，繁忙时会等待当前任务完成。

</details>

---

## 👥 员工与每月余额

**批量创建员工**

- 支持按名单或按序号批量创建，每批最多 200 人。
- 已存在的邮箱自动跳过；不填密码则为每人生成独立随机密码。
- 可设置初始余额、并发数和允许使用的分组，并下载凭据 CSV。

**每月自动重置余额**

- 每月 1 日 00:00（按配置时区，默认 `Asia/Shanghai`）将指定员工或所有员工的余额设为固定金额（可为 0），默认不包括管理员。
- 首次开启从下个月开始生效；服务停机后恢复，只补执行当月。
- 已成功的记录不会重复执行；网络超时、5xx 或重启导致结果未确认时标记为「待核对」，避免覆盖员工已消费的余额。
- 支持手动立即重置（需在页面二次确认）。

---

## 💾 数据与备份

所有数据都在 `data/web` 目录中（Docker 下挂载到容器内的 `/app/data`）：

| 路径 | 内容 |
| --- | --- |
| `config.json` | 连接信息、同步规则、最低价检查周期、每月余额规则 |
| `flow.key` | 加密配置凭据和导出密码的密钥 |
| `flow.sqlite3` | 工作台密码哈希、登录、任务、日志、缓存、额度执行记录 |
| `profiles/` | 同步映射、上游 Key ID、会话续期信息 |

> [!WARNING]
> **备份时请复制整个 `data/web` 目录。** `config.json` 与 `flow.key` 必须配套保存，否则加密的凭据将无法解密。

- 密码和密钥以 `{"encrypted": "..."}` 形式加密存储，页面中留空表示保持原值。
- 保存采用原子写入，失败时保留旧文件。
- 如需手动修改普通字段：先停止服务 → 修改 → 重启。凭据请始终通过 Web 修改。

### 升级

- **Docker**：用新 Release 的 `docker-compose.yml` 替换旧文件后重新 `up -d`，保留 `data/web` 目录即可。
- **从旧 Web 版升级**：自动迁移旧 SQLite 配置，保留原工作台密码、任务和 Key 映射；迁移完成后以配置文件为准，不再读取 `.env`。
- **从旧 CLI 版升级**：请在 Web 中重新保存站点信息，并保留原数据备份。

---

## ❓ 常见问题

<details>
<summary><b>为什么同步没有自动运行？</b></summary>

新安装默认关闭自动同步和最低价分组。请先在「连接配置」测试连接，再手动开启。另外请确认服务在持续运行。

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

1. 将 `pyproject.toml` 中的版本号更新为目标版本并提交。
2. 推送对应 tag：

   ```sh
   git tag v0.4.0
   git push origin v0.4.0
   ```

`.github/workflows/release.yml` 会依次：

- 在 Python 3.10 / 3.12 / 3.14 上运行测试并校验版本一致；
- 构建容器，验证首次设置和重启后配置保留；
- 推送 linux/amd64 + linux/arm64 镜像到 `ghcr.io/<owner>/<repository>`（自动转小写）；
- 构建 Python 包和源码 ZIP，发布 GitHub Release，附带部署 yml、`DEPLOY.md`、镜像摘要和 `SHA256SUMS`。

**镜像标签**：正式版发布 `v0.4.0`、`0.4.0`、`0.4` 和 `latest`；预发布版本（如 `v0.4.0-rc.1`，项目版本填 `0.4.0-rc.1`）不会更新 `latest`。同一 tag 可重新运行并覆盖附件。

工作流使用内置 `GITHUB_TOKEN`（已声明 `contents: write` 与 `packages: write`），无需额外 PAT，但仓库或组织需允许 Actions 发布包。参考：[GitHub 官方 GHCR 发布文档](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)。

</details>
