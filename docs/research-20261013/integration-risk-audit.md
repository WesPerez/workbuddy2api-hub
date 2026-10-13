# WorkBuddy Hub / Panel 整合风险审计（独立证据核验，只读）

日期：2026-10-13（Asia/Shanghai）
归档说明：下述临时工作副本已随会话清理，路径仅标识当时的核验范围；本文为历史快照，当前接入以 [运行约定](../account-hub-bridge.md) 为准。
核验对象：`/tmp/wb_src/ardeyouxipianyi__workbuddy2api-hub`（下称 **hub**）、`/tmp/wb_src/linguo2625469__workbuddy2api-panel`（下称 **panel**）
状态：纯只读。未安装、未编译、未运行任何项目；未写出任何凭据原文。
方法：源码 + 自带测试 + GitHub 开放 Issue（`gh`，只读查询）。

> hub 为 Python 标准库实现（`wb_proxy.py` 等），panel 为 Go 实现（`internal/*`）。
> 两仓均为“反代 + 面板”形态，但安全模型、协议面、导入导出能力差异很大，不能互相替代。

## 0. 范围与命名澄清

`/tmp/wb_src` 下实际有 7 个克隆：`ardeyouxipianyi__workbuddy2api-hub`、`linguo2625469__workbuddy2api-panel`、`xiaofan6ya__workbuddy2api`、`CangShui__workbuddy-gateway`、`ithtelab__workbuddy-manager`、`88lin__workbuddy-auto-signin`、`L0NE-6__WorkBuddy-Daily`。本次核验范围按要求**只覆盖 hub 与 panel 两仓**。

**矛盾（命名）**：用户此前询问的 “workbuddymanager” 在本目录里对应的是 `ithtelab__workbuddy-manager`，**不是** hub 或 panel。hub/panel 自述均为“网关 + 面板”，而 manager 是另一套东西。后续若把 “manager 持有 refresh token、metapi 走内网桥” 作为结论，需要先确认真正的 manager 指的是哪一个，否则结论会挂错对象。

两仓版本线索：hub `main@4c6fab5`（PR #314 合并）、panel `main@6b31db6`（PR #152 合并）。

## 1. 管理入口安全与默认密码 / API 鉴权

### 1.1 hub：面板密码与 API Key 两套独立凭据

证据：

- 默认面板密码硬编码为 `admin`：`wb_settings.py:20` `DEFAULT_PANEL_PASSWORD = "admin"`。
- 校验逻辑：无存储哈希时直接 `password == "admin"`；显式标记为默认密码时同样接受 `admin`；否则才走 PBKDF2 摘要比较。见 `wb_settings.py:211`–`:230`。摘要算法 PBKDF2-SHA256、12 万轮、每账号目录独立 salt，仅存摘要不存明文：`wb_settings.py:23`、`:124`。
- 默认密码可被程序识别并在状态接口暴露布尔量：`wb_settings.py:204` `panel_password_is_default`，经 `wb_proxy.py:9695` 透出为 `panel_password_default`。**只暴露布尔，不暴露密码本身**。
- 登录接口 `/panel/login` 有按客户端 IP 的限流（60 秒内 5 次）与 0.5 秒失败延迟：`wb_proxy.py:17107`–`:17130`。限流键考虑反代场景，见 `wb_proxy.py:17107` 注释与 `login_rate_limit_key`。
- 管理路由的强制门禁：`/accounts*`、`/usage*`、`/settings*`、`/logs*` 等一律要求已登录面板会话；GET 与 POST 各有一处同样的判定（GET：`wb_proxy.py:15758`；POST：`wb_proxy.py:18302`）。门禁覆盖面定义在 `_is_panel_route`：`wb_proxy.py:15711`。
- 会话 token 只经 `X-Panel-Token` 请求头传递，**刻意不接受 query string**，避免泄进浏览器历史/Referer/反代日志：`wb_proxy.py:15701`–`:15709`。
- API Key 与面板密码相互独立；LAN 模式隐含 `--host 0.0.0.0` 并强制生成 Key：`wb_proxy.py:18637`–`:18638`，`auth_required()` 定义于 `wb_proxy.py:420`。

**弱点（项目自己在 README 中标注）**：`?pwd=<面板密码>` 可免手输直接进入面板，文档同时警告公网暴露时不要使用，因为密码会留在浏览器历史与反代日志里：`README.md:44`。这是便捷性与泄密面的直接取舍。

**判读**：hub 的管理入口是“默认弱密码 + 限流 + 摘要存储 + 头传递会话 + 面板路由强制门禁”。默认密码 `admin` 只有在用户从未改过时才可登录，`panel_password_is_default` 会让面板能提示“仍在使用默认密码”——但**没有任何强制改密或拒绝启动**的逻辑。

### 1.2 panel：单一 api_key 同时管 `/v1/*` 与 `/panel/api/*`

证据：

- 网关与面板共用同一把 `api_key`，常量时间比较：`internal/httpauth/httpauth.go:31` `VerifyBearer`，先 SHA-256 摘要再 `subtle.ConstantTimeCompare`（`httpauth.go:24`–`:38`）。注释明确指出此前两处各自复制了“字符串直接比较”，**且都带计时侧信道**，统一后才有常量时间：`httpauth.go:2`–`:17`。
- **空 key = 完全不鉴权**：`httpauth.go:20`–`:22` 中 `if key == "" { return true }`；面板侧同样口径，`internal/panel/panel.go:199` 注释“api_key 为空时放行”；字段注释 `cmd/server/config.go:21`“空 = 不鉴权”。
- 默认监听 `:7863`，即绑定所有接口：`cmd/server/config.go:236`；`Default()` 的 `APIKey` 为空串：`cmd/server/config.go:237`。
- 首次运行会自动落一份推荐配置，且 `api_key` 用 `crypto/rand` 随机生成（`sk-` 前缀）、以 `O_EXCL` + `0600` 写入：`cmd/server/config.go:343`–`:362`。代码注释直接写明动机：“listen 绑定 0.0.0.0，空 key 会把网关裸暴露给局域网”。
- 但**只覆盖“配置文件不存在”这一条路径**。若用户手写或从别处拷贝了一份 `config.json` 而其中没有 `api_key`（`Load` 以 `Default()` 为底，故为 `""`），网关会在 `0.0.0.0` 上以**无鉴权**状态运行，且启动时不会拒绝：`cmd/server/main.go:54`–`:72` 只在文件缺失时补默认配置，缺失字段不触发拒绝。
- 启动日志仅打印布尔：`cmd/server/main.go:351` `api_key=%v`（`cfg.APIKey != ""`）。**没有**“空 key 且非本机监听”的显式告警或退出——与 hub 的 LAN 强制 Key 形成对比。
- 环境变量会覆盖配置文件：`cmd/server/config.go:381`（`WB2A_API_KEY`）。
- 前端把 key 存在 `localStorage`：`internal/panel/app.js:44`、`:322`、`:1104`、`:1183`。

