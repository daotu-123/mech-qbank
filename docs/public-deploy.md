# 公网部署指南（单用户 + 单账号）

> 目标形态：**公网单用户 + 单账号**（见 `docs/architecture-baseline.md` §0.2 / §0.3）。
> 一台 VPS + 域名 + TLS 终止，owner 自用，手机与浏览器登录后访问同一份数据。
>
> ⚠️ **本文档里的每一步都别跳。** 跳错一步的后果不是"用不了"，
> 而是"数据全泄露"或"限流形同虚设"。

---

## 0. 先理解：四个工作块各管什么

| 块 | 问题 | 手段 | 不做会怎样 |
|---|---|---|---|
| W1 | 服务只听 127.0.0.1，外部连不上 | 绑定地址配置化 | 只能本机用（现状） |
| W2 | Host 白名单只有本机名 | `QBANK_ALLOWED_HOSTS` | 配了 W1 之后所有请求 403 |
| W3 | 任何人能读写你的进度/备注/题库 | 单账号鉴权 | **数据全泄露** |
| W4 | 有人暴力猜口令 / 狂传 PDF | 速率限制 | 单点被打穿 |

**W2 与 W3 职责不同，不可互相替代**：
W2 挡的是 DNS rebinding 与跨站调用（"请求来自哪里"），
W3 挡的是未授权访问（"你是谁"）。两者并存。

---

## 1. 准备

```bash
# 1.1 生成口令哈希（交互输入，不回显、不留 shell 历史）
python tools/hash_password.py
# 输出：QBANK_PASSWORD_HASH=scrypt$32768$8$1$<salt>$<hash>

# 1.2 准备一个会话密钥（重启即失效所有旧会话）
python -c "import secrets; print(secrets.token_hex(32))"
```

> **口令哈希可以进配置仓库，明文口令绝对不行。**
> 交互式输入比命令行参数安全 —— 命令行参数会留在 shell 历史里。

---

## 2. 服务端配置

```bash
export QBANK_PUBLIC=1
export QBANK_ALLOWED_HOSTS=qbank.example.com
export QBANK_PASSWORD_HASH='scrypt$32768$8$1$...'
export QBANK_TOKEN_SECRET='<第 1.2 步生成的值>'
export QBANK_TOKEN_TTL=43200          # 12 小时
export QBANK_PORT=8640
```

### 全部可用环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QBANK_PUBLIC` | `0` | 公网模式。设 `1` 才绑 `0.0.0.0` **并强制开启鉴权** |
| `QBANK_BIND` | `127.0.0.1`（公网模式 `0.0.0.0`） | 绑定地址。单独设 `0.0.0.0` 而不开公网模式 → **拒绝启动** |
| `QBANK_ALLOWED_HOSTS` | `127.0.0.1,localhost,[::1]` | 逗号分隔，**精确匹配**（不支持通配/后缀） |
| `QBANK_PASSWORD_HASH` | 空 | scrypt 口令哈希。公网模式**必填**，缺了拒绝启动 |
| `QBANK_TOKEN_SECRET` | 随机 | 令牌表主密钥。不设则每次重启换新的（旧会话全失效） |
| `QBANK_TOKEN_TTL` | `43200` | 令牌有效期（秒），最小 60 |
| `QBANK_ACCOUNT_NAME` | `mech` | 账号名（只作显示） |
| `QBANK_AUTH_FAIL_THRESHOLD` | `5` | 允许连续失败几次，之后开始退避 |
| `QBANK_AUTH_BACKOFF_BASE` | `2` | 退避基数（秒），实际时长 = base × 2^(次数-阈值-1) |
| `QBANK_AUTH_BACKOFF_MAX` | `60` | 退避上限（秒）。**必须有上限**，否则被打 1000 次等于永久封禁 |
| `QBANK_UPLOAD_WINDOW` | `60` | 上传限流窗口（秒） |
| `QBANK_UPLOAD_MAX` | `10` | 窗口内最多上传几次 |
| `QBANK_TRUSTED_PROXIES` | 空 | 受信反代的**精确 IP**，逗号分隔 |
| `QBANK_TRUSTED_PROXY_CIDRS` | 空 | 受信反代的 CIDR 网段。**默认关**：网段写错一点就等于对全网采信 XFF |

