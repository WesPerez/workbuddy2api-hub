# WorkBuddy 反代 / 管理项目对比（只读调研，2026-10-13）

> 范围：只读检索与源码核对，未改动 /root 下任何项目，未向任何账号发起真实请求。
> 克隆与抓取痕迹在 `/tmp/wb_src`、`/tmp/wb_research`。

---

## 0. 更正与撤回（对上一版结论）

| 上一版说法 | 处置 | 依据 |
|---|---|---|
| “国际版必须先 chat 再领，/v2/billing/meter/daily-checkin 是领取步骤” | **撤回** | 官方 Subscription 条款：每日奖励“无需手动领取、系统自动计算、次日入账”。脚本里的 claim 调用是脚本自选实现，且其码表含 `41000 活动未开始或已结束` |
| “步骤 2 写 /v2 又说无 v2”（表述自相矛盾） | **更正** | 上游同时存在 `/billing/meter/*`（global 首选）与 `/v2/billing/meter/*`（CN 现状、global 回落）；hub 的 `CHECKIN_PATH`/`CHECKIN_STATUS_PATH` 用的是 `/v2/...` |
| “workbuddy-manager 仅面板 / 仅 OpenAI” | **撤回（已过时）** | manager `server/main.py` 自述“管理 API + 对外反代网关”；README:153-158 列 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/messages/count_tokens` |

---

## 1. 官方口径（一手）

- 定价与每日奖励额度：<https://www.workbuddy.ai/docs/workbuddy/pricing>
  - Free 100 credits/月；Pro 1,000 base + 1,000 bonus = 2,000/月。
  - Promotional Bonus：**Free 30 credits/day、Pro 50 credits/day**（限时活动）。
- 条款与入账规则：<https://www.workbuddy.ai/docs/workbuddy/subscription#limited-time-daily-activity-reward-terms>
  - 活跃判据原文：*“initiate at least one valid AI conversation or task through the WorkBuddy or CodeBuddy client”*；同节明列 *“Conversations or tasks initiated on the web do not count”*。
  - 不算有效活跃：只打开页面或登录、网络/风控失败的提交，以及 *“Activity generated through scripts, plug-ins, emulators, bulk registration, or other improper methods”*。
  - 发放规则：*“no manual claim is required”*，*“credits are generally issued to the user's Buddy AI account the following day”*。
  - 撤销条款：使用脚本 / 插件 / 模拟器 / **API 调用** / 批量注册生成活跃，可被撤销资格、追回奖励。
- 新用户：注册并首次登录送 250 credits，有效期 14 天（Subscription 页 New User Benefits）。

**结论**：国际版每日 30/50 是“客户端真实活跃 → 官方次日自动入账”，既没有可操作的领取端点，也不允许用脚本/API 造活跃。国内版的签到/成长任务是本地判定的领取式玩法，与国际版机制不同。

---

## 2. 国际版链路的真实实现（逐源核对）

### 2.1 L0NE-6/WorkBuddy-Daily（脚本，非反代）
- 文件：`workbuddy_intl_daily.py`
- 常量（`:47-48`）：`STATUS_PATH = "/billing/meter/checkin-activity-status"`、`CLAIM_PATH = "/v2/billing/meter/daily-checkin"`。
- 主流程顺序（`:363-372`）：先 `detect_activity()` → 若 `available and active and not checked` 才 `claim_daily()` → 再 `keepalive()`（发一次 16 token 的流式对话）。
- `keepalive()` 调 `POST https://www.workbuddy.ai/v2/chat/completions`，模型链 `deepseek-v4.1-flash, hy3, hy4-preview, hy4-preview-f`（`:52, :202-215`）。
- 说明：脚本自身也没证明 claim 对 intl 有效；码表把 `41000` 标为“活动未开始或已结束”，与官方“无需手领”一致。**不要把该 claim 当作国际版机制。**

