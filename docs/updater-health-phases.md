# 健康门禁阶段诊断 / Health gate milestones

本变更仅增强客户端可观测性，不改变业务健康接口、账号、浏览器、凭据、部署锁、drain、回滚或空闲门禁。保留 4 秒**整个探测**预算；只有明确瞬时传输错误且尚余预算时至多再尝试一次。HTTP、结构、身份、`ok=false`、真实 busy 不重试成成功。测试仅使用 hosted 合成 fixture，不请求真实上游。

Client-only diagnostics preserve the total four-second budget, at most two transient transport attempts, exact identity and idle checks. No account, browser, credential, server health handler, deployment lock, drain or rollback behavior is changed. Validation uses hosted synthetic fixtures only.

## 有界事件 / Bounded events

每次原有 timer 事件只增加 `last_phase`、`phase_offsets_ms`、可选 `timeout_source`，不新增每阶段 journal 行。offset 单位毫秒，从**最后一次尝试**开始，非整个多尝试探测；`attempts`/`attempt_reasons` 保留总次数和结果。`elapsed_ms` 仍为整个探测含终止/回收开销，不是服务端耗时。

Existing timer events gain only `last_phase`, `phase_offsets_ms`, and optional `timeout_source`; no per-phase journal entries are emitted. Millisecond offsets describe the **last attempt**, while `attempts` and `attempt_reasons` describe all attempts. Overall elapsed time includes worker termination/reaping, not just server time.

| 阶段 / Milestone | 已观察事实 / Meaning |
|---|---|
| `attempt_start` | 父进程准备启动 worker；未观察 boot 包含解释器/调度延迟。Parent begins attempt; no boot observation leaves interpreter/scheduling unknown. |
| `worker_boot` | 解释器已执行轻量入口，尚未加载其余标准库/读取 stdin。Entry reached before heavier imports and stdin read. |
| `worker_ready` | import 与参数读取完成，准备构造 opener。Imports/input complete, before opener construction. |
| `connect_start` | 开始 stdlib connector（含 DNS/TCP）。Before stdlib connection including DNS/TCP. |
| `tcp_connected` | 该连接原有 connector 返回 socket；不是服务端 accept。TCP socket returned, not proof of server accept. |
| `connection_ready` | HTTP connect 或 HTTPS 默认验证的 TLS 完成。Connection and, for HTTPS, unchanged verified TLS completed. |
| `request_sent` | stdlib request 返回；仅本地 socket 已发送，不证明服务端读取。Request sent locally, not proof of server read. |
| `headers_received` | stdlib getresponse 完成状态行/headers；只留受限 status。Status/headers received, status integer only. |
| `body_complete` | 有界 read 返回；JSON/结构/身份判断可能尚未完成。Bounded read returned, before JSON/identity checks. |

`timeout_source=parent_deadline` 是父进程总截止，`socket_timeout` 是实际 socket 路径超时。旧 `connection_timeout` 仍是响应头之前的宽泛兼容类别，**不等于 TCP 故障**；新的阶段才区分已观察边界。`headers_received` 后的 200 超时仍拒绝，已观察非 200 保持 HTTP 失败且不重试。

`parent_deadline` identifies the owner's overall cutoff; `socket_timeout` identifies the socket path. The compatible broad `connection_timeout` reason is **not a TCP diagnosis**. Observed non-200 status is never retried; a 200 header alone cannot pass the gate.

worker 使用私有 pipe 发送固定阶段与系统单调时钟，最多固定九个 marker，无 URL、header、body、异常文本、账号、Cookie、token 或环境值。父进程拒绝超长、未知/倒序/未来/异常类型的 marker；在 kill/reap 后只采纳不晚于超时观察的阶段。缺失事件仍是未知，不能证明请求未到达或排除服务端阻塞。HTTPS 默认验证、禁代理环境、禁重定向均保留。

The private worker pipe carries only a fixed set of milestones and monotonic timestamps. Bounded parsing rejects invalid types, out-of-order/future/unknown markers and oversized output. No URL, headers, body, exceptions, credentials or environment values are logged. Missing events remain unknown. Default TLS verification, disabled proxy inheritance, and no redirects remain intact.

## 验证边界 / Evidence boundary

loopback HTTP fixtures 覆盖正常完整顺序、延迟 headers/body、瞬断恢复、错误结构/身份/busy、总截止；无网络 hook fixture 覆盖 TLS/TCP 顺序与默认验证，worker 回收/启动前超时、已观察 HTTP 错误、畸形/缺失/晚到 marker、输出上限/脱敏。既有 current、idle、drain 循环、回滚 fixture 不删除。生产仍需自然 timer 样本证明分类可见；这份说明不声明最终 CI、部署或真实根因已修。

Loopback fixtures cover ordered success, delayed headers/body, transient recovery, semantic failures and deadlines. No-network hook fixtures cover TCP/TLS ordering, secure defaults, worker reaping, malformed/missing/late markers and redaction. Existing current/idle/drain/rollback gates remain. Natural production samples are still required; this document does not assert CI success, deployment or a repaired root cause.
