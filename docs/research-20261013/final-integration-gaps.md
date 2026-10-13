# 最终整合方案：缺口核验（只读）

日期：2026-10-13（Asia/Shanghai）
范围：`/tmp/wb_src/ardeyouxipianyi__workbuddy2api-hub`（hub）、`/tmp/wb_src/ithtelab__workbuddy-manager`（manager）、`/tmp/workbuddy-research-root-20261013/manager-core`（Go core，已解包）、`/root/metapi-check-in`（MetAPI）。
性质：纯只读。未安装、未编译、未运行、未触真实账号；未修改任何项目。

## 0. 三处必须先更正的前序结论

| 项 | 前序表述 | 实际 | 证据 |
| --- | --- | --- | --- |
| 方案 A 的代价 | 「配置级」 | **不成立**：`renew.start_scheduler()` 在 `server/main.py:51` 无条件调用，`server/services/renew.py` 里没有开关；`keepalive_enabled` 只关 Go 侧保活，不影响 manager renew（`renew.py:30`–`:33` 明确「不看上游的开关」） | `server/main.py:48`–`:57`；`server/services/renew.py:30`–`:33`、`:221`–`:235` |
| Core 是否支持每账号出口 | 未提及 | **不支持**：`internal/auth/auth.go` 的 `Auth` 结构无 `Proxy` 字段；`cmd/server/config.go` 与 `config.example.json` 无 `proxies`；`internal/upstream/transport.go:84` 只有 `http.ProxyFromEnvironment` | `internal/upstream/transport.go:78`–`:84`；`grep -rn -i proxies --include='*.go'` 零命中 |
| Hub 登录是否有账号级出口 | 未提及 | **没有**：`_route_accounts_login_start` 只收 `realm`/`platform`（`wb_proxy.py:17567`）；`start_login`（`wb_accounts.py:3120`）无 proxy 参数；`poll_login` 的 profile 拉取直接用 `urllib.request.urlopen` | `wb_proxy.py:17567`–`:17575`；`wb_accounts.py:3120`–`:3141`、`:3182`–`:3195` |
| Hub intl 是否支持 GitHub 登录 | 前版误判为「不支持」 | **支持**：intl 的 `chat_upstream` 即 `https://www.workbuddy.ai`（`wb_accounts.py:176`），官方登录页明确提供 **Google 或 GitHub OAuth** | `wb_accounts.py:173`–`:183`；官方文档 `official-more-2.html`（原文见 §1.1） |

**更正后的方案表述**：不是「关一个配置就行」，而是**新增开关并把刷新入口切到手工/单一主控**。仅禁后台 renew 而保留手工 refresh，仍会与 Go 保活并发写同一文件——竞态不消失。

## 1. Hub：让 WatchDeck 自动导入完整 intl credential 的接口与认证

| 问题 | 结论 | 证据 |
| --- | --- | --- |
| 导入端点 | `POST /accounts/import` | `wb_proxy.py:17210` 路由 → 实现 `wb_proxy.py:17809` `_route_accounts_import` |
| 是否必须真人会话 | **必须是面板会话**。`/accounts*` 全部落在 `_is_panel_route`，先过 `_panel_ok()` | `wb_proxy.py:15711`（`path.startswith('/accounts')`）、POST 侧 `:18302`、GET 侧 `:15758` |
| 有无管理 API Token 可替代 | **没有**。Hub 不存在 `wbt_` 类令牌；会话只能是 `X-Panel-Token` 头，且 `PanelSessions` 是**内存态**（重启即失效） | `wb_proxy.py:15701`–`:15709`；`wb_settings.py:1647`–`:1675`；`grep -n 'wbt_'` 零命中 |
| 接受的字段形态 | ①导出文档 `{format,version,exportedAt,count,accounts:[…]}` ②裸数组 ③单账号对象 ④桌面凭据 `{account:{…},auth:{…}}` ⑤cockpit snake_case 行 | `wb_accounts.py:3477` `_coerce_account_rows`；`:3525` `normalise_import_row` |
| 选项 | `dryRun`、`overwrite`、`realm`（强制 intl/cn） | `wb_proxy.py:17832`–`:17844` |
| 校验 | accessToken 必须是 3 段 JWT；realm 由 token/domain 证据判定，忽略行内过期 realm | `wb_accounts.py:3559`–`:3572` |