### 1.3 关键矛盾：panel 开放 Issue #155（仍 OPEN，0 评论）

`gh issue view 155`（2026-10-11 提交，state=OPEN，comments=0）标题：

> [Bug] 修改 config.json 中 api_key 并重启后，面板仍保持旧密钥登录态 / 环境变量 WB2A_API_KEY 可能静默覆盖配置

可见字段与源码对得上：报告者怀疑环境变量静默覆盖配置（对应 `config.go:381`），以及前端仅依赖 `localStorage` 缓存登录态、后端换钥后前端未做一致性校验（对应 `app.js:44`/`:322`）。**核验结论：报的两个疑点中，“环境变量覆盖配置文件”已在源码中确认成立；“换钥后旧会话仍有效”与前端 localStorage 缓存的实现一致，但仓库内没有对应的失效/校验逻辑可查，故只能判“与实现一致、未在源码中看到反证”，不能判为“已复现 Bug”。**

这是本次核验里**唯一与安全直接相关且仍未关闭**的 panel issue。

## 2. 账号导入 / 导出 schema

### 2.1 hub：导入导出都齐全，且导出默认带密钥

- 导出文档构造：`wb_accounts.py:3456` `build_export_document(accounts, realm=None, include_secrets=True, uids=None)`，产出形如 `{format, version, exportedAt, count, accounts:[...]}`（`:3467`–`:3475`）。`include_secrets=False` 时才剔除 `accessToken` / `refreshToken`：`wb_accounts.py:3472`–`:3475`。
- 单条序列化：`wb_accounts.py:3447` `account_to_export`（保留凭据与身份，仅去掉本地 `path`）。
- **HTTP 侧默认值**：`wb_proxy.py:16014` `include_secrets = (query.get("secrets") or ["1"])[0] not in ("0","false","no")` —— 即 `/accounts/export` **默认导出明文 access/refresh token**，需显式 `?secrets=0` 才脱敏。
- 导出接口鉴权：`/accounts/export` 路径以 `/accounts` 开头，落在 `_is_panel_route`（`wb_proxy.py:15711`）内，因此必须持有已登录的面板会话（GET 门禁 `wb_proxy.py:15758`）。**核验结论：默认带密钥导出属实，但接口受面板密码保护，不是未鉴权泄露。**
- 导入接受多种容器：`_coerce_account_rows` 接受导出文档、裸数组、单对象、桌面端凭据：`wb_accounts.py:3477` 起。字段归一化 `normalise_import_row`：`wb_accounts.py:3525`，显式兼容 cockpit 的 snake_case（`access_token`/`refresh_token`/`expires_at`，`expires_at` 为毫秒）并映射到扁平结构（`:3536`–`:3545`）。
- 导入校验：要求存在 accessToken 且必须是 3 段 JWT（`token.count(".") != 2` 即报错）：`wb_accounts.py:3559`–`:3562`。realm 由 token/domain 证据判定，调用方强制值优先，且**忽略行内过期的 realm**（:3564–:3572 注释）。
- 导入有预览与落盘两条路径：`preview_import_rows`（`wb_accounts.py:2491`）、`import_rows`（`:2518`）；HTTP 侧 `/accounts/import`、`/accounts/import/desktop`（`wb_proxy.py:17158`、`:17194`、`:17210`，预览分支 `:17845`）。
- 自带兼容性测试：`tests/_test_cockpit_import.py:1`（“cockpit tools JSON import compatibility”），说明该格式是被明确支持并回归的。

### 2.2 panel：只有导入，没有导出

- 唯一导入接口：`POST /panel/api/import/cockpit`，见 `internal/server/handler.go` 的 `internal/panel/panel.go:160` 路由注册与实现 `internal/panel/import.go:34`。
- schema（`cockpitAccount`，`internal/panel/import.go:17`–`:34`）：`id, email, uid, nickname, access_token, refresh_token, token_type, expires_at, domain, dosage_notify_code, payment_type, status, usage_updated_at, last_checkin_time, checkin_streak, created_at, last_used`。
- 必填校验：`uid`/`access_token`/`refresh_token` 任一为空即跳过该行；`uid` 还需通过 `validImportUID`（只允许 `[A-Za-z0-9_-]`、长度 ≤64，用于拼文件名，防路径穿越）：`internal/panel/import.go:79`–`:90`、`:166`。
- realm 由 `domain` 推断（workbuddy.ai 家族 → global，否则 cn）：`internal/panel/import.go:97`，`auth.ResolveRealm`。
- 时间单位：cockpit 的 `expires_at` 为**毫秒**，此处 `/1000`；≤0 则回退“当前 +365 天”：`internal/panel/import.go:100`–`:104`。**注意**：hub 的 `normalise_import_row` 用 `normalize_epoch()` 也处理毫秒（`wb_accounts.py:3537` 注释），两仓对同一字段的单位理解一致。
- **导入有上游副作用**：导入成功后会按 realm 触发注册激活/Trial/签到，并读资源后 `ReenableIfCredits`：`internal/panel/import.go:130`–`:160`。**核验结论：panel 的导入不是纯本地写盘，会立刻产生上游请求**——对“批量导入多账号”属于限流与封控敏感路径。
- **没有导出接口**：`grep -rn -i export internal/panel/*.go cmd/server/*.go`（排除测试）零命中；面板路由表 `internal/panel/panel.go:150`–`:188` 也没有 export 项。**所以 panel 侧“导出”只能回到上游 cockpit 工具，或改源码**。这与 hub 的对称能力形成明显落差。

