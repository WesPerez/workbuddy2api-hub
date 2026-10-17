# mine 分支维护 / Maintaining mine

`main` 是 `ardeyouxipianyi/workbuddy2api-hub:main` 的原样镜像；本次基线为
`e7055ea6c9e06e9556052659c65539c4a3df1bde`（v1.7.12）。`mine` 以此为父链，
只保留以下四类定制提交，不重写上游提交。

`main` mirrors upstream without fork-specific changes. `mine` starts from that
history and keeps four topic commits; upstream commits remain intact.

1. **网关与账号 / Gateway and accounts**：Account Hub 委托登录与取消；令牌唯一写入者；
   路由独立 CAS、刷新与回滚；Resin 受管出口、代际恢复和禁止直连回落；独立账单、
   模型目录及对话服务配置；完整目录与最后成功快照；按实际响应判断模型可用性；
   空响应判失败、推理用量真实性；保留国内任务，停用国际自动活跃任务。
   Delegated credential ownership, managed egress, capability endpoints,
   catalogue recovery, observed availability and truthful response accounting.
2. **前端 / Dashboard**：子路径兼容、登录来源与出口展示、模型验证及复测界面、
   能力接口设置、推理用量展示和站点图标；保留上游主题、来源横幅、官方名称与采样参数。
   Keep fork UI features together while retaining upstream themes and catalogue UI.
3. **CI 与部署 / CI and deployment**：GitHub-hosted 验证和打包、精确提交产物核验、
   安装与自动更新、运行时依赖清单、服务器边界和手动触发上游测试矩阵。
   Verified runtime packaging, installation and deployment contracts.
4. **文档 / Documentation**：桥接协议、历史调研和本维护说明。
   Bridge contracts, archived research and this branch-maintenance guide.

旧定制历史位于本地 `archive/mine-before-upstream-20261018`，原基线位于
`archive/main-before-upstream-20261018`。这些归档保留历史供核对，不参与新 `mine` 父链。

The local archive refs retain the pre-consolidation history for comparison.
For future upgrades, fast-forward `main`, replay the four topic commits, reconcile
shared files without dropping either side's behavior, and run the hosted tests.

上游的 `wb_identity.py` 保持原样，以保留 v1.7.7 起的国际渠道校验修复。
国际自动活跃任务的差异属于原有分支策略，相关测试验证旧入口不会发送任务请求。

The upstream identity implementation remains unchanged, including the channel
verification fix. International activity tests cover the fork's existing disabled
automation policy rather than restoring upstream automation.