---

## 3. 反代（Caddy 示例）

```
qbank.example.com {
    reverse_proxy 127.0.0.1:8640
}
```

### 3.1 反代必须做的两件事

**① 转发真实客户端 IP**

Caddy 默认会带 `X-Forwarded-For`，但**服务端只在反代本身被登记为受信时**才采信它。
所以要配：

```bash
export QBANK_TRUSTED_PROXIES=127.0.0.1
```

> 采信规则是 fail-closed 的：
> **对端不在受信列表 → 完全忽略 X-Forwarded-For，用真实对端地址。**
> 因为 XFF 是普通请求头，客户端可以随便写 —— 无条件采信等于
> "攻击者自己声明自己是谁"，限流与审计全部失效。
>
> uvicorn 侧的 `proxy_headers` 已被**显式关闭**（否则它会自己把
> `request.client` 换成 XFF 的第一节，把我们这套规则彻底绕过去）。

**② 放行大文件上传**

```nginx
# nginx 写法
client_max_body_size 12m;
```

默认上限 10 MB（`MAX_BODY_BYTES`），超了返回 413。

### 3.2 Host 头必须对齐

W2 的白名单要与反代的 `Host` 重写规则对齐。
Caddy 默认保留原始 Host（`qbank.example.com`）→ 符合白名单。

> 如果反代把 Host 改写成 `127.0.0.1:8640`，那白名单里还得有 `127.0.0.1`，
> 但那样公网访问也会被当作本机 —— **不要这么配**。

---

## 4. 自检清单

启动前逐条核对：

- [ ] `QBANK_PUBLIC=1`
- [ ] `QBANK_ALLOWED_HOSTS` 里有**真实域名**（只有本机名 → 拒绝启动）
- [ ] `QBANK_PASSWORD_HASH` 已设置
- [ ] `QBANK_TRUSTED_PROXIES` 指向反代地址
- [ ] 反代放行了 ≥10 MB 的请求体
- [ ] 反代的 Host 保留原始域名

启动后：

```bash
# 未带令牌 → 401
curl -i https://qbank.example.com/api/v1/stats
# HTTP/2 401
# {"error":{"code":"unauthorized",...}}

# 登录拿令牌
curl -s -X POST https://qbank.example.com/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"password":"你的口令"}'
# {"auth_required":true,"token":"tok_...","expires_at":...}

# 带令牌 → 200
curl -i -H "Authorization: Bearer tok_..." https://qbank.example.com/api/v1/stats
```

---

## 5. 令牌卫生（docs §0.4 的三通道）

| 通道 | 做法 |
|---|---|
| **日志** | 令牌与口令**根本不进日志函数**（不是事后过滤）。访问日志只记 method/path/status/ms |
| **Referer** | 令牌只走 `Authorization` 头，**永不进 URL**。URL 会被浏览器历史、Referer 头、代理日志记下 |
| **分享** | 不提供任何"生成分享链接"的接口 —— 那必然把令牌放进 URL |

前端存储策略（`web/src/auth.js`）：

- **access token** → 只放**内存**。刷新页面就没了（这是特性）。
- **refresh token** → 放 **sessionStorage**（标签页会话级，**不是 localStorage**）。
- **localStorage 里一个字节都不放。** 理由：localStorage 跨会话永存，
  且任何同源脚本都能读；一次 XSS 就等于令牌长期泄露。

---

## 6. 自建提示：想改口径看哪

- 令牌 TTL 改短 → `QBANK_TOKEN_TTL`（如 `3600` = 1 小时）
- 登录失败更严 → `QBANK_AUTH_FAIL_THRESHOLD=3`
- 上传更严 → `QBANK_UPLOAD_MAX=3`
- **改口令** → 换 `QBANK_PASSWORD_HASH` 并重启（重启即所有旧会话失效）

---

## 7. 还没做的（明确登记，别以为有了）

- **批量上传**（一次传多个 PDF）—— 不做。
- **多账号** —— 不做。身份模型就是单账号（docs §0.3）。
- **令牌持久化到 localStorage** —— 不做，理由见 §5。
- **上传进度条的真实百分比之外的阶段提示** —— 没有就是没有，不编。