## 3. OAuth 刷新是否集中互斥

**核验结论：两仓都是“按账号串行”，都不是全局集中式刷新器。** 没有任何单点持有刷新职责；请求路径各自可以触发刷新，只靠账号级锁避免同账号并发。

hub 证据：

- 每账号一把刷新锁与一把保存锁：`wb_accounts.py:627` `self._refresh_lock`、`:628` `self._save_lock`；另有 `_credits_lock`（`:634`）与 `_throttle_lock`（`:637`），注释说明分别用于网络请求与冷却，刻意分开。
- `refresh()` 在锁内二次检查后执行：`wb_accounts.py:1222`–`:1226`；注释写明原因是**上游会轮换 refresh token，并发刷新会让第二次用已消费的 token**（`:1220`–`:1224`）。
- 刷新成功后写回并落盘：`wb_accounts.py:1263`–`:1271`（`self.refresh_token = data.get("refreshToken") or self.refresh_token`）。

panel 证据：

- 每个 `Auth` 一把互斥锁：`internal/auth/auth.go:24` `mu sync.Mutex`；注释“串行化 RefreshToken 写与 SaveAtomic 读，防止并发写回半更新 token”（`:23`）。
- 锁被显式导出给上游包使用：`internal/auth/auth.go:49`–`:52`。
- `RefreshToken` 的写回策略是“锁外发请求、锁内校验快照未变再写回”，并说明若锁外期间别的 goroutine 已完成刷新则放弃本次写回：`internal/upstream/client.go:905`–`:910`、`:962`–`:966`。

对整合的直接影响（与既有报告第 3 节“更正 C”一致并强化）：**若两个进程同时持有同一账号的 refresh token，存在并发竞态风险**——hub 与 panel 各自都只为“单进程内并发”设计，跨进程没有任何协调锁；一旦发生“读快照 → 对方刷新并写回 → 本地用旧快照整份覆盖”，就可能把对方刚落盘的新 token 盖回旧值。该窗口是否真的命中取决于两侧的实际调用时序与频率，本次静态核验**无法证明它必然发生**，只能证明缺少防御、存在风险（详见 §10）。

## 4. 刷新失败是否转 browser

**核验结论：两仓都不会在刷新失败时自动转浏览器。都表现为“标记失败/禁用，等待人工重新登录”。**

hub 证据：

- 无 refresh token：`_set_last_error("no refresh token; sign in again")` 并返回 False：`wb_accounts.py:1229`–`:1231`。
- 请求异常：`_set_last_error("refresh failed: %s")` 返回 False：`wb_accounts.py:1245`–`:1248`。
- 响应无 token：`_set_last_error("refresh returned no token …")` 返回 False：`wb_accounts.py:1259`–`:1262`。
- 全仓 `relogin/re-login/auto login/browser` 检索只命中两处**文案**（`wb_proxy.py:12036`、`:12070` 的 “until re-login” 提示语），没有浏览器自动化接管。
- hub 确有一套**浏览器登录流程**，但它是人工设备流：面板发起后轮询 `AUTH_TOKEN_PATH`，返回 `pending / "waiting for browser login"`，需用户在自己的浏览器完成——见 `wb_accounts.py:3155`–`:3180`。这与“刷新失败自动换浏览器”是两件事。

panel 证据：

- 刷新响应缺 accessToken → 直接 `"refresh_failed: no accessToken in response — re-login required"`：`internal/upstream/client.go:955`。
- 会话失效文案：`internal/upstream/hint.go:59` “account session expired at upstream; the account is disabled until re-login”。
- **panel 完全不含浏览器自动化**：对 `browser|playwright|chromium|rod` 的全仓 `.go` 检索，命中的全是 `Pnuct`（上游字段名）与无关字符串，没有任何浏览器驱动。
- panel 的登录是 CLI 设备授权流：`cmd/login/main.go:1`–`:16` 注释写明 `login url` → 拿 state+authUrl，`login poll` → 取 token/account；并特别注明 **“无 PKCE（workbuddy 设备流由服务端签发 state）”**。也就是说登录本身要求用户在浏览器打开授权 URL，但**网关不会替用户开浏览器**。

**整合含义**：WorkBuddy 若要做到“刷新失败自动恢复”，浏览器这一环在 hub/panel 里**没有现成实现可抄**；可参照的是本机 WatchDeck 已有的浏览器恢复链（另一份报告已覆盖），而不是这两个仓库。

## 5. 协议兼容测试覆盖实测

### 5.1 panel：只做 OpenAI Chat Completions，**没有 Responses**

- 路由全集只有四条：`POST /v1/chat/completions`、`GET /v1/models`、`GET /status`、`GET /healthz`，外加 `/panel/*`：`internal/server/handler.go:138`–`:143`。
- **没有 `/v1/responses`，也没有 `/v1/messages`**：对 `internal/server/handler.go` 与 `internal/panel/*.go` 检索 `/v1/responses`、`/v1/messages` 均无路由命中（`image_url` 仅出现在请求来源提示的解析里）。
- 与开放 Issue 相互印证：panel #89 “请问WorkBuddy2api没有原生的response支持吗？”（OPEN，2026-09-30）、#48 “不能够直接在codex中使用反代的api吗？”（其中讨论需靠 New API / 自写路由转换）。

已覆盖（Go 单测，均为“转换/流式处理函数”级）：

