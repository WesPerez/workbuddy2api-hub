# Account Hub 国际版接入与验收

Account Hub 持有 GitHub 根会话，沿 WorkBuddy 国际站的官方 GitHub/OneID 页面授权。
已经注册与首次 GitHub 注册使用同一条接入流程。Hub 保存并单独刷新 WorkBuddy
access/refresh token；Account Hub 只保存 `accountRef`、UID、realm 和委托版本。

设置 `WB_RESIN_PLATFORM=AppsGlobal` 后，每个国际版受管 WorkBuddy 账号使用独立
的 `wb-<hash>` API 租约，统一在 AppsGlobal 策略池；不同租约可由 Resin 分配到
同一节点，不承诺独占公网 IP。GitHub 浏览器授权继续使用 Account Hub 管理的登录
出口；`bridgeRoute` 只保留登录路由和 Resin 连接配置，不作为 API 当前租约。
网关连接同时核对平台、节点、公网 IP 和租约代次；CONNECT 409 在请求到达上游前
重新获取代次并仅重试一次。传输失败累计两次上报 Resin，恢复结果用于后续请求；
不确定是否已被上游接收的请求不自动重放。没有可用节点时短暂退避并停止，不能
改为直连或触发账号的 30 分钟熔断。

## 可复用验收

1. 用 Account Hub 的“连接 WorkBuddy”或其 `scripts/workbuddy-authorize.mjs`
   接入，核对预期 GitHub 身份、`realm=intl` 与委托引用。重复打开会对账已有交付。
2. 用受限 bridge 令牌调用 `POST /bridge/v1/routes/reconcile`（可传 `uid`）让
   运行中的 Hub 获取 API 租约；`GET /bridge/v1/accounts` 返回不含凭据的
   `egress`，核对 `platform=AppsGlobal`、`key=wb-*`、代次、IP 和可用状态。
   `/var/lib/workbuddy-hub/resin-leases.json` 是不含凭据的最近回执。

   以下工具另行核验**原登录出口**的资料访问，不代表 API 租约或当前聊天链路；
   不创建第二个账号池、不刷新或保存令牌。

   ```sh
   python3 -B scripts/verify_bridge_accounts.py \
     --uid <预期 WorkBuddy UID> --expected-ref account-hub:<账号编号>
   ```

   两个官方端点 `/console/accounts`、`/v2/plugin/accounts` 均需返回唯一匹配 UID
   且 `areaInfoComplete=true`。工具只输出身份与判据，不输出令牌、个人资料或代理密码。
   `routeSource=persisted_bridge_route` 与 `leaseCreatedAt` 表明核验的是 Hub 保存的
   原登录出口，工具不会重绑租约。若碰到 Hub 正在刷新令牌或协调出口，先等待其完成并
   核对真实租约；一次 401/409 不证明账号本身失效。
3. 网关验收另发一次明确指定模型与输入的小请求。直接向上游发送 HTTP 只能证明
   上游连接；完整 SUB2 验收必须经过已有单账号 exclusive group 的 Key，在 Hub
   仅启用目标 UID、确认并发为空后发送。结束后核对 SUB2 用量账号、Hub 实际 UID、
   HTTP/SSE 终态与用量，并恢复原账号启用状态。未知网络结果不能自动重放。

资料工具不会发送模型请求，资料接口成功不能代替第 3 步。已验收的账号不为整理
文档再次消耗额度。
此前工具的批量 `--ref` / `--chat` 用法已改为明确的 `--uid` / `--expected-ref`；
模型验收统一走运行中的网关，避免独立脚本再启动令牌刷新者或绕开网关选择过程。

## 出口问题的排查顺序

API 请求先查 Hub 的 `egress` 回执与实际 `AppsGlobal/wb-*` 租约；浏览器登录
请求查 Account Hub 保存的平台与实际登录租约。再查 Resin 的
`/api/v1/nodes?platform_id=<ID>&egress_ip=<IP>` 是否包含原节点，最后核对代次和 IP。
管理 API 能读到租约不代表该节点仍在平台可路由集合中。维护平台归属的权威来源是
账号服务；通过其正式 API 操作，不在服务外调用会改写绑定的内部函数。

订阅刷新保留现用的可用节点；本地 `account-session-reserve` 保存仍被会话依赖的
连接配置。删除重复源前先核对替代源和活跃绑定，不能按订阅名称或节点数量判断用途。
代理故障、任务排队和上游挑战分别处理，不能据一次超时清空有效 Cookie。

## 管理会话与并发

原生面板使用 `X-Panel-Token`，不通过 Cookie 自动附带管理凭据，也不从查询串读取。
令牌仅存在进程内，最长有效七天，退出或重启即失效；面板重新登录不会清除
WorkBuddy 账号令牌。机器连接使用受限 bridge 令牌，不依赖面板会话。
模型请求的原生并发上限默认 32，由 `WB_MAX_CONCURRENT_CHAT` 配置；`auto`
按就绪账号扩展槽位。占满返回 503，不能把本地排队拒绝归因为账号或出口失效。

国际版页面、OneID 弹窗和双层 state 的历史依据见
[OAuth 调研](research-20261013/international-oauth-flow.md)，上游变化时以实际官方流程复核。
