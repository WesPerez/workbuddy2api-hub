# WorkBuddy 部署健康门禁诊断 / Deployment health diagnostics

本候选仅增强健康探测的原因分类和保守拒绝，不声称修复了已经证明的 GIL 或网络根因。
The candidate adds classified, fail-closed health results; it does not claim a proven GIL/network root cause.

## 已知事实 / Observations

- 生产 `f8b3c626b4757f6658df1313237a7da33ab0b831` 已包含 `/healthz` 成功 GET 日志静默；该修复不重复。
  The existing successful-GET logging fix is already live and is not repeated.
- 10-20 至 10-21 05:15 的只读自然样本累计 235 `unhealthy_current` / 498 `current`；旧日志没有 health 原因，不能将其全部归为误报。
  Existing events lack per-probe reasons; 235 rejected / 498 accepted natural updater checks do not establish a single cause or false positives.
- 当前 `daily_enabled()` 为常量 False，busy 读取没有获取账号锁。本候选不修改服务端 busy 或账号逻辑。
  Daily scheduling is constant False; this change does not alter account/busy logic.

## 边界 / Contract

- 单一 monotonic 4 秒判定预算，保留旧探测的完整首次响应预算。只有明确瞬时连接/读超时、reset/refused/disconnect 等才可用剩余预算再尝试一次。
  One monotonic four-second decision budget; the first attempt is not shortened. Only classified transient failures can use the remaining budget for one retry.
- HTTP 状态、过大响应、JSON/对象/字段类型错误、ok=false、commit 不符、真实 busy 都拒绝；不在单次探测内部重试成通过。
  Semantic failures are not retried inside a probe. Missing/invalid fields fail closed.
- current / 新版健康检查校验精确 SHA；预发布 idle 和 drain 后 idle 校验实际旧 runtime 的 SHA，`activeJobs` 必须为非 bool 的非负整数，idle 只接受零。
  Idle probes additionally bind to the actual old runtime revision; non-idle current checks still permit real busy jobs.
- 原部署后的启动轮询与 drain 轮询继续存在并有界；新结果类型只改变诊断，不跳过锁/制品/空闲/drain/回滚。
  Existing bounded deployment/drain loops remain; structured results do not bypass safety gates.
- GET reader 使用独立进程，总预算结束即 kill/reap；没有 timeout 后继续读取的后台线程。进程创建与操作系统回收不能保证硬墙钟上界；超预算结果永不成功，但不声称系统冻结时必在 4 秒内退出。
  An isolated GET reader is killed and reaped on expiry. OS process creation/reaping is not a hard wall-clock promise; results after the decision deadline cannot be accepted.
- URL 只经 stdin 传给 reader；不跟重定向、不使用环境代理，仅继承有限 OS 运行变量。日志白名单为 reason/status/elapsed/attempts/安全 SHA；不输出响应/异常文本/URL/认证/账号。
  Worker stdin carries the configured URL; it never appears in argv/logs. Redirects and inherited proxies are disabled, and diagnostics are allowlisted.

## 验证和发布 / Validation and release

可控 loopback fixture 覆盖正常、错误 SHA/ok/busy/schema/JSON/过大、HTTP 异常、断连恢复和耗尽、延迟 header/read/trickle、完整首次慢响应、日志脱敏，以及 current/idle/drain/启动失败回滚。
Loopback fixtures cover semantic/transport/deadline cases and fail-closed current/idle/drain/rollback paths, without real accounts or upstream requests.

本机仅 AST/diff 静态检查；所有测试和打包在 GitHub-hosted 执行。候选分支不是 mine，不能被正式 updater 接受或自动部署。
Local validation is static only. Candidate-hosted evidence does not authorize automatic deployment.

实际 systemd 调用 `/opt/workbuddy-hub/updater/deploy/update.py`，安装器切换 updater 与 current 后下一次 timer 才使用新代码；必须读回两份身份和自然分类日志。
The actual systemd updater path, not merely the runtime's copy, must be verified after a controlled release.

如果自然日志只支持诊断增强，继续将故障根因标为 unknown；短时没有失败仅支持该窗口，不声称永久健康。
Root cause remains unknown until classified natural evidence supports it; a quiet window is not proof of permanent health.