| 维度 | 覆盖情况 | 证据 |
| --- | --- | --- |
| 流式 | 有，且较细：强制 stream、聚合、帧归一化、首帧 id、`[DONE]` 兜底、空流各分支 | `internal/upstream/sse_test.go:15/78/106/118/158/231/273/315/349/409/454/522/546/562/584/608/676/685` |
| 工具调用 | 有：出站配对三步（merge/repack/cleanup）、tool_choice 三种形态 | `internal/upstream/tool_pairing_test.go:1`；`sse_test.go:24/36/56` |
| 思考 | 有：9 条（深寻/默认 effort/禁用删 effort/非深寻跳过/字符串保留等） | `internal/upstream/thinking_test.go:29/63/106/142/170/209/237/288` |
| 断连/空闲 | 有：上游超时判定、body 空闲切断/活动续期/关闭停 goroutine | `internal/server/upstream_timeout_test.go:20`；`internal/upstream/idle_test.go:11/23/39/54` |
| Responses | 无（接口不存在） | 见上 |
| 图片/多模态 | **无**：`grep -rln -i 'image_url\|input_image\|vision\|multimodal' --include='*_test.go'` 零命中 | — |
| 面板安全响应头 | 有：CSP / nosniff / X-Frame-Options / Referrer-Policy 覆盖页面、静态资源与 401 | `internal/panel/security_test.go:16` |

### 5.2 hub：三种协议都做，Responses 有转换级回归

- 路由同时支持 `/v1/chat/completions`、`/v1/responses`（`wb_proxy.py:18318`、`:18368`）与 `/v1/messages` + `/v1/messages/count_tokens`（`wb_proxy.py:16395`、`:18305`）。
- Responses 相关回归（确定性、离线）：
  - `tests/_test_custom_tools.py:1` “Deterministic protocol-conversion tests for the custom-tool port”，断言 Codex 依赖的 Responses 形态：自定义工具降级为 function、`custom_tool_call` / `custom_tool_call_output` 历史存活、命名空间工具映射、流式工具片段（`:37/56/75/85/201/244/279`）。
  - `tests/_test_namespace_tools.py`、`tests/_test_max_output_tokens.py:11/15/18/24`（`responses_to_chat` 的 `max_output_tokens` 默认与显式值）、`tests/_test_orphan_function_call_output.py:7`（注释记录真实故障：“Desktop: every /v1/responses retry died as a bare 502”）、`tests/_test_local_web_tools.py:211`（构造 `self.path = "/v1/responses"`）。
- Anthropic/思考/流式事件序：`tests/_test_anthropic_messages.py:1`（标题即“translation and streaming regression tests”），含流式文本事件序（`:149`）、流式工具事件序（`:169`）、流式错误事件（`:189`）、流式思考事件（`:197`）；HTTP 面 `tests/_test_anthropic_http.py:333` 断言原生事件序列。
- 图片：转换代码存在（`wb_proxy.py:13050`–`:13059`、`:13666`、`:13921`、`:13959`、`:14109`），但**没有专门的图片/多模态测试**；测试里唯一触及 `image_url` 的是 Anthropic 路径 `tests/_test_anthropic_messages.py:95`。

覆盖缺口（hub）：

- **没有端到端驱动 `/v1/responses` 的 SSE 流式测试**。现有测试都是转换函数级（`responses_to_chat` 等）或 Anthropic 路径的流式事件序，未见到“以 `/v1/responses` 为入口、消费真实 SSE 帧序列”的用例。
- **客户端中途断连没有集成级覆盖**。`client_aborted` 被当作终态采集（`wb_proxy.py:673`、`:977`），且明确**对 aborted 不重试**（`:3939`、`:3954`、`:4688`、`:6311`、`:7675`）；但测试只在用量分析聚合层造 `client_aborted` 行（`tests/_test_analytics_hour_buckets.py:194`、`tests/_test_perf_incremental.py:164`），**没有在代理入口制造真实半途断连**。

## 6. 开放 Issue 中的近期致命故障

计数：hub 开放 12 条；panel 开放 53 条（`gh issue list --state open`，2026-10-13）。

hub 近期高关注项：

| # | 日期 | 标题要点 | 风险 |
| --- | --- | --- | --- |
| 311 | 10-12 | 面板登录 `Failed to fetch`；反代 503「该区域没有任何账号」，但账号未封、直连正常 | **可用性**：面板与出口同时不可用，用户无法自助 |
| 316 | 10-12 | 要求把熔断从“整账号”改为“单模型”；维护者在帖内承认 429 走单模型冷却、30 分钟熔断来自**连续 3 次硬失败**（网络断连会误伤） | 调度语义，非致命 |
| 313 | 10-12 | 同一出口 IP 挂 12 个国际版账号，实测墙高约 13–19 请求/分钟；另有贡献者给出反例（>29/min 持续 1 小时无 429） | **上游风控形态未定**，两方口径有冲突 |
| 304 | 10-12 | v1.6.17 设置 300 不调度，仍被干到 0 | 余额耗尽，需核 |
| 252 | 10-10 | Codex 使用中突然报错 | 未定位 |
| 322/323 | 10-13 | 国际版额度为 0 后是否还能拿积分；请求新增反代目标 | 功能诉求 |

panel 近期高关注项：

| # | 日期 | 标题要点 | 风险 |
| --- | --- | --- | --- |
| 155 | 10-11 | `api_key` 改后不生效 / 环境变量静默覆盖（见 §1.3） | **安全，仍 OPEN** |
| 163 | 10-12 | 一键完成任务与任务中心全部失效 | 每日积分链路直接受损 |
| 160 | 10-12 | 「过期积分优先」策略疑似失效 | 积分浪费 |
| 154 | 10-11 | 积分总额与剩余显示错误 | 数据正确性 |
| 149 | 10-10 | 会话总是莫名中断 | 与 §5.2 断连缺口呼应 |
| 81 | 09-29 | 历史中重复 `tool_call id` → 上游 400/11148 → 配对清理放行后**整条会话持续 503**。帖内已定位到 `cleanupOrphanToolCalls` 用全局 id 集合，**同 id 复用时两侧都被判已配对**，落在安全网盲区 | **协议级致命**：单会话彻底无法继续，仍 OPEN |
| 43 | 09-22 | global 域模型目录漏掉编码代理面模型（`/v3/config` 按 UA 下发不同目录，探测只用了 IDE UA） | 模型可用性 |
| 48 / 89 | 09-23 / 09-30 | 无原生 Responses 支持；需 New API 中转 | **协议面**，与 §5.1 一致 |
| 49 | 09-23 | 标题「南山必胜客发力了」，评论区大量“上游仓库消失/逃难到这里”叙述 | **项目延续性/法律风险**，非技术故障 |