### 2.2 ardeyouxipianyi/workbuddy2api-hub
- `wb_accounts.py:750-752`：`canCheckin = realm == "cn" and not enterprise`；`canDailyChat = realm == "intl"`；`canReportActivity = realm == "cn"`。→ 国际版走的是 `daily_chat`，不是签到。
- `wb_accounts.py:204,209`：`CHECKIN_PATH = "/v2/billing/meter/daily-checkin"`、`CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"`（带 `/v2`）。
- `wb_scheduler.py:200-212`：intl 账号执行 `daily_chat()`，日志写明“送 30/50 积分福利”。
- **争议点（与官方条款冲突）**：`wb_accounts.py:22-36` 注释称“桌面身分发 chat/completions 不计数，只建会话不接沙箱也不计数，真正认的是跑完的网页 agent 会话”，并据此新增 `daily_chat_web()`（`:1313-1388`，走 `console/as/conversations` + 沙箱 link/token + ACP `initialize/session/load/session/prompt` 轮询到 completed）。
- 该主张来自用户实测 issue：#59（closed，用户称“30 积分需网页版对话才能领到”）、#90（closed，用户给出“网页手动发一轮、有返回内容则次日到账”的对照表）。
- **风险判定**：这与官方 2026-10-12 条款“web 对话不计入”**直接矛盾**。两种可能——条款与实测口径不同（如活动早期与现在规则变化），或用户实测不可复现。无论哪种，**该 claim 不可作为部署依据**，且以“跑完网页会话”来触发奖励，正落在官方列举的撤销情形精神范围内。

### 2.3 linguo2625469/workbuddy2api-panel
- `internal/scheduler/scheduler.go:461-465`：`if a.IsGlobal() { continue }`，即**国际版跳过签到**，不对 global 发起任何签到请求。
- `internal/scheduler/scheduler.go:503-536`：`RunActivityNow` 对 CN 与 global **都**做 `/v2/report` 活跃上报（注释 PR #45 实测 global `/v2/report` 返回 code=0），用于点亮连登。
- `internal/upstream/client.go:753-775`：虽定义了 `dailyCheckinPath = "/billing/meter/daily-checkin"`（global 首选）与 `/v2` 回落，但签到入口对 global 已门控，实际不会为国际版调用。
- 即：panel 对国际版只做“活跃上报”，不做领取。

### 2.4 ithtelab/workbuddy-manager
- `server/services/tencent.py:505` 注释直接写明：“**国际版没有签到体系**：上游调度器对 global 账号直接过滤、不发起任何请求”。→ 与国际版“无手领端点”的官方口径一致。

---

## 3. 候选项目对比表

数据：GitHub API，2026-10-13 快照。

