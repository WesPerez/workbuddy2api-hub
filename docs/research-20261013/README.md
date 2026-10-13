# WorkBuddy 生态调研结论（2026-10-13）

本目录保存 2026-10-13 对 WorkBuddy / CodeBuddy 反代与账号管理生态的只读调研，原先只存在于临时目录，现归档进仓库，供后续实现与复核引用。

## 文件

- `project-comparison.md`：各 WorkBuddy 反代 / 管理项目能力对比与若干前序结论的更正。
- `integration-risk-audit.md`：hub 与 panel 的鉴权、导入导出、签到与合规风险核验。
- `final-integration-gaps.md`：把 panel/manager/MetAPI 接入本 hub 时的缺口清单与证据。
- `candidates/`：候选项目的对接要点摘要。

## 与生产落地直接相关的结论

1. **hub 自身没有账号级出口。** `_route_accounts_login_start` 只接受 `realm`/`platform`，`start_login` 无 proxy 参数，profile 拉取直接用 `urllib.request.urlopen`。因此“每个 WorkBuddy 账号一个独立出口”不能由 hub 内部实现，必须由前置代理层提供——生产上由 account-hub 的 Resin 出口租约（`hub-<id>-<hash>` 身份键）承担，hub 只负责凭据与刷新。
2. **hub 的面板路由没有可替代的令牌接口。** `/accounts*` 全部走 `_is_panel_route` 且必须是内存态面板会话（`X-Panel-Token`），重启即失效；不存在 `wbt_` 类长期令牌。自动化必须通过浏览器会话建立，不能靠静态令牌。
3. **导入契约是宽容的。** 导出文档、裸数组、单账号对象、桌面端凭据、cockpit snake_case 行都能被 `_coerce_account_rows` / `normalise_import_row` 归一化；`access_token`/`refresh_token`/`expires_at`（毫秒）会被映射到扁平结构。
4. **导出默认包含明文机密。** HTTP 侧 `/accounts/export` 默认 `secrets=1`，必须显式 `?secrets=0` 才脱敏。任何导出物都按机密处理，不进入仓库与工单。
5. **默认面板口令是 `admin`。** 校验逻辑不强制改密，仅通过 `panel_password_default` 透出布尔量提示。部署后必须立刻改密。
6. **Cookie/CSRF、会话 TTL 与并发上限尚未逐行核验**，属于已知未确认项。
7. **续期写入存在竞态窗口。** manager 的 renew 从读快照到 `os.replace` 之间隔着一次网络往返，Go 侧保活可在此期间完成一次“选号→刷新→写回”。因此委派账号的凭据只能由单一所有者刷新，MetAPI 侧只允许通过官方会话续期交付更新，不得手工 `PUT` 覆盖 ``密码/代理``。

## 使用约束

本目录内容为调研证据，不构成对上游项目的运行时依赖。任何实现变更以 `mine` 分支代码与 `docs/account-hub-bridge.md` 为准。