**判读**：没有发现“近期新引入、导致全站不可用”的致命回归。最需要警惕的两条是 panel #81（协议级，且根因已定位但未修）与 panel #155（安全，与源码实现一致）。#49 类叙述提示该领域项目存在被上游/法务施压下线的历史，属于选型层面的持续风险而非代码缺陷。

## 7. 矛盾汇总

1. **命名矛盾**：用户口中的 “workbuddymanager” 对应 `ithtelab__workbuddy-manager`，不是本次核验的 hub/panel。“manager 持钥”这一结论目前没有落在这两仓上（§0）。
2. **默认配置安全性矛盾**：panel 在“配置文件缺失”时会生成随机 Key（`config.go:343`），但在“配置文件存在但缺 `api_key`”时静默无鉴权（`config.go:237` + `main.go:54`）。同一项目里“安全默认”与“不设防默认”并存，取决于文件是否存在。
3. **测试与实现的自洽性矛盾**：panel 有完善的安全响应头测试（`security_test.go:16`），却支持“空 key = 无鉴权”（`httpauth.go:20`）——安全头到位、鉴权可为零。
4. **上游风控形态矛盾**：hub #313 中维护者与贡献者给出方向相反的实测（约 13–19 req/min 撞墙 vs >29/min 持续 1 小时不撞），且贡献者指出“终局 429 不能用来判断墙”——被重试吸收的 429 不留终局记录。此矛盾未解决（§6）。
5. **导入语义矛盾**：hub 有预览路径（`preview_import_rows`）与导出能力（默认带密钥）；panel 的导入**无预览、无导出、且立即产生上游请求**（`import.go:130`）。两仓同叫“导入”，副作用强度不同。

## 8. 未知与未核验

1. **panel 的 `cmd/login` 是否有官方支持的完成路径**：已知是无 PKCE 的设备流（`cmd/login/main.go:6`–`:16`），但“上游是否接受自动化完成授权”未从源码判定。
2. **hub 面板会话的 cookie/CSRF 细节**：已确认 token 走 `X-Panel-Token` 头（`wb_proxy.py:15701`），但 `PanelSessions`（`wb_settings.py:1647`）的 TTL、单点登出、并发会话上限未逐行核验。
3. **hub `/health` 的字段暴露面**：`do_GET` 中 `/health` 未在 `_is_panel_route` 名单内（`wb_proxy.py:15711`），启动器还依赖它做重复实例探测（`wb_proxy.py:18664` 附近），但本次未确认它在公网暴露时具体吐出哪些账号级字段。
4. **panel #155 是否可复现**：源码支持“env 覆盖 config”这一半，另一半（旧会话继续有效）与前端 localStorage 实现一致，但无仓库内失效逻辑可查，故未判定为已复现。
5. **Responses 端到端流式**：hub 有此路由与转换测试，但未见“真 SSE 端到端”用例；panel 无该路由。两者都未覆盖 `/v1/responses` 的断连/工具/图片组合路径。
6. **open Issue 的“近期致命故障”只能按标题+正文判读**：`gh` 只读查询未逐一复现，故 §6 的严重度是文本判读，不是复现结论。

## 9. 对整合方案的直接影响

- **管理入口**：panel 若被采用，必须先确认 `api_key` 非空且监听不落在 `0.0.0.0` 的裸奔组合；hub 则必须改掉默认 `admin`（`wb_settings.py:20`）并避免 `?pwd=`（`README.md:44`）。两者的门禁都够用，默认值都不够用。
- **幂等导入**：hub 有预览+realm 证据判定（可作幂等键的参考实现），panel 导入会立刻打上游且无导出回读。这与既有报告第 3 节“更正 D：provider+region+officialUserID”一致——重复导入在 panel 上代价更高。
- **单一所有者**：§3 证实两仓都只做单进程内账号级互斥、无跨进程协调，因此跨进程共享 refresh token 有并发竞态风险，强化“更正 C：MetAPI 走 manager 内网桥、不持副本”。
- **浏览器恢复**：§4 证实这两个仓库都没有刷新失败转浏览器的现成能力；该能力只能来自本机 WatchDeck 的既有链路。

## 10. Manager 窄范围加验（ithtelab/workbuddy-manager，main = `1695ec1` / v1.0.85）

核验对象：`/tmp/wb_src/ithtelab__workbuddy-manager`；对照 `/tmp/wb_research/project-comparison.md` §8。
仍为只读：未安装、未运行、未触真实账号。

### 10.1 「不复制 token、也不走真人面板导入」的 auto OAuth 链路：**存在**，但它不是 GitHub

**存在性证据（管理 API 层）**：

- `POST /api/auth/start` 的依赖是 `security.require_admin`：`server/routers/accounts.py:503`–`:515`。
- `GET /api/auth/poll` 的依赖同样是 `security.require_admin`：`server/routers/accounts.py:569`–`:575`。
- 两者都接受 **admin scope 的 API Token**（token 鉴权路径见 `server/security.py:509`–`:521`；`require_admin` 定义在 `:515`）。
- **账号落盘由 manager 自己完成，不经过 upload**：`auth_poll` 命中 ready 后调用 `_save_and_finish`（`server/routers/accounts.py:679` 附近）→ `tencent.write_auth_file`（`server/routers/accounts.py:825`–`:846`；写盘实现 `server/services/tencent.py:333`–`:400`）。
- 对照：唯一需要真人会话的导入端点是 `POST /api/accounts/upload`，依赖 `security.require_session_admin`：`server/routers/accounts.py:146`–`:151`；其定义明确「拒绝 API Token」：`server/security.py:521`–`:533`。
- 甚至还有一条**服务端自跑的轮询**：`_poll_login_background`（`server/routers/accounts.py:682`–`:712`）复用 `auth_poll` 跑到终态并缓存，前端只负责读——即调用方不需要维持一个前台轮询循环。

**结论**：从「必须真人面板导入」这一限制看，链路确实可绕过——**admin token 全程足够，账号由 manager 的 poll 路径自己写盘**，无需复制 access/refresh token，也无需 `require_session_admin`。

