# WorkBuddy 国际版登录链路（匿名静态勘察）

归档说明：以下为 2026-10-13 的上游页面与匿名接口观察。前后两部分是静态分析与后续运行观测，冲突时以后者为当时证据；不将上游 JS 分片名、应用标识或参数拼装当作长期 API 合同。生产接入使用 [Account Hub 国际版流程](../account-hub-bridge.md)，由官方页面生成授权 URL；已有四个身份完成接入和 Sub2 验收，本文中的“待确认”仅描述调研当时状态。

日期：2026-10-13。仅匿名 GET 公开静态资源，未登录、未注册、未授权、未使用任何凭据。
未记录任何 state/code/token 值。

入口：`https://www.workbuddy.ai/login?platform=CLI`（301 → `https://www.workbuddy.ai/login/?platform=CLI`）。
登录页唯一应用 JS：`https://download.codebuddy.ai/web/login/ca3767016a37e69bd2a888a0b760a58f84b3dbec/assets/index-oMkbFDfl.js`

## 结论：GitHub 授权 URL 由服务端下发，客户端不构造

页面上的 GitHub 按钮来自 OneID 组件（`oneid-login-method` / `oneid-social-auth`，见 vendor-oneid）。
点击 GitHub **不直接跳转**，而是：

1. `POST https://api.account.tencentid.com/authz/v1/authn/social/github/login`
   body：`{callback_uri: "https://account.tencentid.com/?connector=github&redirect_route=/cidp/transfer", app: <appId>, client_mode: "widget", scene: "login"}`
2. 响应 `authorize_url`；客户端 `window.open(authorize_url, "_blank", "width=500,height=550,...")` 开**弹窗**。
   （结果缓存 60s：`authUrlCache.set(e, {url:t, expireAt: Date.now()+6e4})`）

因此**公开 JS 中没有任何 GitHub `client_id` / `redirect_uri` 常量**，两者都由 OneID 服务端在 `authorize_url` 里给出。Account Hub 不得自行拼 GitHub authorize URL。

相关 OneID 接口（vendor-oneid，`axiosInstanceIntl`）：

- `POST /authz/v1/authn/social/{provider}/login` → `{authorize_url}`
- `POST /authz/v1/authn/social/{provider}/callback` → `{params}`
- `GET /v1/customer/consent/oauth2`

## 回调链（WorkBuddy 侧）

路由（index-oMkbFDfl.js）：国际版分支注册 `/login/oneid-callback`、`/login`、`*`；国内分支另有 `/select`、`/login/started`、`/login/register/user/complete`。

回调组件（分片 `index-nxpQuoip.js`）逻辑：

1. 从 `window.location.search` 取 `code`、`platform`（默认 `admin`）、`state`（默认 `0`）、`redirect_uri` 或 `target_link_uri`。
2. 组 `w = ${origin}/login?platform=<platform>&state=<state>`（若有 `redirect_uri` 则附上）。
3. 隐藏 iframe 载入 `${origin}/auth/realms/copilot/protocol/openid-connect/auth?client_id=console&response_type=code&redirect_uri=${encodeURIComponent(w)}`。
4. `onLoad` 读 `iframe.contentWindow.__ONEID__.loginUrl`，执行 `GET ${loginUrl}&from_oneid_login=true`，取回 `{redirect_uri, state}`。
5. `window.location.href = ${redirect_uri}?code=<code>&state=<state>`。

失败文案键：`oneidCallback.{missingCode,sessionFailed,loginInfoFailed,loginFailed}`；15s 超时 → `sessionFailed`。

配套 WorkBuddy 自有接口（分片 `index-Dgih-XKe.js`）：

- `GET /console/auth/login?platform&state&domain`（头 `X-Domain`）→ 登录态
- `GET /v2/plugin/auth/token?state`（头 `X-No-Authorization: true`）
- `POST /console/login/enterprise/{id}?state`、`POST /console/login/account`、`POST /console/account/switch`
- `GET /v2/plugin/login/entry-policy`、`GET /v2/plugin/login/gray-decision`
- `GET /console/sso-domain-exists`、隐藏 iframe `GET /console/logout`

两层 state **不同**：WorkBuddy plugin state（来自 `/v2/plugin/auth/state`，走 URL/iframe 透传）与 OneID 自己的 state（`from_oneid_login` 返回）各自独立，不能要求字符串相等。

## Account Hub 辅助浏览器 allowlist

导航/请求必须放行：

- `www.workbuddy.ai`（`/login`、`/login/oneid-callback`、`/auth/realms/copilot/...`、`/console/*`、`/v2/plugin/*`）
- `download.codebuddy.ai`（全部前端资源；JS/CSS/svg）
- `api.account.tencentid.com`、`oauth2.account.tencentid.com`、`account.tencentid.com`、`identity.tencentid.com`
- `github.com` 及资源域（GitHub 登录页、OAuth consent、`github.githubassets.com`、`avatars.githubusercontent.com`）
- 验证码资源：`turing.captcha.qcloud.com`、`captcha.gtimg.com`、`global.captcha.gtimg.com`
- 遥测（可阻断，非必需）：`galileotelemetry.tencent.com`

浏览器能力要求：

- GitHub 走 **弹窗**（`window.open`），上下文不能拦截 popup，且需能操作弹窗页并等待其关闭/回跳。
- OneID 通过**隐藏 iframe**（同源 `www.workbuddy.ai`）暴露 `__ONEID__.loginUrl`；需允许同源 iframe 及读取其 `contentWindow`。
- 跨站 XHR 到 `api.account.tencentid.com` 需带凭据，不能注入禁用 CORS/cookie 的策略。

