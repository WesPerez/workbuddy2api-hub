# mine 分支维护 / Maintaining mine

`main` 跟随 `ardeyouxipianyi/workbuddy2api-hub:main`；`mine` 以最新上游为父链，
仅把仍有价值的个性化修改按职责追加成提交。上游已覆盖或实现更好的功能直接采用上游，
不为保留旧提交而重复实现。修复在最终验收前折叠进对应职责。

`main` tracks upstream. `mine` appends a small set of responsibility-based commits
to that history. Prefer upstream whenever it supersedes a customization; fold
review fixes into their owning topic before final acceptance.

## 当前职责 / Current responsibilities

1. **网关与账号 / Gateway and accounts**：Account Hub 委托登录、取消、令牌唯一写入；
   令牌与路由独立 CAS；Resin 受管出口、代际恢复、禁止直连回落；独立能力端点；
   实时模型目录与最后成功快照、实际响应可用性、空响应和推理用量判定；国际自动任务停用。
   Delegation, single credential ownership, fenced egress, capability sources,
   catalogue recovery, observed availability and truthful response accounting.
2. **面板 / Dashboard**：子路径、委托来源及出口展示、模型验证与复测、能力配置、
   推理用量状态和图标；保留上游新增的登录闸门、主题、筛选、模型标签与管理能力。
   Subpath support, delegation and egress display, availability, source settings
   and usage semantics alongside the upstream dashboard.
3. **验证与发布 / Verification and deployment**：健康探测、处理阶段诊断、精确 SHA
   的 GitHub-hosted 测试与产物、原生发布锁、排空、数据恢复组和安装核验。
   Bounded health diagnostics, hosted verification, exact-commit artifacts,
   deployment locks, draining and native recovery.
4. **说明 / Documentation**：桥接协议、诊断指南、历史调研与本记录。
   Bridge contracts, diagnostics, archived research and these maintenance records.

## 整合记录 / Consolidation records

### 2026-10-23：升级至 v1.7.32 / Upgrade to v1.7.32

- 上游基线：`477ba36ceea78ec4252c47a68e94a5a1612bab0b`（`v1.7.32`）；
  旧个性化来源：`5e50b1e25c24e63b1e54da6066661547759d2499`。
  Upstream base and previous deployed customization source are recorded above.
- 按以上四类职责追加提交，旧健康诊断的多轮修复、回退和重交付归并为最终实现。
  Append four topic commits; consolidate the health-fix/revert/re-delivery history.
- 采用上游当前模型快照、别名、SSE 心跳、API Key 账号与企业额度、
  付费档位门控、401 刷新、TLS 快速失败、国内任务调度和界面改进。
  Adopt upstream catalogue data, aliases and streaming heartbeat,
  API-key accounts, enterprise billing, tiers, refresh, TLS and UI improvements.
- 退役旧模型快照和随之变化的测试模型替换；统一采用各协议的失败终帧，保留空响应与流中断的真实失败状态。
  Keep upstream snapshot fixtures and protocol-specific terminal failures, including empty streams.
- 独立能力来源与上游新账单重试一并核查，避免独立账单失败后发送账号凭据。
  Preserve source isolation across the newly merged billing retry paths.
- 本机遵守仅编辑、静态审查与部署已验证产物的边界；动态回归和打包在
  GitHub-hosted CI 执行，生产切换由已有 updater 完成，随后验收健康与真实请求。
  Run dynamic verification and packaging on hosted CI; deploy through the existing
  updater and validate production health and a real request afterward.

候选 `02401c1` 已通过 [三平台回归](https://github.com/WesPerez/workbuddy2api-hub/actions/runs/39908845831)
和 [健康门禁与产物验证](https://github.com/WesPerez/workbuddy2api-hub/actions/runs/39908914026)：
共 237 个套件（175 Python + 62 JS）。候选证据仅对应候选；归并后的最终 SHA 必须重新通过
完整矩阵与 `mine` 分支的产物验证，不能复用其他 SHA 或分支的发布证据。
Candidate `02401c1` passed all 237 suites and the linked hosted gates. The final
consolidated SHA must pass the full matrix again and have its own verified runtime
from the `mine` branch; evidence from another SHA or branch cannot authorize deployment.

后续升级先更新上游基线，再按职责重放；共享文件同时核查双方行为，保持
`wb_identity.py` 上游原样。原生运行时与业务数据恢复链遵守项目发布机制，Docker
镜像只保实际使用版本，统一策略不因旧分支说明而放宽。

For later upgrades, update the upstream base and replay only retained topics.
Keep the upstream identity module intact and follow the existing native release
and data recovery contracts and the host's current-only Docker image policy.