**但存在一处关键矛盾：授权动作是微信/QQ 扫码，不是 GitHub。**

- `start_login` 打的是腾讯端点 `/v2/plugin/auth/state?platform=CLI`，返回 `{state, authUrl}`：`server/services/tencent.py:116`–`:131`。
- 前端把 `authUrl` 渲染成二维码（`web/components/common/accounts/AddAccountDialog.tsx:4` 引入 `QRCodeSVG`）。
- 使用文档原文：「用微信 / QQ 扫码 → 授权成功后自动签到、落盘并重载上游容器」：`docs/usage.md:8`；README 同口径（`README.md:42`、`:70`、`:165`）。

→ 你设想的「WatchDeck 发 auth/start → 用已有 **GitHub** 会话授权 → manager poll 自动入库」中，**前段与后段成立，中间那段不成立**：这里没有 GitHub OAuth，需要的是腾讯/微信侧的真人扫码（或等价的腾讯登录态）。若 WatchDeck 侧只有 GitHub 会话，它无法替用户完成这一步。

### 10.2 单一刷新所有者：Go 的 on-demand 刷新**保留**后，禁用 manager renew **不能**覆盖全部账号

**Go 上游确有内置刷新，但没有对外接口，且只在三个内部时机触发**（manager 源码里的一手描述，`server/services/tencent.py:405`–`:433`）：

> 上游 workbuddy2api **有**刷新能力（`internal/upstream/client.go` 的 `RefreshToken`），但它**没有任何对外接口**——其路由表里只有 `/v1/chat/completions`、`/v1/models`、`/status`、`/v1/stats` 与三个 `/admin/accounts/...`（后者还默认关闭）。刷新只发生在三个**内部**时机：保活排程（`schedule.keepalive_hours`，默认每天 22 点一次）；每次 chat 选号后、token 距到期不足 `RefreshSkew`（默认 10 分钟）时；签到前 token 临近过期时。

**因此「禁 manager renew，仍能刷所有直连 API」不成立**：每个触发点都要求「这个账号当刻真的被选中使用」。`renew.py` 的模块说明把缺口写得很直白（`server/services/renew.py:1`–`:45`）——账号长期闲置无对话流量、上游容器停机或调度器没跑、保活时刻网络抖动，这三种情况下 token 会一路走到过期而无人续期。这正是该模块存在的理由（issue #40）。

**阈值差异（决定覆盖度）**：

| 一侧 | 触发条件 | 阈值 | 位置 |
| --- | --- | --- | --- |
| Go 核心 | chat 选号后 / 签到前 / 保活 | 距到期 10 分钟（`RefreshSkew`） | `server/services/tencent.py:420`（转述上游） |
| manager renew | 每小时巡检，仅看寿命 | 距到期 3 天（`_RENEW_BEFORE_SECONDS`） | `server/services/renew.py:64`、`:67`、`:76`–`:81` |

→ 若禁掉 manager renew，只有「在到期前 10 分钟内恰好被选中」的账号会被 Go 续上；闲置账号会过期，需要重新扫码。**这是覆盖度回退，不是等价替换。**

### 10.3 竞态的本质与「少改且能占竞态」的设计

**竞态的本质（两个独立进程各写同一份账号文件，无跨进程锁）**：

- manager 侧写入：`_atomic_write_json`（`server/services/tencent.py:220`–`:268`）——`mkstemp` 唯一临时名 + `fsync` + `os.chown/chmod` + `os.replace`。注释说明上游自 2026-09-18 起每 5 秒按「文件名 + mtime + 大小」热加载，写入必须原子，否则 0 长度窗口会被判成账号被删。
- manager 续期是 **read-merge-write**：`update_auth_tokens`（`server/services/tencent.py:272`–`:315`）先整份读、只改 `auth.*` 几个键、再整份原子替换。
- **关键**：`_iter_accounts` → `read_account_file_any` 读快照（`server/services/renew.py:100`–`:118`、`:136`）与最终 `os.replace` 之间隔着一次网络往返（`tencent.refresh_token`，`renew.py:154`）。这个窗口里 Go 可以完成一次「选号 → 刷新 → 写回」。
- 两个 `os.replace` 各自原子，但**互不感知**，因此是「最后写入者获胜」，不是互斥。

**本次能否证明它必然发生？不能。** 只能证明：缺少防御、窗口真实存在（与 `/tmp/wb_research/project-comparison.md` §8.5 的判断一致，那里也把它列为「待实测确认的竞态」）。是否命中取决于两侧实际调用频率与时序。

**推荐设计（改动最小）：保留双侧能力，但在 manager 写回前加 CAS。**

做法：让 `renew_once` 把「自己这次消费掉的 refreshToken 原值」传给 `update_auth_tokens`；后者在 `_atomic_write_json` 之前**重读一次磁盘**，若 `auth.refreshToken` 已不等于那个原值，说明这一轮期间已有别人（Go）完成刷新并写回，于是**放弃本次写入**（新 token 由对方那份覆盖，manager 的 refresh 结果直接丢弃）。

- 落点：`server/services/renew.py:154`–`:161`（传参）+ `server/services/tencent.py:272`–`:315`（写前复核）。
- 为什么选这条：它是**单边改动**，不需要给 Go 打补丁、不需要新增接口、不动发布流程；把竞态窗口从「一次网络往返（秒级）」压缩到「重读到 replace 之间的毫秒级」，并且**失败方丢弃而非覆盖**，因此不会再把对方的新 token 盖回旧值。
- 残留风险（必须写在方案里）：重读与 `os.replace` 之间仍有理论 TOCTOU 缝隙，CAS 只是**降低**而非**根除**。要根除只能收敛成单写者。

**备选设计（真单写者，改动更大）：** 禁用 manager renew，让 Go 成为唯一写者，但要补上闲置账号覆盖——需要给 Go 打补丁（暴露一个 admin 刷新端点，或调大 `RefreshSkew`）。manager **已具备** patch 上游的机制：`dev/upstream-patches/0001-tool-result-pairing.patch`、`dev/upstream-patches/0002-listing-compat.patch` 与 `dev/pack_fixed_upstream_src.py`、`docs/upstream-tool-history-fix.md`；代价是要重建 Go 核心并刷新 `upstream-src` 资产（`.github/workflows/release.yml`），属于发布流程变更，不符合「少改」。