**关键约束**：Hub 没有「给脚本用的管理令牌」这一层。任何自动导入都必须持有 `X-Panel-Token`，而该 token 只能在面板登录后换取、且不落盘。**这意味着 WatchDeck 要么代为持有面板密码做登录换取会话，要么该路径必须改 Hub 代码。**（对照：Manager 有 `readonly`/`admin` 两档 API Token，但导入同样被锁在 `require_session_admin`，见 `server/routers/accounts.py:146`–`:151`、`server/security.py:521`–`:533`。）

### 1.1 start/poll 工作流：官方授权页在 intl 支持 GitHub 登录（既非「腾讯专属」也非「GitHub device flow」）

**区域决定端点**：`get_realm_config(realm)` 取 `REALM_CONFIGS`，intl → `https://www.workbuddy.ai`，cn → `https://copilot.tencent.com`（`wb_accounts.py:173`–`:200`、`:217`–`:218`）。`start_login` 的 `realm` 决定用哪一套（`wb_accounts.py:3129`）。

| 步骤 | 事实 | 证据 |
| --- | --- | --- |
| 起点 | `POST /accounts/login/start`，参数 `realm`/`platform`（`platform` 默认 `CLI`） | `wb_proxy.py:17190` → `:17567`–`:17575` |
| 上游调用 | `POST {chat_upstream}/v2/plugin/auth/state?platform=<platform>`，返回 `{state, authUrl}` | `wb_accounts.py:3129`–`:3138` |
| 用户动作 | 打开 `authUrl`，**在浏览器完成官方登录**；README 明确「选择要登录的区域（国际版 / 国内版），点弹出的官方授权链接并在浏览器完成登录」 | `README.md:277`–`280` |
| intl 的登录方式 | 官方 WorkBuddy 文档原文：*“If not logged in, the system will display a login guide. Click the login button and sign in with **Google** or **GitHub** OAuth.”* | `/tmp/workbuddy-research-root-20261013/official-more-2.html`（官方文档存档） |
| cn 的登录方式 | README 概览与 usage 文档以「微信 / QQ 扫码」描述默认/国内路径 | `docs/usage.md:8`、`README.md:42`、`:70` |
| 轮询 | `GET /accounts/login/poll?state=…`；上游 `LOGIN_PENDING`(11217) → `waiting for browser login` | `wb_proxy.py:15784` → `wb_accounts.py:3143`–`:3180`；常量 `wb_accounts.py:213` |
| 完成 | 拿 WorkBuddy `accessToken` → `jwt_uid` → 拉 profile → 构造 `Account` 入库 | `wb_accounts.py:3172`–`:3195` |

**两处必须同时避免的误称**（前版犯了第二处）：

1. 这**不是** GitHub 的 device flow。`state`/`authUrl` 由**腾讯 WorkBuddy 服务端**签发、无 PKCE，登录态是 WorkBuddy 自己的 `accessToken`/`refreshToken`。
2. **也不能反过来说 intl 不支持 GitHub**。intl 走 `www.workbuddy.ai`，其官方登录页把 GitHub 列为可选 OAuth 提供方（见上表官方文档原文）。

**对方案的准确含义**：WatchDeck 手里已有的 GitHub **网页会话**，可以用浏览器打开该 `authUrl` 并完成 GitHub 授权（首登/新设备验证按 GitHub 侧要求），随后 Hub 的 poll 自动拿到 **WorkBuddy 凭据**并入库。**这不是「用 GitHub token 直接兑换 WorkBuddy token」，而是「用 GitHub 会话完成官方登录页，换取 WorkBuddy 凭据」**——两者别混。

⚠️ 待你确认的现实约束：GitHub 在 WorkBuddy 登录页是**浏览器内交互**（可能含设备验证/2FA）。WatchDeck 的辅助浏览器可以驱动它，但「端到端无人值守」取决于该页是否复用了 WatchDeck 已有的 GitHub 会话——这一点本次无法从 hub 源码判定，需要一次真实链路验证（本次只读，未执行）。

