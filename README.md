# Flow · Sub2API 同步工作台

通过 Web 配置上游与本地 Sub2API，定时同步健康分组、倍率、模型和转发账号，管理员工账号及每月余额。Python 3.10+，前端无需 Node 或外部 CDN。

所有业务配置通过 Web 保存到本地 `config.json`，不读取 `.env` 或业务环境变量。

## Docker 部署

从源码运行，在仓库目录执行：

```sh
docker compose up -d --build
```

打开 **http://127.0.0.1:8787**，首次设置工作台密码，再在“连接配置”填写上游账号和本地管理员密钥。新安装默认关闭自动同步和最低价分组，测试连接后自行开启。管理员密钥入口为本地 Sub2API 的“系统设置 → 安全与认证 → 管理员访问密钥”，需要分组、转发账号及员工管理权限。

`compose.yaml` 将 `./data/web` 挂载到容器 `/app/data`，提供 `/healthz` 健康检查并配置自动重启。局域网访问时将 `127.0.0.1:8787:8787` 改为 `8787:8787`。端口和挂载路径直接修改 yml，无需环境变量。

每个 GitHub Release 会附带已填入实际 GHCR 镜像地址的 `docker-compose.yml` 和 `DEPLOY.md`，下载到一个目录后执行：

```sh
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f
```

无需源码或 `.env`。部署文件固定多架构镜像摘要，支持 **linux/amd64** 和 **linux/arm64**。GHCR 包为私有时先 `docker login ghcr.io`；允许匿名拉取需在 GitHub Packages 将包设为 Public。升级时在原目录替换新 Release 的部署文件，重新启动并保留整个 `data/web` 目录。

## 本地配置与升级迁移

| 文件或目录 | 内容 |
| --- | --- |
| `data/web/config.json` | 连接、同步规则、最低价检查周期和每月余额规则 |
| `data/web/flow.key` | 配置凭据和导出账号密码的加密密钥 |
| `data/web/flow.sqlite3` | 工作台密码哈希、登录、任务、日志、缓存和额度执行记录 |
| `data/web/profiles/` | 同步映射、上游 Key ID、会话续期信息 |

Web 保存时原子写入配置文件，失败时保留旧文件。密码与密钥使用 `{"encrypted": "..."}` 加密存储，页面留空保持原值，正常 API 不回显。备份时复制整个目录，`config.json` 与 `flow.key` 必须配套保存。手动修改普通字段时先停止服务，修改后重启；凭据通过 Web 修改。

升级现有 Web 版时，自动迁移旧 SQLite 配置，保留原工作台密码、任务和 Key 映射。文件存在后以文件为准，旧 `.env` 不再加载。只有旧 CLI 配置的安装请通过 Web 重新保存站点信息，并保留原数据备份。不要同时运行多个 Web 调度进程。

## GitHub Actions / GHCR 发布

`.github/workflows/release.yml` 由推送 `v*` tag 触发。先将 `pyproject.toml` 版本更新为对应版本，提交代码，再推送 tag：

```sh
git tag v0.4.0
git push origin v0.4.0
```

工作流先在 Python 3.10、3.12、3.14 上运行测试，检查版本一致，再实际构建容器、测试首次设置和重启后的配置保留。通过后推送多架构 GHCR 镜像，构建 Python 包及源码 ZIP，发布 GitHub Release，附带部署 yml、部署说明、镜像摘要和 `SHA256SUMS`。

镜像地址自动使用小写的 `ghcr.io/<owner>/<repository>`。正式版本发布 `v0.4.0`、`0.4.0`、`0.4` 和 `latest` 标签；预发布如 `v0.4.0-rc.1`（项目版本也填写 `0.4.0-rc.1`）不更新 `latest`。相同 tag 的工作流支持重试并覆盖附件。

使用仓库自带的 `GITHUB_TOKEN`，无需额外 PAT；工作流已声明 `contents: write` 和 `packages: write`，仓库或组织仍需允许 Actions 发布包。实现参考 [GitHub 官方 GHCR Actions 文档](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)。

