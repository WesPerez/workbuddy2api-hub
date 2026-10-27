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

## 可重复更新流程 / Repeatable upgrade procedure

此流程源于 2026-10-23 的已验收更新。用户明确选择“按职责整理 Git 提交，并更新说明”，
并补充“不一定所有个性化提交都要保留，以最优版为准”。更新要求包含职责重组，
不能只 merge 上游后追加一个大提交，也不要为了凑四条保留重复实现。
The accepted October 23 workflow rebuilds topic commits on current upstream and
prefers the better implementation. Commit count follows actual responsibilities.

1. **盘点与锁定来源。** 读本文件、AGENTS、CONTRIBUTING；确认工作区干净、没有其他
   写入者，再 fetch `origin` 和 `upstream --tags`。登记旧 `mine` SHA、生产 healthz SHA、
   `merge-base`、最新上游 SHA/tag。`main` 仅镜像上游，生产追踪 `mine`。
   Inventory the clean checkout, concurrent writers, previous source/runtime,
   merge base and upstream SHA/tag before making changes.
2. **先保留源码恢复点，再开隔离工作树。** 给旧 `mine` 建带日期的本地恢复分支；
   从固定的上游 SHA 新建 `sync-upstream-<date>` 工作树。导出旧 merge-base 到旧 mine
   的 `git diff --binary`，在候选中 `git apply --3way`。不 reset 活跃工作区，不复制
   生产账号或令牌，不更换代理与调度配置。
   Keep a source recovery ref, replay the customization diff in an isolated
   checkout and leave credentials, egress and live traffic configuration alone.
3. **按功能审查双方差异。** 列出保留、采用上游、退役三类及其理由；逐块解决冲突，
   也检查自动合并的共享调用链。至少核对账号令牌唯一写入、路由 CAS、Resin 禁止直连、
   未知结果写请求不重放、独立能力凭据隔离、真实响应/截断记账、国际自动任务闸门、
   子路径、翻译及发布健康身份。`wb_identity.py` 保持最新上游，不钉旧版本指纹。
   Review merged call paths as well as conflict markers; preserve ownership,
   replay boundaries, accounting and deployment identity, and use upstream identity code.
4. **修复与归并。** 在候选里完成回归修复后，把最终 diff 按功能暂存和提交，正文中英
   双语。界面、网关、发布等可分开；共享文件按语义 hunk 分，不按修复次数分。
   保存“旧提交 → 新职责”和上游功能清单；确认最终 tree 与已验证候选一致。
   Group the final diff by responsibility, with bilingual bodies and a mapping
   from previous commits; verify that regrouping did not alter the tested tree.
5. **提交前冒烟与完整验证。** 依 CONTRIBUTING，必须启动空账号、独立临时数据目录的
   候选实例，随机面板密码登录、导航并确认无 JS/HTTP 错误。真实模型烟测通过正在运行
   的 Hub 令牌所有者转发，候选不得复制或轮换生产凭据；证据注明两层网关的边界。
   界面修改覆盖少量/大量账号、桌面/手机、合并/独立页签、折叠、菜单和筛选。
   Python 标准库和已安装 Node 的回归可本机运行；禁止本机安装依赖、编译、镜像构建
   或打包。完整三平台验证与发布产物必须来自 GitHub-hosted CI。测试工作流使用 `CI_SOURCE_REF` /
   `CI_SOURCE_REF_NAME` 保留原事件语义；非 tag 跳过版本标签门禁，tag 仍强制核验。
   Smoke-test before committing, using isolated accounts and the running credential
   owner for a real upstream reply. Exercise the layout in a browser. Hosted CI
   owns cross-platform verification and packaging; never build a local runtime.
6. **CI 请求只登记一次。** push 后先 `gci build <workflow> --repo WesPerez/workbuddy2api-hub
   --ref <branch>` 复用自动事件，随后等待原 request ID。只有明确不存在自动事件，或
   工作流本来要求手动事件时才 `gci dispatch`；不要因等待慢重复派发。`gh` 在 fork
   可能默认选上游，命令始终指定 fork 仓库。先检查 `gci status` 的 health，受管
   `ci-*` 备用仓库的 refs、Actions、secrets、release 只由控制器操作。
   Reuse automatic CI requests; dispatch only a genuinely required manual event.
   Always name the fork explicitly and leave managed backup repositories to the controller.
7. **发布整理后的历史。** 重新读取远端 `mine`，必须仍等于第 1 步的旧 SHA；若变化，
   先纳入新提交再重新验收。`main` 快进到上游；用户要求重组的 `mine` 用精确
   `--force-with-lease=refs/heads/mine:<old-sha>` 更新，不能无条件 force。最终 SHA
   重新跑完整 `tests.yml` 及 `mine.yml` 健康/产物门禁，不能拿候选或别的分支回执发布。
   Recheck the source lease, fast-forward the upstream mirror and publish the
   authorized topic history with an exact lease. The final SHA needs its own full
   test matrix and production artifact, even when its tree matches the candidate.
8. **由原生 updater 切换并闭环。** 既有 timer/update.py 持有发布锁，校验仓库、分支、
   SHA、事件与产物，等待空闲并排空后安装。不要手工替换 current、重启服务或绕过
   EvidenceError；先确认 CI 是否尚未回存。验收 current、MainPID cwd、release.json、
   healthz、last-success 五项 SHA 一致，并校验 files.json 哈希、真实请求、公开子路径。
   写入脱敏证据目录和本文件的日期记录，关闭临时进程并删除本次临时凭据。
   The existing updater owns verification, locking and draining. Match all five
   runtime identities, verify file hashes and a real reply, record evidence and
   retire only this upgrade's temporary processes and credentials.

源码恢复分支不是镜像备份。镜像保留仍服从主机“仅保正在使用版本”的统一合同；
业务数据和原生代码恢复链按 updater 维护。若上游已追平、没有功能差异，不必制造空提交。
Source recovery refs are separate from image retention. Do not manufacture commits
when upstream already contains the required implementation.

### 2026-10-27：升级至 v1.8.0 / Upgrade to v1.8.0

- 上游：`4f7ae6f` / `v1.8.0`；原生产及个性化来源：`37e6918`；原基线：`477ba36`。
  Upstream, previous production source and previous base are recorded above.
- 采用上游日志非阻塞、工具首段参数增量、成功解除凭证标记、重启恢复模型限流、
  更新检查超时和最新身份选项；国际自动任务重构保留在禁用闸门之后，新增活跃上报入口
  同样受闸门约束。国内原生任务与只读跨区域 streak 查询保留上游修复。
  Adopt upstream fixes while keeping new international activity entry points
  behind the existing disabled gate; retain domestic tasks and realm-aware reads.
- 网关保留 `74b55d1` 的不重放修复、`447f240` 的截断/断流与用量修复、`37e6918` 的
  compact effort 记账，折叠进功能职责；面板修复账号多余纵向滚动、关闭菜单的溢出
  留白，以及签到四项筛选在标题下方排列；支持的浏览器以原生 popover 顶层展示菜单，避免被吸顶标题或表格裁切。此次沿用四个仍完整的职责，无额外碎片提交。
  Fold replay and truthful terminal-accounting fixes into the gateway topic, and
  fold account scrolling, closed-menu overflow and filter layout into the dashboard topic.
- 候选标准库/Node 回归 237 套全部通过；浏览器 18 个布局组合及筛选、折叠交互通过，
  候选经运行中令牌所有者的流式/聚合真实请求均收到正确模型正文。验证与发布回执按最终
  SHA 单独保存；工作树测试不代表已上线。
  Record final-SHA CI and deployment evidence separately from working-tree checks.