## 2. Hub：refresh / checkin / tasks 的关闭粒度

| 维度 | 现状 | 证据 |
| --- | --- | --- |
| 调度器开关 | **只有一个全局 `enabled`**，无按任务开关 | `wb_scheduler.py:25` `self.enabled = True`；`wb_proxy.py:17434`–`:17437` `POST /scheduler/toggle` |
| 单次触发 | `POST /scheduler/trigger` 跑一轮 | `wb_proxy.py:17429`–`:17430` |
| 时刻配置 | **全部硬编码**为类属性（签到 `[9,21]`、旅行 `[9,21]`、保活 `[22]`、猫 `[1]`），`all_hours` 由其并集算出；**没有 settings 键、没有 API、也没有 `schedule_schema`/`update_schedule`**（hub 与 manager 两仓 grep 均零命中） | `wb_scheduler.py:19`–`:24`、`:85`–`:113`；`wb_proxy.py:18739`–`:18742` 只用 `Scheduler(POOL)`，不传 hours |
| 单全局 enabled vs 可调 hours | **只有 `enabled` 一个运行期开关**（`/scheduler/toggle`）；**hours 不可调**。两者不是「全局开关 + 可调时段」的组合，而是「全局开关 + 写死时段」 | `wb_scheduler.py:25`、`:85`–`:93` |
| `daily_chat_web` | 是**真实存在**的 settings 项（默认开），但它控制的是「intl 每日活跃是否额外走网页通道对话」，**不是排程时刻开关** | `wb_settings.py:1249`–`1272`；消费点 `wb_accounts.py:1446` |
| 手工端点 | `/accounts/checkin`、`/accounts/refresh`、`/accounts/daily-chat`、`/accounts/daily-chat-web`、`/tasks/run`、`/tasks/travel`、`/tasks/streak-bonus` | `wb_proxy.py:17184`–`:17203` |
| 粒度结论 | **可以让 MetAPI 通过 HTTP 触发单次工作**（暂停全局调度器 + 手工调用），**但做不到「保留保活、只关签到」**这类按任务关闭——需要改代码 | 同上 |

**避免双调度**：`POST /scheduler/toggle` 可暂停 hub 的**整点定时**，工作改由 MetAPI 触发（需面板会话）。但如下方所述，**单靠 toggle 不能完整关闭自动任务**，需要补丁才能达到「只有 MetAPI 能触发」的状态。

### 2.1 toggle 不足以关闭自动活跃——三处泄漏（已核实）

| 泄漏点 | 事实 | 证据 |
| --- | --- | --- |
| 启动即跑一轮 | `_run_loop` 在 `time.sleep(10)` 后**无条件**调用 `_execute_cycle("启动初次初始化巡检")`，**不受 `self.enabled` 约束**；`if self.enabled` 只包住后面的整点判定 | `wb_scheduler.py:71`–`93`（初始化调用在 `:75`，`enabled` 判定在 `:85`） |
| toggle 不持久化 | `_route_scheduler_toggle` 仅翻转内存布尔 `SCHEDULER.enabled`，**不写 settings**；`wb_settings.py` 中没有任何 scheduler 键。重启后 `__init__` 的 `self.enabled = True` 复位 | `wb_proxy.py:17433`–`17438`；`wb_scheduler.py:25`；`grep scheduler_enabled/set_scheduler wb_settings.py` 零命中 |
| web=False 仍发对话 | `_daily_chat_upstream` **先无条件**发一条桌面身份 chat（`messages:[{role:user,content:"Hi"}]`、`max_tokens:10`、`reasoning_effort:none`），**之后**才读 `daily_chat_web` 决定是否加走网页通道。`daily_chat_web=False` 只关掉 web leg | `wb_accounts.py:1423`–`1447`（chat 在 `:1426`–`:1435` 先发，`web` 判定在 `:1446`） |