**不成立的备选：** 「manager renew 改为只触发、不写」——因为 Go 没有对外刷新接口（§10.2 引文），manager 无从触发，只能自己打腾讯端点，等于回到现状。

### 10.4 #158（namespace 回程）缺字段：**证实**，且当前测试捕不到

issue #158 标题（`gh issue view 158`，state=**OPEN**，created 2026-10-11，updated 10-12，2 条评论）：

> [Bug] 命名空间工具回程丢失 namespace 字段——Codex 端浏览器 / computer use / open_in_codex / multi-agent 全部报 unsupported call

源码逐点证实：

1. 展开（出站）有 namespace 分支：`server/routers/responses.py:106` `if kind == 'namespace':`（递归 `_walk`）。
2. 但回程映射只存两个值：`server/routers/responses.py:121` `self._reverse[chat_name] = (name, restored_kind)` —— **没有 namespace**。
3. 还原函数只返回两个值：`server/routers/responses.py:166`（`restore` 返回 `(name, kind)`）。
4. 回程构造项没有 namespace 参数：`_function_call_item(call_id, item_id, name, arguments)` 定义在 `server/routers/responses.py:721`–`:733`；`_custom_call_item` 同在 `:735` 附近，同样无 namespace。
5. 全仓 `namespace` 在 `server/` 下只有两处命中（`:79` docstring、`:106` 展开分支）——**除展开外没有任何写出**。
6. 流式路径同样缺：`server/routers/responses.py:1037` `restored_name, _kind = self.bridge.restore(state['name'])`，取出的 `_kind` 被丢弃，更没有 namespace。
7. 两条出口的调用点：`server/routers/responses.py:809`（非流式）与 `:1037`（流式）。

**为什么测试没抓到**：`server/tests/test_namespace_compat.py` 只断言还原后的 `item['name']` 与 `item['type']`（`:150`–`:155`、`:166`–`:190`），**全程没有断言 `item['namespace']`**。因此「名字还原正确」与「namespace 字段存在」是两件事，测试只钉住了前者。

### 10.5 落实前必须修补的清单

| # | 修复项 | 位置 | 判据 |
| --- | --- | --- | --- |
| 1 | `_reverse` 增存 namespace（或改存 `(name, kind, namespace)`） | `server/routers/responses.py:121` | 回程能拿到原 namespace |
| 2 | `restore` 返回值扩展到含 namespace | `server/routers/responses.py:166` | 调用方不再丢弃 |
| 3 | `_function_call_item` / `_custom_call_item` 增加 namespace 参数并写入 item | `server/routers/responses.py:721`、`:735` | 非流式回程带 namespace |
| 4 | 流式 done 事件同步带 namespace | `server/routers/responses.py:809`、`:1037` | 两条出口字段一致 |
| 5 | 补断言：还原后 `item['namespace']` 等于客户端原值 | `server/tests/test_namespace_compat.py:150`、`:166`、`:210` | 覆盖非流式 + 流式 + 嵌套 namespace |
| 6 | 核清 issue #158 第二点：工具结果图片顺序 | `server/routers/responses.py` 中 `pending_tool_images` **不存在**（全仓仅 `server/routers/anthropic.py:367/442/453` 有 `tool_images`） | 需确认该诉求是「responses 路由也要做工具结果图片提升」，还是记错了路由 |

注：第 6 项与 8.1 节判断一致——`pending_tool_images` 在 responses 路由里没有对应实现，issue 的第二点指向的是 Anthropic 路由已有的机制。这是**待与维护者/报告人澄清**的点，不宜直接按「改 responses」执行。

## 11. Manager 配套 Go core 加验（`/tmp/workbuddy-research-root-20261013/manager-core`，208 个 .go）

来源：manager Release 的 `upstream-src` 预发布资产（`UPSTREAM-SRC.txt` 自述：原仓 `Sliverkiss/workbuddy2api` 已被作者删除，源码由 manager 维护者留存继续维护，MIT）。
本次仅静态阅读源码，未编译、未运行、未触真实账号。

### 11.1 校验状态：**哈希无法在本机复核**

调用方给出的资产 sha256 为 `68c08d13…c036d`、commit `47d0c39`。但 `/tmp/workbuddy-research-root-20261013/manager-core` 是**已解包目录**，本次未找到对应的 `.tar.gz`/`.tgz`/`.zip`，因此**无法重算该 sha256**，也不能从目录内证实 commit `47d0c39`——`grep -rn '47d0c39'` 零命中，且目录内无 `.git` 与版本标记。
可佐证的只有 `UPSTREAM-SRC.txt`：打包时间 2026-10-10 19:12:40，包含模型清单 Magpie 字段（#156）、工具结果配对与 11148 请求级返回（#154）、环境变量代理（#136）、脚本 UTF-8（#147）。**结论：真实性依赖调用方的下载记录；本次只能核对内容自洽性，不能独立背书哈希。**

### 11.2 是否有 HTTP 刷新端点：**没有**（证实 manager 转述）

核心路由表是完整列举，无刷新端点（`internal/server/handler.go:105`–`:121`）：

```
POST /v1/chat/completions      GET /v1/models      GET /status
GET  /v1/stats                 POST /v1/stats/reset      GET /healthz
POST /admin/accounts/{uid}/disable
POST /admin/accounts/{uid}/enable
POST /admin/accounts/{uid}/revive      （三者仅 admin.enabled=true 时注册）
```

没有任何 `refresh` / `renew` 端点。刷新实现只存在于内部函数 `Client.RefreshToken`（`internal/upstream/client.go:960`），调用点仅三处：