| 项目 | ★/Fork | 语言 | 许可 | 最近推送 | 对外协议 | 区域 | 日常任务 |
|---|---|---|---|---|---|---|---|
| [linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel) | 2932/831 | Go | MIT | 10-12 | 仅 OpenAI Chat + `/v1/models`（无 `/v1/messages`、无 `/v1/responses`） | CN + global 路由 | CN 全量（签到/17 任务/旅行/连登兑换/抽奖）；global 仅 `/v2/report` |
| [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) | 1071/240 | Python + Next.js | MIT（GitHub 元数据 NOASSERTION） | 10-12 | **自带网关**：Chat / Responses / Anthropic Messages / count_tokens | CN + global 登录+地区注册 | 定时只跑幂等 `claim`；点亮需手动确认 |
| [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) | 1097/236 | Python | MIT | 10-12 | Chat / Responses / Anthropic | CN + intl | CN 签到/成长/旅行；intl `daily_chat`（含争议的网页通道） |
| [CangShui/workbuddy-gateway](https://github.com/CangShui/workbuddy-gateway) | 264/45 | Go | **无 LICENSE** | 10-12 | Chat / Responses / Anthropic | CN + global | CN 签到；global 跳过 |
| [xiaofan6ya/workbuddy2api](https://github.com/xiaofan6ya/workbuddy2api) | 72/30 | Python | MIT | 10-13 | Chat / Anthropic / Responses | CN 为主 | 成长任务 15 项全自动 |
| [HanawaBanana/workbuddy2api](https://github.com/HanawaBanana/workbuddy2api) | 82/63 | Go | MIT | 10-11 | 仅 OpenAI | 上游精简 | 上游自带 |

---

## 4. workbuddy-manager 深读

### 4.1 它现在是什么
- `server/main.py:1`：`"""WorkBuddy Manager 入口：管理 API + 对外反代网关 + 静态前端托管。"""`
- `server/main.py` lifespan 里启动：`tasklog.start_collector()`、`taskrun.start_scheduler()`、`renew.start_scheduler()`、`accountlog.start_collector()`。
- 结论：**它有自己的对外 `/v1` 网关**（把上游 Chat 翻译成 Responses / Anthropic），同时对上游 Go 服务做账号池管理、日志采集与 token 续期。README 的“配套面板”定位已落后于代码。

### 4.2 协议范围（README:153-158）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 兼容对话（流式/非流式） |
| POST | `/v1/responses`、`/responses` | OpenAI Responses API 兼容（流式/非流式） |
| POST | `/v1/messages` | Anthropic Messages API 兼容（Claude Code 等） |
| POST | `/v1/messages/count_tokens` | 按字符数粗估输入 token |
| GET | `/v1/models` | 模型列表 |
- 路由实现：`server/routers/responses.py:1481,1487`（`/v1/responses`、`/responses`）、`server/routers/anthropic.py`（Messages 翻译层，`:1-30` 头部注释说明“上游只有 OpenAI 兼容接口，本层做双向翻译”）。
- 翻译层不重复实现鉴权/配额/限流：`responses.py:13-15`、`anthropic.py:26-30` 都写明复用 gateway 既有逻辑。
- SSE：`responses.py:843` 起为“Chat Completions SSE → Responses 事件流”，`:1253` 有 1 MiB 解析缓冲上限；错误回非流式 JSON（`:30`）。

### 4.3 反向代理与上游关系
- `server/routers/gateway.py:770`：`url = f'{upstream["base_url"]}{upstream_path}'`；`:472-493` 支持多上游（`upstreamsvc.resolve_for_key`）。
- `docker-compose.yml`：`WB2API_BASE` 指向上游；注释明确“**本镜像不含上游 workbuddy2api**……两边分别用 compose 起，管理端通过 WB2API_BASE 连接上游”。
- 注意：`docker-compose.yml` **同时挂载** `../workbuddy2api:/opt/workbuddy2api`（读写 `auths/` 与 `config.json`）与 `/var/run/docker.sock`（容器内可重建上游容器），并以 `group_add: ${DOCKER_GID}` 进入 docker 组。挂 socket 的等价权限风险在 Dockerfile 顶部注释里被显式讨论（结论：宿主部署本就以 root 运行，两者权限等价）。

### 4.4 账号导入/导出 API
- `server/routers/accounts.py`：`export_accounts`（`:107`）、`upload_accounts`（`:147`，支持上传文件/zip，逐条导入并记录审计 `security.audit(user, 'accounts.import', ...)`）。
- 账号目录解析 `_safe_file()`（`server/services/wb2api.py`）：只接受 `workbuddy*.json`，拒绝 `/`、反斜杠、`..`、NUL；注释说明通配宽度要与上游 `auth.AuthFileGlob = "workbuddy*.json"` 对齐。
- 分组/多账号池：`_group()`、`_group_dir()`（`:312,330`）。

### 4.5 token 续期
- `server/services/renew.py`：进程内每小时巡检，剩余寿命 < 3 天即主动刷新并原子写回账号文件（`:26-34, :62-66`）；`renew_once()`（`:120`）统计 `checked/renewed/failed/skipped`；缺 `refreshToken` 时明确报“需重新扫码或登录”（`:148-151`）。
- 设计理由：上游只在“保活时刻”与“有流量时”刷新，长期闲置账号会过期（`:11-14`）。

### 4.6 国际版支持
- `server/routers/accounts.py:504-570` `auth_start` 接受 `realm`（cn/global，body 优先）与 `region`（如 HK）。注释记录过一个真实 bug：标量参数走 query，body 里的 realm 被静默忽略导致“切国际版仍出国内二维码”。
- `server/services/tencent.py:116-129` `start_login`：`POST {chat_base(realm)}/v2/plugin/auth/state?platform=CLI`，global 时基址为 `www.workbuddy.ai`（`:956` 设置 `X-Domain: www.workbuddy.ai`）。
- 地区注册：`submit_region()`（`:1116`）与 `INTERNATIONAL_REGIONS`，形状对齐上游参照实现 `scripts/global_region.py`。
- **但不做国际版每日活跃**：`tencent.py:505` 明示沿用上游对 global 的过滤。

### 4.7 许可与 NOASSERTION
- `LICENSE`：标准 MIT 全文（Copyright (c) 2026 WorkBuddy Manager contributors），**没有**附加使用限制。
- 文件末尾 `---` 之后是 `Third-party notices:`：UI 组件改编自 linux-do/cdk（MIT）、后端账号池交互目标为 workbuddy2api。
- GitHub 元数据之所以是 `NOASSERTION`：`LICENSE` 在标准 MIT 文本后追加了第三方声明段，`licensee` 的自动识别无法把整文件判为纯 MIT，因此不给出 `MIT` 结论——属于**识别问题，不是许可证差异**。

---

## 5. panel / hub 的流式与刷新

### linguo panel
- 网关路由（`internal/server/handler.go:149` 等）：只有 `/v1/chat/completions` 与 `/v1/models`；README:690-691 确认。**无 `/v1/messages`、无 `/v1/responses`**（在我抓取的 main 快照上）。
- 流式：chat 支持 SSE 流式/非流式（README:337-346 示例含 `stream:true`）。
- token 刷新：`internal/upstream/client.go:910-920` `RefreshToken()`，端点 `chatBase(a) + "/v2/plugin/auth/token/refresh"`；另有调度保活。

### hub
- 入口注释 `wb_proxy.py:7`：`POST /v1/chat/completions (stream=true and stream=false)`；CORS 前缀含 `/v1`、`/responses`（`:259`）。
- 三协议：README 第 12-16 行（Chat Completions、Responses API、原生 Anthropic Messages）。
- token 刷新：`wb_accounts.py:1221` `refresh()`，刷新头 `X-Auth-Refresh-Source` 按 realm 取 `workbuddy`(cn) / `plugin`(intl)（`:1242`）。

---

## 6. 明确缺口与未知

1. **国际版每日活跃可否由程序触发**：官方条款明确“脚本/API 造活跃可撤销”，且 hub 的“网页通道才计入”与官方“web 不计入”冲突。**不建议作为默认能力**，任何自动打卡都属于规则风险区。
2. **hub issue #75** 无法通过 API 取到（返回空），实际存在性未确认；#59、#90 均为已关闭的用户实测帖，结论未获官方页面支持。
3. **“wgpgic”** 仍未定位到同名仓库；按描述最接近 `88lin/workbuddy-auto-signin`（1174★）或 `L0NE-6/WorkBuddy-Daily`（280★）。
4. **CangShui/workbuddy-gateway 无 LICENSE 文件**：法律上默认保留所有权利，商用/再分发风险高于 MIT 项目。
5. stars/forks 为 2026-10-13 快照，会持续变动；所有“最近推送”均为当日观察值。
6. linux.do 原帖：需登录，匿名抓取受 Cloudflare 拦截；本报告未取得帖子正文，仅依据 README 自述“在 LINUX DO 社区发布”。

---

## 7. 面向部署的取向（推断，供讨论）

- 需要 **Codex（Responses）+ Claude Code（Messages）** 直连：`manager` 或 `hub` 才有原生协议层；`panel` 只有 Chat Completions。
- 需要 **完整管理面**（多密钥、IP/模型白名单、用量审计、token 续期补位、账号导入导出）：`manager` 覆盖面最广，且同时支持 CN 与 global 登录。
- 需要 **国内签到与成长任务最全**：`panel`（含连登兑换、抽奖、企业版门控）。
- 国际版 30/50：**按官方口径不应由程序代刷**；可保留“余额查询、账号导入、正常 API 转发”，把自动造活跃默认关闭。

---

## 8. Manager 收口（2026-10-13，核对 main = `1695ec1` / v1.0.85）

### 8.1 issue #158（namespace 回程）与 #161（PoolTimeout）

**#158 仍 OPEN**（created 2026-10-11，updated 10-12，2 条评论）。维护者回复把修复归因于“上游 workbuddy2api 链路”，但这与代码位置不符：namespace 展开与回程还原在 **manager 自己的** `server/routers/responses.py`：

- `_ToolBridge`（`:78`）docstring：`"Flatten namespace children for Chat, then restore Responses names/types."`；展开逻辑 `:106 if kind == 'namespace'`。
- 但还原表只存了 `(name, restored_kind)`：`self._reverse[chat_name] = (name, restored_kind)`（`:122`），而回程构造函数 `_function_call_item(call_id, item_id, name, arguments)`（`:721-733`）**没有 namespace 参数**；全文件除 `:106` 外没有 `namespace` 的写出。
- 该 issue 第二条（工具结果图片顺序）提到的 `pending_tool_images` 在 `responses.py` 中**不存在**（`grep -n pending_tool_images` 无结果）。

**结论**：#158 的两项修复在当前 main 上**都未落地**；且这是 manager 本仓库的 Responses 翻译层问题，不应只等上游。建议：向维护者指出位置（`_ToolBridge._reverse` + `_function_call_item`），或自行打补丁。

**#161 仍 OPEN**（created 10-12，1 条评论，label=bug）。维护者回复：大批量“已禁用（需重新登录）”是上游判定 `11140 request illegal`（同批账号在腾讯官方网页也报同样错），不是面板禁用；`PoolTimeout` 是**另一件事**——面板到腾讯那条链路排队超时。维护者追加要求：真实面板版本号 + 这批账号是否**同一时间批量导入、走同一出口 IP**（机房段更易被标记）。→ 尚未修复，处于信息收集阶段。

### 8.2 Docker 发行包与 fork 构建

| 问题 | 结论 | 位置 |
|---|---|---|
| 发行包含 Go 网关吗 | **Release 压缩包含**，位于包内 `upstream/`；**Docker 镜像不含** | `.github/workflows/release.yml`（从 `releases/download/upstream-src/workbuddy2api-src.tar.gz` 拉取并塞进 `$STAGE/upstream`，取不到仅告警）；`Dockerfile` 顶部注释第 1 条“本镜像不含上游 workbuddy2api” |
| Go 网关来源 | manager 自己的固定 Release 资产 tag `upstream-src`（标为 pre-release），**不是** Sliverkiss 原仓 | release.yml 注释：“上游源码不放进本仓库（不是我们的代码），其公开地址也已不可用” |
| fork 能否脱离 Sliverkiss 404 独立装 | 能，但要显式指定：`UPSTREAM_SRC=<目录/包>` 优先，其次包内 `upstream/`，再次目标目录已有 `.git`，最后才 `UPSTREAM_REPO` | `deploy/install.sh:27-33, :134-142` |
| 默认上游地址是否可用 | **不可用**（Sliverkiss 已 404）。脚本已写明“默认值保留原地址只为『已有副本的人不必改代码』；新装请用 `UPSTREAM_SRC` …或把 `UPSTREAM_REPO` 改成你自己的副本” | `deploy/install.sh:29-33`；`deploy/update.py:161` 同样默认 `WB_UPSTREAM_REPO` = 该 404 地址 |
| 自建镜像工作流 | 有，`deploy/fork-image/build-image.yml`：零配置推 `ghcr.io/<owner>/workbuddy-manager-multiarch`，多架构 amd64+arm64、按默认分支触发、npm cache、`concurrency` 排队；可选 Docker Hub secrets；**不建 Release、不签名** | 需手动 `cp deploy/fork-image/build-image.yml .github/workflows/` |
| 要不要 UPSTREAM_URL/tag/cache | 管理器镜像本身**不需要** UPSTREAM_URL（镜像从不含上游）；重建发行包时才需要 `UPSTREAM_SRC_URL`，默认指向 manager 自己的 `upstream-src` tag | release.yml 中 `UPSTREAM_SRC_URL` |
| 一键更新签名 | `deploy/update.py` 用内置公钥强制验签，未签名 Release 会被拒；fork 自建包需 `WB_SKIP_SIGNATURE=1` 才能更新 | `deploy/update.py:194-196, :374-409` |

**healthcheck / 数据目录 / 默认调度**
- 管理端健康检查：`GET http://127.0.0.1:7864/api/healthz`（`Dockerfile` HEALTHCHECK 与 compose healthcheck 一致）。
- 上游健康检查：`update.py upstream_health_url()`，顺序为 `WB_UPSTREAM_HEALTH_URL` 显式 URL → … → `http://127.0.0.1:7863/healthz`（`:66-98`）。
- 数据目录：`WB_DATA_DIR=/app/data`（compose 挂 `./data`）、`WB_AUTH_DIR=/opt/workbuddy2api/auths`、`WB_UPSTREAM_CONFIG=/opt/workbuddy2api/config.json`，并整体挂载上游目录 `../workbuddy2api:/opt/workbuddy2api` + `/var/run/docker.sock`；端口仅绑定 `127.0.0.1:7864`。
- 默认调度：`taskrun.start_scheduler()` **只自动跑幂等 `claim`**（不伪造活跃，`:main.py` lifespan 注释）；`renew.start_scheduler()` 每小时巡检、剩余 < 3 天续期；另有 tasklog / accountlog 采集器。

### 8.3 管理 API token scopes 能否支撑 WatchDeck / MetAPI 免会话

只有两档 scope：`readonly` → viewer、`admin` → admin（`docs/api-tokens.md:61, :76-77, :131-132`）。令牌 CRUD 一律仅会话（`server/routers/tokens.py:1-8`），令牌不能自我提权。

| 需求 | 实际依赖 | 结论 |
|---|---|---|
| WatchDeck `auth/start` | `POST /api/auth/start` = `require_admin` | `admin` 令牌可用 |
| WatchDeck `auth/poll` | `GET /api/auth/poll` = `require_admin` | `admin` 令牌可用 |
| WatchDeck 导入账号 | `POST /api/accounts/upload` = **`require_session_admin`** | **令牌不可用，必须真人会话** |
| MetAPI 只读 credits（明细） | `GET /api/accounts/{filename}/credits` = `require_admin` | `readonly` 不够 |
| MetAPI 读 credits 快照 | `GET /api/accounts/credits-snapshot` = `current_user` | `readonly` 可用 |
| MetAPI 触发刷新积分 | `POST /api/accounts/refresh-credits` = `current_user` | `readonly` 也能触发（写语义） |
| MetAPI 任务命令 | `GET/POST /api/task-run` = `require_admin`；`/api/task-logs/collect` = `require_admin` | 需 `admin` 令牌 |
| 导出 / 删除 / 移动 / 重启 | `require_session_admin` | 令牌一律不可用 |

**收敛建议**：现状无法做到“WatchDeck 仅 start/poll+导入、MetAPI 只读 credits+任务命令”且全用令牌——**导入**、**导出/删除**、**重启**被刻意锁在会话内。可行的最小调整只剩两条：给 WatchDeck 一把 `admin` 令牌（导入仍需走会话或新增专用端点），给 MetAPI 一把 `admin` 令牌（若只用 credits-snapshot，则 `readonly` 足够）。

### 8.4 `auth/start` 的 region 是否强制 / OAuth 成功副作用

- `auth/start` 的 `realm` 与 `region` 都是可选（`accounts.py:504-515`）；`region` 可晚于 start 到达，`auth/poll` 也会接收并记录（`:592-596`）。
- **已注册账号可省**：`_poll_login_background` 在 `region` 为空时调 `tencent.registration_status()`；已完成则不加提示。**未注册的国际版账号**会返回“尚未完成地区注册，请在弹窗中选择地区后重试（否则聊天会报 14017）”（`:638-646`）——即不是硬性必填，但账号在补地区前不可用。
- **OAuth 成功即产生副作用**（`:625-662`）：① 国际版地区注册 `submit_region()`；② 一次性 trial `claim_trial()`（幂等，`tencent.py:1202-1227`）；③ 自动签到 `tencent.checkin()`（国际版直接跳过）；④ `creditsvc.invalidate()`；⑤ `_mark_provisioned(state)` 先落标记，保证落盘失败重试时不重跑副作用；⑥ `_save_and_finish()` 写账号文件 + 热加载。

→ 即：**“登录成功”就等于“账号已入库 + 已注册地区 + 已领 trial + 已尝试签到 + 已热加载”**，不是纯取凭据。若由 WatchDeck 自动跑完 poll，会连带产生这些账号状态变更。

### 8.5 续期双写（manager renew vs Go refresh）

- manager 侧写入：`_atomic_write_json()`（`tencent.py:220-268`）唯一临时名 + `fsync` + `chown/chmod` + `os.replace`；docstring 明确要求原子性，因为上游每 5 秒按“文件名+mtime+大小”扫描 auths，直接 `write_text` 的 0 长度窗口会被判定为账号被删。临时名以 `.` 开头、不以 `.json` 结尾，规避上游 glob 与指纹。
- 续期走 read-merge-write：`update_auth_tokens()`（`:272-315`）先读整份 JSON，只改 `auth.accessToken/refreshToken/expiresAt/domain`（并补 `realm`），再整体原子替换，**保留 `device_token` 等未知键**。
- **没有跨进程锁**：manager 与 Go 是两条独立进程，靠“最后写入者获胜”的原子替换，不是互斥。窗口是 manager 读取之后、`os.replace` 之前；若 Go 恰在此间刷新成功，manager 会用自己那份旧快照整体覆盖，**覆盖掉 Go 写入的新 token**。当前无法从源码证明有加锁，属于**待实测确认的竞态**。
- 缓解方向：让 manager 复检 mtime/内容后再写（CAS），或把续期职责收敛到单侧（例如 manager 只读、由 Go 的 keepalive 负责写，或反之）。

---

## 9. 成熟度与口碑证据审计（快照 UTC 2026-10-13T03:12:28Z）

> 方法：八个仓库的实时 GitHub API 元数据 + `/releases/latest` + 近 6 次提交 + `.github/workflows` + `/actions/runs` 最近一次。
> **星标不是口碑，也不代表支持质量；“三协议”不等于 100% 兼容。**

| 仓库 | ★/Fork | Open issues | 创建 | 最近推送 | 最新 Release | 最近 CI |
|---|---|---|---|---|---|---|
| [linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel) | 2939/834 | 61 | 09-12 | 10-12 | v1.13.0（10-08） | last `docker-ghcr`：**action_required**（需人工批准，非构建失败） |
| [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) | 1075/240 | 9 | 09-11 | 10-12 | v1.0.85（10-12） | last `Release`：**success**（10-12 17:16） |
| [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) | 1100/237 | 19 | 09-12 | 10-12 | v1.7.2（10-12） | last `tests`：**success**（10-13 02:46） |
| [changexbc/workbuddy-switch](https://github.com/changexbc/workbuddy-switch) | 1227/156 | 26 | 08-16 | 10-12 | v0.1.62（10-12） | last `build`：**success** |
| [cv-superding/WorkBuddy-Switch2api](https://github.com/cv-superding/WorkBuddy-Switch2api) | 44/8 | 1 | 09-16 | 10-12 | v1.6.9（10-12） | last `build`：**success** |
| [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin) | 1176/105 | 1 | 08-18 | 10-12 | v1.7.0（**09-23**） | last `Tests`：success（10-12） |
| [L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily) | 280/129 | 1 | 09-12 | 10-12 | v7（10-12） | last `workbuddy`：**failure**（10-06） |
| [babygoton/WorkDaddy](https://github.com/babygoton/WorkDaddy) | 1874/195 | **74** | 08-18 | 10-12 | 1.2.11（10-01） | last `build-win`：**failure（08-29，约 6 周未跑成功）** |

**年龄维度**：panel / manager / hub 均创建于 2026-09-11～09-12，**约一个月**，属同期新项目；switch / auto-signin / WorkDaddy 创建于 08-16～08-18，约两个月。全部 `archived=false`。

**活跃维度（近 6 次提交）**：panel、manager、hub、switch、Switch2api、WorkDaddy 在 10-12 当日均有实质提交；auto-signin 最近两次为 README 赞助段（内容提交停在 10-01）；Daily 在 10-12 合入国际版设备授权登录。

**测试/CI 规模**：panel 68 个 `*_test.go`；manager 124 个 `server/tests` 用例 + 52 个 `dev/verify*` 脚本；hub `tests/` 150 个文件；switch 有三个工作流（build/test/pages）。WorkDaddy 有 `build-win.yml` 但最近一次是 8 月底失败；Daily 的定时工作流 10-06 失败。

**hub 的维护风险（巨型单文件）**：`wb_proxy.py` **18,930 行**（另有 `wb_accounts.py` 3,600、`wb_settings.py` 1,685），合计 25,094 行。单文件近两万行意味着：路由、协议适配、面板后端与业务逻辑共处一文件，merge 冲突面与评审成本都高；这也解释了它近期的修复集中在“面板窗口限高 / namespace 回程”这类跨界改动上。

**hub 的“active default”风险**：`wb_settings.daily_chat_web()`（`wb_settings.py:1249-1261`）**未配置时返回 True**，docstring 明写 “On unless the operator turns it off”。即默认安装会自动执行网页通道打卡——这与官方条款“web 对话不计入、脚本/API 造活跃可撤销”直接冲突，属默认开启的合规风险，不是可选项遗漏。

**panel 的 17/18 任务与 issue #163**：GitHub issue #163 **OPEN**（2026-10-12 建，1 条评论；用户称“下午一键完成任务搞了没用，晚上又正常了”），标题“一键完成任务功能和任务中心都失效了”。因此 README 宣称的“18 个任务中 17 个可一键完成”应视为**宣称值**，与实际可用性存在波动；建议把它当“多数任务可自动化”，而非 SLA。

**hub namespace 修复已落地**：issue #308（Codex Desktop 0.162+ 把工具放进 `additional_tools`，网关未处理）**已于 10-12 关闭**；对应提交 `fix(responses): 命名空间内嵌 custom 工具回程判成 function_call`（10-12 08:52）与 PR #310。相对地，manager 的同类问题 #158 仍 open（见 8.1）。

**关于“原仓私有”的归因纪律**：`Sliverkiss/workbuddy2api` 公开地址 404 是**已核实**；*“原作者转为私有、维护者获授权持续获取更新”* 一句仅来自 **manager 的 `upstream-src` Release body（2026-09-24 发布，资产 10-10 更新，sha256 由审阅方提供）**，属**单一来源归因，未获独立证实**。panel README 的“已删库”则是另一方的说法。综合表述应为：**公开原仓 404 不可访问，维护方各执一词，分支来源分化**。

**口径提醒**：本表 Open issues 数量未按“真 bug / 功能请求 / 重复”分类（manager 9 条最少，WorkDaddy 74 条最多），不能单独作为质量结论；同时“支持三协议”只说明存在翻译层，不代表每个客户端的工具调用、命名空间、流式语义都已 100% 兼容——panel #163、manager #158 正是反例。

**更正**：上文表中的 `open_issues_count` 取自 GitHub 仓库对象，**包含未合并的 Pull Request**，不是纯 issue 数，也不等于 bug 数。最终对外引用时应删除该列或显式标注“含 PR”。

---

## 10. 三个补充候选（只读核 README + 入口/模块，2026-10-13 UTC）

### 10.1 ShouZhuo0413/codebuddy2api（358★/77，Python，MIT，建 06-26，推 09-30）

- 协议齐全：`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/models`、`/health`（README 首段与“当前支持”表）。
- **仅国内**：转发到 `copilot.tencent.com`，README:333 写“浏览器授权添加**国内** CodeBuddy / WorkBuddy 账号”；未见国际版入口。
- 能力：多账号轮转、冷却、积分耗尽避让、凭据刷新、余额查询、每日自动签到（默认北京 09:00 可配置，README:334）；可选 Web 管理台 `admin/`（`server.py` 26KB、`pool.py` 19KB），核心 `core/` 为 `converter.py` 57KB + `responses_adapter.py` 22KB + `responses_projection.py` 22KB + `anthropic_adapter.py` 18KB；`tests/` 11 个文件。单文件偏大是其主要维护风险。
- 适合：纯国内、要三协议 + 多账号 + 内建签到的单机 Python 部署。不适合：国际版 30/50、需要最新 Responses 命名空间修复的项目。

### 10.2 lovingfish/workbuddy-cliproxy（194★/57，Go，MIT，建 07-07，**推 07-08**）

- 定位是 **CLIProxyAPI 插件**而非独立网关：编译为 `.so/.dylib/.dll` 放入 CPA 的 `plugins/`，依赖 CPA v7.2.x + CGO + 架构匹配（README「安装」）。
- **仅国内**：`main.go:80 upstreamBase = "https://copilot.tencent.com"`；协议由 CPA 转出 OpenAI 与 Anthropic，README 未列 Responses。
- 能力：扫码登录、token 刷新、凭据存 `workbuddy.json`；模型为写死清单；**无签到/积分/任务**（`main.go` 35KB 单文件）。
- 适合：已在跑 CLIProxyAPI、只要一个轻插件复用国内 CodeBuddy。不适合：独立部署、国际版、任务自动化。**约三个月未推送，属“可用但停更”**。

### 10.3 maiphucgiang/codebuddy2api（190★/43，Python，MIT，建 09-10，推 10-11）

- 三协议 + 自带 WebUI + **双区**：README 明写 “Chat Completions, Responses and Anthropic Messages, with tool calling and streaming” 与 “Automatic multi-account routing across domestic and international sites, with credential refresh”，预构建 GHCR 镜像开箱可用。
- **国际版活跃默认关闭**：`app/daily_chat.py:9-10` “the switch **defaults to off** and international accounts opt in explicitly, exactly like the check-in and travel preferences.” 与 hub 的默认开正好相反，对本项目场景更安全。
- 模块化程度高：`app/` 含 `checkin.py`、`daily_chat.py`、`travel.py`、`trial_management.py`、`credits.py`(36KB)、`audit_store.py`(35KB)、`control_store.py`(32KB)；`tests/` 68 个；CI 含 `docker.yml` + `codeql.yml`。风险是单作者、创建仅一个月、模块数 50+，审计面大。
- 适合：要开箱 WebUI + 双区 + 默认关闭国际版活跃的账号/任务自动化。不适合：追求最小依赖，或需要 Manager 那种会话/scope 分级治理。