→ **因此「暂停 toggle」或「`daily_chat_web=false`」都不能单独作为完整关闭自动活跃的依据。** 要做到「仅 MetAPI 触发」，最终修补需**持久化按任务/区域的策略，并同时覆盖启动、手工、定时三条路径**（启动路径当前完全绕过开关，是最大缺口）。

**关于「token 双写」（前版此处判断有误，已更正）**：

- Hub 是**单进程、自包含**的 Python 网关，账号文件就是它自己的 `accounts/*.json`；它**不使用 Go core**，与 Go 之间不存在共享刷新文件的关系。
- Hub 内部的写入由账号级锁串行化：`_refresh_lock` 保护 token 刷新、`_save_lock` 保护落盘（`wb_accounts.py:620`–`:630`，注释原文即「without a lock the upstream rotates the refresh token concurrently and the last writer wins, so a freshly minted token can be overwritten by a stale snapshot」）。
- 因此 **MetAPI 通过 HTTP 调 Hub 的 `POST /accounts/refresh`，写入仍发生在 Hub 进程内，仍受同一把锁串行化——是同一 writer，不构成多写者竞态**。
- 前版把它与「Manager+Go core 两进程写同一文件」的情况混为一谈，属于错误类比：那条竞态只存在于 Manager 与 Go core 之间，与 Hub 无关。

→ 更正后的结论：**Hub 作为工作执行者可满足「单刷新所有者」**，MetAPI 经 HTTP 触发仍落在同一 writer，不必要求 MetAPI 放弃 refresh。但「把调度收敛到一侧」这件事**不能靠现有 toggle 完成**——必须先补 §2.1 的三处泄漏（尤其启动路径），否则每次重启都会自动跑一轮。真正的多写者风险只出现在 Manager+Go core 栈，见 §3 与风险审计 §10/§11。

## 3. Manager 能否直接叠加在 Hub 上：**不能**

| 判据 | Manager 的硬依赖 | Hub 的现状 |
| --- | --- | --- |
| 定位 | 「为 workbuddy2api（Go）做**可视化**的配套项目」，上游负责能力、面板负责呈现 | `README.md:7`、`:36`、`:49`–`:53`；hub 自身就是网关+面板 |
| 连接方式 | `WB2API_BASE` 指向 Go 上游（默认 `http://127.0.0.1:7863`） | `deploy/install.sh:300`；Hub 无此概念 |
| 调用的上游端点 | `/status`、`/v1/stats`、`/v1/models`、`/admin/accounts/{uid}/{disable,enable,revive}` | `server/services/wb2api.py:903`、`:941`、`:511`、`:540`；Hub 无这些路由 |
| 配置/状态耦合 | 共享上游仓库目录（`WB_UPSTREAM_CONFIG`）、读上游状态文件、重启上游容器 | `server/services/wb2api.py:627`；`server/services/reload.py`；Hub 无对应契约 |
| 镜像 | `Dockerfile:1`–`10` 明确「本镜像不含上游 workbuddy2api…管理端通过 `WB2API_BASE` 连接上游」 | — |

**结论**：Manager 与 Go core 是**成对契约**（管理面 + 运维端点 + 目录/容器耦合 + `{account}` 代理字段协商）。Hub 是另一套 Python 自包含网关，既无 `/admin/accounts/*` 也无 `proxies`/`{account}` 约定。**没有兼容契约，不能建议叠加**；二选一。

## 4. 数据通路与适配器落点：先纠正「MetAPI 也转发 LLM」的误画

**MetAPI 是纯签到系统，不含 LLM 转发。**

- `AGENTS.md:50`–`:51` 明确把「原 Metapi 面向外部客户端的 `/v1` 网关、下游 Key、Token 路由、计费」列为**已退役、不属于本仓库**。
- 实际注册的路由只有：health、accounts、checkin、auth、settings、sessionRecovery、sub2ApiNoteSync、siteTools（`src/server/index.ts:109`–`:117`），**没有 `/v1/chat/completions`**。
- README 自述：「独立的账号签到服务…签到、余额刷新、模型读取、定时任务」（`README.md:3`）。

**适配器落点（新增平台时改这些）**：