- chat 选号后按需：`internal/server/handler.go:713` `if acct.NeedsRefresh(h.cfg.RefreshSkew)`（`RefreshSkew` 默认 10 分钟，`:98`–`:99`）。
- 签到前按需：`internal/scheduler/scheduler.go:452`、`:463`（`checkinRefreshSkew` = 10 分钟，`:105`）。
- 保活排程：`internal/scheduler/scheduler.go:791` `RunKeepaliveNow()`。

**这证实了 manager 源码里的转述**（`server/services/tencent.py:409`–`:433`）：Go 有刷新能力、无对外接口。因此 manager 若想吃 Go 的刷新，**没有可供调用的 HTTP 面**。

### 11.3 是否有跨进程锁：**没有**（与 manager 侧一致）

- Go 侧锁是**进程内、按账号**的 `sync.Mutex`：`internal/auth/auth.go:22`（`mu` 串行化 RefreshToken 写与 SaveAtomic），`:48`–`:52`（`Lock/Unlock` 供 upstream 包复用）。注释给的是同进程内 `go test -race` 实测的数据竞争场景（`AccessTokenValue` docstring，`:55`–`:71`）。
- 全仓 `flock` / `Flock` / `syscall.LOCK` / `F_SETLK` **零命中**——没有任何跨进程文件锁。
- 写回是「临时文件 + rename」，但**不做读回校验**：`SaveAtomic`（`internal/auth/auth.go:284`–`:327`）直接 `os.WriteFile(tmp, raw, 0o600)` + `os.Rename`，**没有和磁盘现值比对**。
- 唯一**进程内**的 CAS 在 `RefreshToken` 第 2 段：写回前检查 `a.AccessToken != atBefore && a.RefreshToken != rtSnapshot`，命中则放弃本次写回（`internal/upstream/client.go:1020`–`:1030`）。**这层防护只覆盖同一进程内的两个 goroutine，对 manager 进程完全不可见。**

**结论：两侧（manager 与 Go core）都只有进程内锁，彼此之间没有任何协调；Go 侧也没有“写前重读比对”这道防线。** 这与 §10.3 描述的窗口完全吻合——但同样**无法证明必然发生**，只是确认了缺少防御。

### 11.4 修正 §10.2 的覆盖度结论：Go core **有**每日全账号批量刷新

§10.2 只按 manager 的转述（「保活排程默认每天只有 22 点一次」）推断覆盖度，**低估了 Go 的保活**。核对源码后更正：

- 保活是**全账号、无条件**刷新：`RunKeepaliveNow` 遍历 `Pool.List()`，只跳过 `Disabled` 与无 refreshToken 的账号，**不做 `NeedsRefresh` 判定**（`internal/scheduler/scheduler.go:791`–`:815`）。
- 每个成功账号都会 `BackfillRealm()` 后 `SaveAtomic()` 写回。
- 它由调度器定时触发：`dispatch` 中 `taskKeepalive` → `RunKeepaliveNow()`（`internal/scheduler/scheduler.go:396`），排程为 `KeepaliveHours`，默认 `[22]`（`:87`–`:89`、`config.example.json` 的 `keepalive_hours: [22]`），开关 `keepalive_enabled` 默认 `true`。
- README 自述同一口径：「**token 保活**（22 点）— 全账号刷新 token，session 失效连续 3 次才禁用」（`README.md:94`）。

**因此更正后的判断**：禁用 manager renew 后，Go core 仍会**每天 22 点对全部非禁用账号刷新一次**（外加 chat/签到时的按需刷新）。由于实测 accessToken 有效期约 60 天（manager 侧 `tencent.py:429` 附近转述，Go 侧 `refreshTokenExpiresInMax` 注释亦记「实测 R-D 响应恒 5184000=60d」，`internal/upstream/client.go:944`–`:947`），**只要容器与调度器在跑、保活未被显式关闭，闲置账号在正常情况下不会被放到过期**。

**残留缺口（比先前判断窄得多）**：

1. `schedule.keepalive_enabled=false` 被显式关闭 → 全账号批量刷新消失，只剩按需。
2. 容器/调度器在保活窗口持续不可用（或那一小时本身故障且跨过下一次窗口）→ 该轮缺失。
3. 短寿命 token（manager 记录「短的可到几天」）叠加上述任一情形 → 可能真的过期。
4. **保活不续 manager 侧读到的那个 realm 之外的账号**——分组目录会遍历到（`RunKeepaliveNow` 用 `Pool.List()`），但 `.disabled` 后缀/`disabled_by_panel` 的账号在 manager 侧被跳过（`renew.py:127`），两侧口径需按部署确认。

**修正后的设计建议**：§10.3 的「manager 写前 CAS」方案依然成立，但**优先级下降**——若确认保活开启且容器常驻，更简单的路径是**关掉 manager renew（或把它改成只做监控与告警），让 Go 保活成为唯一批量写者**，同时保留 Go 的按需刷新。这样天然是单写者，不需要 CAS，也不需要给 Go 打补丁。**代价是**：失去「距到期 3 天」这层提前量，缺少保活故障那一轮的兜底；若这段兜底对你有价值，再回落 §10.3 的 CAS 方案。

### 11.5 对「单一刷新主控」的选型结论

| 方案 | 改动量 | 竞态 | 覆盖度 | 结论 |
| --- | --- | --- | --- | --- |
| A. 关 manager renew，留 Go 保活 + 按需 | 最小（配置级） | 单写者，无竞态 | 每日全账号 + 按需；关保活或停机时缺兜底 | **首选**，前提是保活开启且容器常驻 |
| B. 保留双侧 + manager 写前 CAS | 中（改 2 处 manager 文件） | 降到毫秒级 TOCTOU，未根除 | 保留「距到期 3 天」提前量 | 备选；需要额外提前量时用 |
| C. 给 Go 加 HTTP 刷新端点后由 manager 调到单写者 | 大（打 Go 补丁 + 重发 `upstream-src`） | 单写者 | 可控 | 不符合「少改」 |

**判读**：A 与 B 都不需要碰 Go 二进制。A 更省事但放弃提前量；B 保留能力但要接受残留 TOCTOU。**推荐 A 作为默认，B 作为「需要 3 天提前量或有保活缺口」时的升级路径**；C 仅在必须把刷新做成可编排的集中服务时才考虑。