## Windows / Python 运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\manage_web.ps1 -Action Start
.\manage_web.ps1 -Action Status
.\manage_web.ps1 -Action Stop

# 或前台运行
.\.venv\Scripts\python.exe -X utf8 web_app.py
```

入口支持 `--host`、`--port`、`--data-dir` 和 `--no-scheduler`，例如 `web_app.py --host 0.0.0.0 --data-dir D:\flow-data`。`manage_web.ps1` 会停止旧 CLI 后台任务。进程日志在 `data/web.log`，业务日志在网页中。计划执行需要服务持续运行。

## 分组同步与最低价 OpenAI

普通同步默认每 30 分钟，默认只选 OpenAI；可选其他平台、倍率系数及同步前 N 个分组，0 表示不限。支持每个平台分别取 N 个或所选平台合计 N 个。

采用 providers 的 1h 默认排序，包含自动故障转移、健康、综合评分及 ID 比较，不使用个人收藏或自定义权重。先截取再检查访问权限和健康有效期，不用第 N+1 名补位。优先处理排名靠前的分组并更新本地排序。缩小范围时停用，重新符合条件时恢复，只管理来源标记资源，Key 持续保留复用。探测过期、快照不完整或重复来源标记时停止相应写入。

只同步声明且健康的模型，倍率优先使用用户专属值（含 0），否则用分组倍率，本地倍率再乘配置系数。上游套餐、用户余额及内部路由不属于渠道同步范围；基准倍率不代表所有特殊计费规则。每轮默认最多新增 50 个 Key，限额后的分组后续继续处理。

最低价分组从 OpenAI 默认前 10 名选择可访问、探测有效、有健康模型且适用倍率最低的渠道，默认还要求模型检测通过。候选池独立于普通数量限制。当前渠道仍在前 10 且可用时保持；离开前 10 或检测不可用时重新选择，无合格候选则停用。固定本地分组 ID，切换时先停止路由，再更新凭据、倍率和模型，员工 Key 无需重绑。

| 最低价设置 | 默认值 | 范围 |
| --- | --- | --- |
| 排名检测间隔 | 1 小时 | 1–720 小时 |
| 可用性检测间隔 | 5 分钟 | 1–1440 分钟 |
| 失败重试间隔 | 1 分钟 | 1–1440 分钟 |

三项均在 Web 设置，保存后重新排程。连接失败或等待 Key 时用重试周期，成功后恢复正常周期。需选择 OpenAI 并开启转发账号同步；最低价计划独立于普通同步开关。每 5 秒检查计划，远端任务串行执行，繁忙时等待当前任务完成。

## 员工与每月余额

支持按名单或序号批量创建员工，每批最多 200 人，已有邮箱跳过，省略密码则生成独立随机密码，可下载凭据 CSV，指定初始余额、并发及允许分组。

月度重置按配置时区（默认 Asia/Shanghai）在每月 1 日 00:00，将指定或所有员工的余额设为金额（含 0），管理员默认排除。首次开启从下个月开始，停机恢复只补当月。成功记录不重复重置；网络超时、5xx 或重启导致的未确认结果标为待核对，避免重复覆盖已消费余额。手动立即重置需在页面确认。

## CLI 与测试

CLI 同样读取 Web 保存的文件，默认仅预览：

```sh
python sync_sub2api.py --data-dir data/web --once --dry-run
python sync_sub2api.py --data-dir data/web --once --apply
python sync_sub2api.py --data-dir data/web --inspect-upstream
```

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 tests/browser_smoke.py
```

测试使用隔离模拟站点，覆盖配置文件、迁移、加密、原子保存、周期重排、筛选排序、最低价切换、Key 复用、员工与额度操作、认证和响应式 UI。浏览器测试需本机 Chrome，截图在 `.cache/browser/`。