| 用途 | 文件 | 行 |
| --- | --- | --- |
| 平台接口 | `src/server/services/platforms/base.ts` | `:90` |
| 适配器注册 | `src/server/services/platforms/index.ts` | `:10` |
| 别名/host 提示 | `src/shared/platformIdentity.js` | `:1` |
| 每日签到平台枚举 | `src/server/services/dailyCheckinRunner.ts` | `:11` |
| Python 协议实现 | `python/checkinlib/sites/*.py` | — |
| 非签到站点工具 | `src/server/services/siteTools/index.ts` | `:5` |
| 会话恢复契约 | `src/server/contracts/sessionRecoveryPayloads.ts` | `:14` |

**正确数据通路**：

```
WatchDeck ──(GitHub/LinuxDo 会话, 浏览器)──► 目标站点 OAuth
                                              │
                            ┌─────────────────┴─────────────────┐
                            ▼                                   ▼
                  Manager+Go core (持凭据/刷新/聊天)      Hub (持凭据/刷新/聊天)
                            ▲                                   ▲
                            │ 内网桥: 余额/任务/状态             │
                            └──────────── MetAPI ───────────────┘
                                     (只存 remoteUserRef)
```

MetAPI **不承载模型流量**，不参与 `/v1` 转发；它只做签到编排、余额/积分读取与展示。把 LLM 转发画进 MetAPI 会同时违背仓库边界与本次整合目标。

## 5. 待你裁决的开放项

1. **Hub 会话问题**：Hub 的导入与 start/poll 都需要面板会话（无 API Token）。若走 Hub，WatchDeck 必须代持面板密码并登录换取 `X-Panel-Token`（内存态、重启失效，需重登逻辑）。这是额外成本，Manager 侧没有等价障碍（有 admin 令牌，仅导入受限）。
2. **Hub 的调度收敛**（已更正）：refresh 写回本身没有问题（单进程、账号级锁串行化）；需要补的是**开关的完整性**——补丁须新增持久化的按任务/区域策略，并覆盖启动初始化、手工、整点定时三条路径（见 §2.1），单靠 `toggle` 或 `daily_chat_web=false` 都不够。
3. **Manager 的每账号代理**：core 无 `proxies`/`proxy` 字段，只有进程级 `HTTP_PROXY`。若要求继承 WatchDeck 的 egress 一致性，需**补 core 代理字段**或**按出口/region 拆多实例**；manager 面板的「账号线路」目前只作用于管理端自身请求（`docs/account-proxy-routes.md:85` 起明确「原版上游不具备这一支持，仅安装管理端不能保证聊天也走绑定线路」）。
4. **Hub 的登录无账号级出口**：`start/poll` 与 profile 拉取都走进程网络（`urllib.request.urlopen`），登录阶段与其他账号出口不隔离；`_route_accounts_login_start` 也只接受 `realm`/`platform`（`wb_proxy.py:17567`），无法在发起时指定出口。若要全链路一致，WatchDeck 桥需把 `egress_ref` 绑定到 pending OAuth state，并让整条链（start → 浏览器授权 → poll → 入库 → 后续请求）走同一出口。注意 intl 浏览器授权页支持 GitHub 登录（§1.1），该阶段同样需要出口一致。
5. **Hub 缺少 intl 地区注册（新号可能报 14017）**：Hub 侧对 `region`/`14017`/`registration` **零支持**——`wb_accounts.py`、`wb_proxy.py`、`wb_identity.py` 三处 grep 均无命中，poll 完成后直接构造 `Account` 入库，没有地区补登记步骤。对照 Manager 明确实现了这一步：`registration_status`（`server/services/tencent.py:1083`）、`submit_region`（`:1116`），并在 `:1065` 注明「国际版新号必须先完成地区注册，否则聊天会报 14017」。
   **结论是待补齐项，不是笼统假设**：Hub 当前的 intl 登录链路**未包含地区注册调用**；新注册的国际版账号在补地区前是否可用、以及 14017 的实际触发条件，需要一次真实链路验证（本次只读，未执行）。修补方向是给 Hub 的 poll 路径补 `registration_status`/`submit_region` 等价调用，或改由 WatchDeck 桥在入库后补 region。