## 重定向白名单（WorkBuddy 自身 `sanitizeRedirectUri`）

分片 `sanitizeRedirectUri-CQbI7sgo.js` 规则，用于 `redirect_uri`/`redirect_route` 参数：

- 站点相对路径（以 `/` 开头且不以 `//` 开头、不含 `\`）→ 放行
- 绝对 `http`/`https`：host 必须**等于**当前 host 或 `GENIE_DOMAINS` 列表内域名（`Mode.Equal`）；另 `workbuddy.link` 亦在列
- 仅 staging 额外允许 `page.qq.com`、`workbuddy.link` 的**子域**（`Mode.Subhost`）
- 拒绝 `javascript:`、`data:`、`vbscript:`、`blob:`、`file:`

页面注入的 `GENIE_DOMAINS`（登录页 HTML）：`www.ge.site, genie.codebuddy.cn, genie-canary.ge.site, genie-canary.codebuddy.cn, staging.ge.site, genie.tencent.com, genie-bj.tencent.com, www.workbuddy.link, www.workbuddy.cc`

## OneID 应用标识（index-oMkbFDfl.js）

- appId：prod `etahzsqej0n4`；staging/test/dev `esoikz80kd8g`
- realm：prod `sso-etahzsqej0n4`；staging/test/dev `sso-esoikz80kd8g`
- SSO 域：codebuddy/workbuddy/tencentbuddy → `tencent.sso.codebuddy.cn`（staging `tencent.staging-sso.codebuddy.cn`）；其余 → `tencent.sso.copilot.tencent.com`（staging `tencent.sso.copilot-staging.tencent.com`）
- OneID 端点映射（vendor-oneid，env=prod）：api `https://api.account.tencentid.com`、oauth2 `https://oauth2.account.tencentid.com`、main `https://account.tencentid.com`；国内对应 `account.tencent.com` 系

## 未取到 / 需运行期确认

- GitHub 侧 `client_id`、`redirect_uri`、`scope` 的实际取值（由 OneID 服务端下发，静态不可得）
- OneID 应用后台为 WorkBuddy 注册的 GitHub 回调地址
- GitHub 是否出现 OAuth consent、是否已授权直接回跳

---

# 追加：匿名实测 OneID GitHub login API

匿名 POST 公开接口得到结构，未登录、未授权、未跟随 authorize_url，未记录 state 值。

## 正确调用方式

`POST https://oauth2.account.tencentid.com/authz/v1/authn/social/github/login`

请求头（Origin 是硬要求）：

```
Content-Type: application/json
Accept: application/json
Origin: https://account.tencentid.com      # 必需；缺失 → 409 E0010029 不允许的操作
Referer: https://account.tencentid.com/
User-Agent: <浏览器 UA>
```

请求体：

```json
{"callback_uri":"https://account.tencentid.com/?connector=github&redirect_route=/cidp/transfer",
 "app":"codebuddy", "client_mode":"widget", "scene":"login"}
```

- `app` 必须 `codebuddy`（`workbuddy`/`console` → 400 E0010030 参数[app]不正确）
- `client_mode` 必须 `widget`（`web` → 400 E0010030 参数[client_mode]不正确）
- `callback_uri` 必填（缺失 → 400 E0010030 callback_uri required）
- `scene` 可用 `signup`（同一 redirect_uri，allow_signup=true）

无需 CSRF token、无需登录 Cookie；`withCredentials:true` 只是客户端 axios 默认（`cS={timeout:1e4, withCredentials:!0, headers:{Accept,Content-Type}}`）。

## 响应结构

HTTP 200 → `{"authorize_url":"..."}`（顶层单字段）。错误 → `{"errCode","errMessage","id"}`。

| 项 | 值 |
|---|---|
| host/path | `github.com` `/login/oauth/authorize` |
| client_id | `Iv23liqNab23Foc1AG5e` |
| redirect_uri | `https://account.tencentid.com/?connector=github&redirect_route=/cidp/transfer` |
| scope | `read:user user:email` |
| allow_signup | `true` |
| prompt | `select_account` |
| 其他参数 | `state`（存在，值不记录） |

## 受限浏览器精确 allowlist

1. 授权弹窗只允许 `https://github.com/login/oauth/authorize`，必须钉死 `client_id=Iv23liqNab23Foc1AG5e` 与 `redirect_uri=https://account.tencentid.com/?connector=github&redirect_route=/cidp/transfer`。
2. GitHub 回跳必须落 `https://account.tencentid.com/`（query 含 `connector=github&redirect_route=/cidp/transfer`），其余 host 拒绝。
3. 再由 account.tencentid.com 处理并回到 `https://www.workbuddy.ai/login/oneid-callback`。

## 登录页 iframe（补充）

登录页（`index-BoJ3AJkv.js`）本体不含 GitHub 按钮，而是内嵌同源 iframe：

```
\${origin}/auth/realms/copilot/protocol/openid-connect/auth?client_id=console&response_type=code
  &redirect_uri=<encode(/login/select?platform&state&redirect_uri)>&v=2210&product=codebuddy
```

GitHub/Google 按钮位于 iframe 内的 `#kc-social-providers`。iframe 同源，可直接读 `contentWindow`。
