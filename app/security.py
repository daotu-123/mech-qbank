# -*- coding: utf-8 -*-
r"""mech-qbank · security 层：公网暴露安全原语（W1–W4）

## 定位
--------------------------------------------------------------------------
`app/` 的分层是 storage → repository → service → core → router。
本模块属于 **router 之下、service 之上**的一层「安全域」：

    router (api.py / server.py) → security（本模块）→ service → core

**只允许标准库**。原因与 service.py 相同：`app/server.py` 是零第三方依赖的
回退服务端（start.bat 在 fastapi 不可用时启动它），本模块一旦 import
argon2 / passlib / jwt 之类，这条路径直接失效。

## W1–W4 各自做什么
--------------------------------------------------------------------------
| 块 | 做什么 | 在本模块的哪部分 |
|---|---|---|
| W1 | 绑定地址配置化，默认仍 `127.0.0.1` | `Config.bind` |
| W2 | `ALLOWED_HOSTS` 配置化 | `Config.allowed_hosts` |
| W3 | 单账号鉴权 | `hash_password` / `verify_password` / `TokenStore` |
| W4 | 速率限制 | `RateLimiter` |

## 为什么不引第三方库（owner 硬要求）
--------------------------------------------------------------------------
- 口令哈希用 **`hashlib.scrypt`**（标准库，内存硬，抗 GPU/ASIC）。
  不用 argon2-cffi：那是唯一的硬要求，且它需要编译扩展。
- 会话令牌用 **`hmac` + 服务端令牌表**（可撤销）。
  不用自签 JWT：自签的载荷里带了签发时间，服务端**无法撤销**一个
  已经发出去的 JWT——除非额外维护黑名单，那还不如直接用令牌表。
  本模块的令牌表让「撤销」是 O(1) 的删除，语义干净。
- 限流用普通 dict + 时间戳，不用 redis 之类：单用户场景，进程内足够。

## ⚠️ 令牌卫生三通道（docs/architecture-baseline.md §0.4）
--------------------------------------------------------------------------
1. **日志**：访问日志不得出现明文令牌。做法是**根本不把令牌传进日志函数**，
   而不是事后过滤——过滤会漏。口令同理：登录接口的 body 不进日志。
2. **Referer**：令牌不进 URL。做法是令牌只走 `Authorization` 头，
   永远不放 query string（放 query 会被浏览器存进历史与 Referer 头）。
3. **分享**：不生成含令牌的可分享链接。本模块不提供任何「生成链接」接口。

## 客户端 IP 的判定（W4 的关键，也是最容易做错的地方）
--------------------------------------------------------------------------
限流按「客户端 IP」计。反代（Caddy / nginx）后面，若直接用 socket 的
peer address，所有请求的源 IP 都变成反代自己的 —— 限流形同虚设
（一个 IP 打满，全站被锁）。

所以要采信 `X-Forwarded-For`。**但 XFF 是客户端可伪造的**：
任何人加一个 `X-Forwarded-For: 1.2.3.4` 头就能假装自己是别人。

规则（**fail-closed**）：
    只有当**直接对端（peer）本身**是配置里登记过的受信反代时，
    才采信 XFF 的最左一节；否则一律用 peer 地址。

这与 nginx 的 `real_ip_header` + `set_real_ip_from` 是同一思路：
**信任链只能从已知节点开始**，不能凭一个头就相信来路。

## 失败退避的算法（W4）
--------------------------------------------------------------------------
鉴权失败按 IP 计数。第 N 次失败（超过阈值）后的等待时间按
`BASE * 2^(N - THRESHOLD)` 递增，设上限。
这不是"延迟递增"就完事——**必须封顶**，否则一个 IP 被打 1000 次后
要等 2^990 秒，等于永久封禁（虽然不致命，但会吃掉令牌表的槽位）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

# ---------------------------------------------------------------- 配置


class Config:
    """一份配置快照。**由环境变量驱动**，两套服务端读同一份。

    为什么用环境变量而不是配置文件：公网部署时 systemd / docker / Caddy
    都能直接注入；而配置文件要额外处理「谁有权限读它」——口令哈希文件
    一旦被别的用户可读，公网化就白做了。

    ## 默认值 = 现在的行为（硬门）
    `Config()` 不带任何环境变量时，得到的配置与本模块存在之前**完全一致**：
    绑 127.0.0.1、白名单三个本机名、不鉴权、无限流。
    """

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.env = env

        # ---- W1：绑定地址 ----
        # 默认必须是 127.0.0.1。只有**显式**开公网模式才切 0.0.0.0。
        # 这一点是安全底线：本机模式下 0.0.0.0 不开放（docs §0.2）。
        self.public = _flag(env.get('QBANK_PUBLIC'))
        self.bind = (env.get('QBANK_BIND') or '').strip() or ('0.0.0.0' if self.public else '127.0.0.1')

        # ---- W2：Host/Origin 白名单 ----
        # 公网模式**必须**显式给出域名，否则宁可不启动。
        # 通配/后缀匹配一律禁止（它是 DNS rebinding 防护，见 docs §0.2）——
        # 「.example.com 能匹配 evil.com 吗」这类判断一旦写错就全开放了。
        raw_hosts = (env.get('QBANK_ALLOWED_HOSTS') or '').strip()
        if raw_hosts:
            self.allowed_hosts = frozenset(
                h.strip().lower() for h in raw_hosts.split(',') if h.strip())
        elif self.public:
            # 公网模式没给域名 → 后面 start() 会拒绝启动（fail-closed）
            self.allowed_hosts = frozenset()
        else:
            self.allowed_hosts = frozenset({'127.0.0.1', 'localhost', '[::1]'})

        # ---- W3：鉴权 ----
        # 口令哈希：scrypt。格式 `scrypt$n$r$p$salt_b64$hash_b64`。
        self.password_hash = (env.get('QBANK_PASSWORD_HASH') or '').strip()
        # 会话令牌表的主密钥。缺省时**每次启动随机生成**——
        # 这样重启即让所有旧令牌失效（本来也该如此，令牌是短时的）。
        self.token_secret = (env.get('QBANK_TOKEN_SECRET') or '').strip() or \
            secrets.token_hex(32)
        # 令牌有效期（秒）。默认 12 小时：够手机上一次使用，又不至于
        # 长期有效。短时令牌的意义在于「泄露后很快失效」。
        try:
            self.token_ttl = max(60, int(env.get('QBANK_TOKEN_TTL') or 43200))
        except ValueError:
            self.token_ttl = 43200

        # ---- W4：速率限制 ----
        # 鉴权失败阈值：超过这个次数就开始退避。
        try:
            self.auth_fail_threshold = max(1, int(env.get('QBANK_AUTH_FAIL_THRESHOLD') or 5))
        except ValueError:
            self.auth_fail_threshold = 5
        # 退避基数（秒）与上限（秒）。
        try:
            self.auth_backoff_base = max(1, int(env.get('QBANK_AUTH_BACKOFF_BASE') or 2))
        except ValueError:
            self.auth_backoff_base = 2
        try:
            self.auth_backoff_max = max(1, int(env.get('QBANK_AUTH_BACKOFF_MAX') or 60))
        except ValueError:
            self.auth_backoff_max = 60
        # 上传接口限流：窗口秒数 + 窗口内最大次数。
        try:
            self.upload_window = max(1, int(env.get('QBANK_UPLOAD_WINDOW') or 60))
        except ValueError:
            self.upload_window = 60
        try:
            self.upload_max = max(1, int(env.get('QBANK_UPLOAD_MAX') or 10))
        except ValueError:
            self.upload_max = 10

        # ---- 反代信任 ----
        # 登记了哪些对端地址是「受信反代」。只有这些对端带来的 XFF 才采信。
        raw_proxies = (env.get('QBANK_TRUSTED_PROXIES') or '').strip()
        self.trusted_proxies = frozenset(
            p.strip() for p in raw_proxies.split(',') if p.strip())
        # 受信反代是否允许「按网段」信任（CIDR）。默认关：
        # 网段写错一点点就等于对全网采信 XFF，攻击者直接绕过限流。
        self.trusted_proxy_cidrs = tuple(
            c.strip() for c in (env.get('QBANK_TRUSTED_PROXY_CIDRS') or '').split(',')
            if c.strip())

    # ------------------------------------------------------------ 判定

    @property
    def auth_enabled(self) -> bool:
        """是否启用鉴权。

        **公网模式强制开启**：公网暴露 + 无鉴权 = 任何人都能读走全部
        进度、备注并改题库。这不是"用户自己注意"能解决的，是默认拒绝。
        """
        if self.public:
            return True
        return bool(self.password_hash)

    def host_allowed(self, host: str) -> bool:
        """Host 头是否在白名单。**精确匹配**，不做通配或后缀匹配。"""
        h = (host or '').split(':')[0].strip().lower()
        return h in self.allowed_hosts

    def host_allowed_for(self, path: str, host: str) -> bool:
        """某个路径的 Host 是否放行。

        ## 为什么健康检查要豁免
        `/health/*` 是**基础设施接口**，不是业务 API：
        反代（Caddy / nginx）的健康探针打过来时，Host 头是**上游地址**
        （`127.0.0.1:8640`）而不是用户访问的域名 —— 那是反代自己发的请求，
        不带用户域名。若这里也按白名单拦，公网部署的探针会全部 503，
        反代会把服务标记为不健康并把它踢下线。

        所以健康检查豁免 Host 白名单（**但仍然需要鉴权**吗？—— 不需要，
        见 api.py 的 _is_auth_exempt：live 只回 {"status":"ok"}，不泄露任何信息）。

        ⚠️ 这个豁免**只针对精确的两个路径**，不放开别的。
        """
        if path in ('/health/live', '/health/ready'):
            return True
        return self.host_allowed(host)

    def start_errors(self) -> list:
        """启动前的自检。返回错误清单（非空 = 不该启动）。

        **fail-closed**：配置有问题就拒绝启动，而不是"降级着跑"。
        一个"公网模式但没配域名"的服务如果悄悄按本机模式跑，
        owner 会以为已经公网化了 —— 那比直接报错危险得多。

        ## 半开配置也要拒绝（2026-10-21 打包前自查发现）
        除了"公网模式配不全"，还有一种**更隐蔽**的半开状态：

            QBANK_ALLOWED_HOSTS=qbank.example.com     # 白名单里有真实域名
            # 但没设 QBANK_PUBLIC，也没设 QBANK_PASSWORD_HASH

        它的表现是：绑 127.0.0.1（看起来很安全）、白名单里是公网域名、
        **却完全没有鉴权**。本机直连会被 Host 白名单拦掉（403），
        所以 owner 自测时看到的是"打不开"，会以为配置生效了；
        但反代一旦把 `Host: qbank.example.com` 转发过来 —— 请求全部放行，
        **任何人都能读走全部进度、备注、打卡记录**。

        危险在于它"看起来"是安全的：绑定地址是回环、没开公网模式。
        但 `QBANK_ALLOWED_HOSTS` 里有非本机名，就意味着**存在一条
        能把外部请求带进来的路径**（反代 / 端口转发 / SSH 隧道）。
        有外部入口就必须有鉴权 —— 这条不变量由下面的检查守住。
        """
        errs = []
        if self.public:
            if not self.allowed_hosts:
                errs.append('公网模式必须设置 QBANK_ALLOWED_HOSTS（实际域名，逗号分隔）')
            if not self.password_hash:
                errs.append('公网模式必须设置 QBANK_PASSWORD_HASH'
                            '（用 tools/hash_password.py 生成）')
            # 只有一个本机名 = 域名没配对
            only_local = self.allowed_hosts <= {'127.0.0.1', 'localhost', '[::1]'}
            if only_local:
                errs.append('公网模式的 QBANK_ALLOWED_HOSTS 里只有本机名，'
                            '公网访问会被 403')
        if self.bind == '0.0.0.0' and not self.public:
            errs.append('QBANK_BIND=0.0.0.0 需要同时设置 QBANK_PUBLIC=1'
                        '（默认只监听 127.0.0.1）')

        # ---- 半开配置：白名单含非本机名，但没有鉴权 ----
        # 判据是「白名单里有没有非本机名」，而不是「public 是不是 1」。
        # 因为外部请求能不能进来，取决于白名单里有没有外部用的名字，
        # 不取决于有没有开 public 开关。
        local_names = {'127.0.0.1', 'localhost', '[::1]'}
        external_hosts = sorted(self.allowed_hosts - local_names)
        if external_hosts and not self.auth_enabled:
            errs.append(
                'QBANK_ALLOWED_HOSTS 里有非本机名（%s）但没有开启鉴权 —— '
                '这等于「能进来的路开着、门却没锁」。'
                '要么设 QBANK_PUBLIC=1（会强制要求 QBANK_PASSWORD_HASH），'
                '要么把 QBANK_ALLOWED_HOSTS 改回只有本机名。'
                % ', '.join(external_hosts))
        return errs


def _flag(v) -> bool:
    """环境变量真值判定。1/true/yes/on 都算真。"""
    return str(v or '').strip().lower() in ('1', 'true', 'yes', 'on')


def reload_config(env=None) -> Config:
    """按当前环境变量重新读一份配置。

    ## 为什么需要它
    `Config` 通常在模块 import 时读一次环境变量。但**测试**需要在
    同一个进程里跑「本机模式」与「公网模式」两套行为 ——
    若只能靠「起子进程 + 设环境变量」，本机沙箱会把监听端口的子进程
    SIGTERM 掉（实测：测试进程直接被杀，退出码 SIGTERM）。

    所以给一个显式的重载入口：测试改完 os.environ 后调它，
    两套服务端各自调 `reload_security()` 重建自己的 Config/Account/Limiter。

    ⚠️ 重载会**清空令牌表**（新的 Config → 新的 TokenStore）。
    这正是想要的行为：换配置 = 换一套会话。
    """
    return Config(env)


# ---------------------------------------------------------------- W3：口令哈希


def hash_password(password: str, n: int = 2 ** 15, r: int = 8, p: int = 1) -> str:
    """算口令哈希（scrypt）。

    ## 参数为什么是这些
    `n=2**15, r=8, p=1` → 内存开销约 **32 MB/次**（scrypt 的成本是
    `128 * n * r` 字节）。这是 OWASP 对 scrypt 的最低推荐档。
    内存硬是 scrypt 相对 bcrypt 的核心优势：GPU 并行算得动 bcrypt，
    但算不动 32MB 内存的 scrypt。

    之所以**不引 argon2-cffi**（owner 硬要求）：那需要编译扩展，
    在只有标准库的零依赖回退路径上直接不可用；`hashlib.scrypt` 是
    标准库自带的（OpenSSL 提供），两边都能跑。

    格式：`scrypt$n$r$p$salt_b64$hash_b64`
    盐每次随机（16 字节），同一个口令两次哈希结果不同 —— 彩虹表无效。
    """
    if not isinstance(password, str) or not password:
        raise ValueError('口令不能为空')
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p,
                         dklen=32, maxmem=64 * 1024 * 1024)
    return 'scrypt$%d$%d$%d$%s$%s' % (
        n, r, p,
        base64.b64encode(salt).decode('ascii'),
        base64.b64encode(dk).decode('ascii'))


def verify_password(password: str, stored: str) -> bool:
    """校验口令。**任何异常都当作校验失败**，不向外抛。

    为什么要吞异常：stored 可能是用户手抄错的、格式是别的算法、
    或者 n/r/p 超出了 hashlib 的限制。这些都该表现为"口令不对"，
    而不是 500——500 反而告诉攻击者"这个字段被特殊处理了"。
    """
    try:
        parts = (stored or '').strip().split('$')
        if len(parts) != 6 or parts[0] != 'scrypt':
            return False
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        expect = base64.b64decode(parts[5])
        dk = hashlib.scrypt((password or '').encode('utf-8'), salt=salt,
                            n=n, r=r, p=p, dklen=len(expect),
                            maxmem=64 * 1024 * 1024)
        # 常量时间比较：避免通过响应时间逐字节猜哈希
        return hmac.compare_digest(dk, expect)
    except Exception:
        return False


# ---------------------------------------------------------------- W3：令牌表


class TokenStore:
    """服务端会话令牌表。**可撤销**是它的全部意义。

    ## 为什么不是自签 JWT
    JWT 的载荷自己带签发时间，服务端不查表 → **发出去就撤不掉**。
    要撤销就得额外维护黑名单，那还不如直接用表。
    本表让撤销 = `del`，语义没有歧义。

    ## 令牌长什么样
    `tok_<32 字节随机>`，服务端**只存它的 SHA-256**：
    即使内存被 dump，也拿不到可直接使用的令牌原文。
    比「存明文 + 比对」多一步，但代价只有一次哈希。

    ## 存哪
    内存。理由：令牌是短时的，重启即失效本来就符合预期；
    落盘反而多一个"令牌库文件被谁读了"的问题。
    """

    def __init__(self, secret: str, ttl: int = 43200, clock=None):
        self._secret = (secret or '').encode('utf-8')
        self._ttl = max(60, int(ttl or 43200))
        self._clock = clock or time.time
        self._tokens = {}          # sha256(tok) -> {token_hash, issued, expires}
        self._lock = threading.Lock()

    # ---- 签发 ----

    def issue(self) -> dict:
        """签发一个新令牌。返回 {token, expires_at, expires_in}。

        令牌本体用 `secrets.token_bytes(32)`（CSPRNG），不是 hmac 派生 ——
        随机性比"用密钥派生"更彻底：即使主密钥泄露，已发令牌也无法被
        预测出下一个。主密钥的用途是**轮换**（见 rotate）。
        """
        raw = secrets.token_bytes(32)
        tok = 'tok_' + base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
        now = self._clock()
        exp = now + self._ttl
        with self._lock:
            self._tokens[hashlib.sha256(tok.encode('utf-8')).hexdigest()] = {
                'issued': now, 'expires': exp,
            }
        return {'token': tok, 'expires_at': int(exp), 'expires_in': self._ttl}

    # ---- 校验 ----

    def verify(self, token: str) -> bool:
        """令牌是否有效。过期/不存在/已撤销一律 False。

        **不做任何日志输出**（令牌卫生第 1 条）。
        """
        if not token:
            return False
        h = hashlib.sha256(token.encode('utf-8')).hexdigest()
        now = self._clock()
        with self._lock:
            rec = self._tokens.get(h)
            if not rec:
                return False
            if rec['expires'] <= now:
                del self._tokens[h]
                return False
            return True

    # ---- 撤销与轮换 ----

    def revoke(self, token: str) -> bool:
        """撤销一个令牌。返回是否真的撤掉了。"""
        if not token:
            return False
        h = hashlib.sha256(token.encode('utf-8')).hexdigest()
        with self._lock:
            return self._tokens.pop(h, None) is not None

    def revoke_all(self) -> int:
        """撤销全部令牌（改口令时用）。返回撤销数量。"""
        with self._lock:
            n = len(self._tokens)
            self._tokens.clear()
            return n

    def rotate(self, token: str) -> dict:
        """轮换：拿旧令牌换一个新令牌，旧令牌立即失效。

        这就是"令牌短时 + 可轮换"的落地：短时令牌到期前可以换新的，
        不用重新输口令；而旧令牌**立刻作废**，即使它还没到期——
        泄露的旧令牌在轮换那一刻就废了。
        """
        if not self.verify(token):
            return {}
        self.revoke(token)
        return self.issue()

    def sweep(self) -> int:
        """清掉过期令牌。返回清理数量。调用方按需定期调。"""
        now = self._clock()
        with self._lock:
            dead = [h for h, r in self._tokens.items() if r['expires'] <= now]
            for h in dead:
                del self._tokens[h]
            return len(dead)

    def count(self) -> int:
        with self._lock:
            return len(self._tokens)


# ---------------------------------------------------------------- W4：限流


class RateLimiter:
    """按客户端 IP 的两个限流器 + 鉴权失败退避。

    ## 为什么按 IP 而不是按令牌
    令牌在**认证之后**才有；暴力猜口令发生在认证**之前**，
    那时还没有令牌。所以失败退避只能按 IP 计。
    上传限流同理：上传也要先过鉴权，但限流要挡的是"已登录的人狂传"，
    按 IP 也能挡（单用户场景下按 IP 与按令牌效果一样）。
    """

    def __init__(self, cfg: Config, clock=None):
        self.cfg = cfg
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._auth_fails = {}      # ip -> 失败次数
        self._auth_blocked_until = {}   # ip -> 解封时间戳
        self._uploads = {}          # ip -> [时间戳, ...]

    # ---- 鉴权失败退避 ----

    def auth_retry_after(self, ip: str) -> int:
        """这个 IP 现在还要等几秒才能再试。0 = 不限。

        退避时长 = `base * 2^(失败次数 - threshold)`，封顶 `max`。
        """
        now = self._clock()
        with self._lock:
            until = self._auth_blocked_until.get(ip, 0)
            if until <= now:
                return 0
            return int(until - now)

    def record_auth_failure(self, ip: str) -> int:
        """记一次鉴权失败。返回现在的退避秒数（0 = 还没到阈值）。

        ## 阈值语义
        `threshold` = **允许连续失败多少次**。第 threshold 次失败**不延迟**
        （它只是把计数打到阈值），第 threshold+1 次开始退避。

        退避时长 = `base * 2^(失败次数 - threshold - 1)`，封顶 `max`。
        即 threshold=5, base=1 → 第 6 次失败等 1s、第 7 次等 2s、第 8 次等 4s…

        ## 为什么必须封顶
        否则被打 1000 次后要等 2^990 秒 ≈ 永久封禁。封顶到 max 后，
        攻击者最多只能把一个 IP 锁住 max 秒。
        """
        now = self._clock()
        with self._lock:
            n = self._auth_fails.get(ip, 0) + 1
            self._auth_fails[ip] = n
            over = n - self.cfg.auth_fail_threshold - 1
            if over < 0:
                return 0
            # base * 2^over，封顶。over 从 0 开始（即第 threshold+1 次失败）
            delay = min(self.cfg.auth_backoff_base * (2 ** over),
                        self.cfg.auth_backoff_max)
            self._auth_blocked_until[ip] = now + delay
            return int(delay)

    def record_auth_success(self, ip: str) -> None:
        """鉴权成功 → 清空该 IP 的失败计数。"""
        with self._lock:
            self._auth_fails.pop(ip, None)
            self._auth_blocked_until.pop(ip, None)

    def auth_fail_count(self, ip: str) -> int:
        with self._lock:
            return self._auth_fails.get(ip, 0)

    # ---- 上传限流 ----

    def upload_retry_after(self, ip: str) -> int:
        """上传是否超出配额。返回需等待秒数（0 = 允许）。

        用**滑动窗口**而不是固定窗口：固定窗口的边界会让用户
        在窗口交界处一口气传 2 倍配额。滑动窗口按时间戳逐条淘汰。
        """
        now = self._clock()
        window = self.cfg.upload_window
        with self._lock:
            hits = [t for t in self._uploads.get(ip, []) if now - t < window]
            self._uploads[ip] = hits
            if len(hits) < self.cfg.upload_max:
                return 0
            # 最早的那次过期后就能再传
            oldest = min(hits)
            return max(1, int(window - (now - oldest)) + 1)

    def record_upload(self, ip: str) -> None:
        now = self._clock()
        with self._lock:
            self._uploads.setdefault(ip, []).append(now)

    def upload_count(self, ip: str) -> int:
        now = self._clock()
        window = self.cfg.upload_window
        with self._lock:
            return len([t for t in self._uploads.get(ip, []) if now - t < window])

    def reset(self) -> None:
        """清空所有计数（测试用）。"""
        with self._lock:
            self._auth_fails.clear()
            self._auth_blocked_until.clear()
            self._uploads.clear()


# ---------------------------------------------------------------- 客户端 IP


def client_ip(peer: str, forwarded_for: str = '', trusted_proxies=frozenset(),
              trusted_cidrs=(), ipaddress_module=None) -> str:
    """判定客户端 IP。

    ## 参数
        peer           直接对端地址（socket 的 peer address）
        forwarded_for  `X-Forwarded-For` 头的原始值
        trusted_proxies    受信反代的**精确**地址集合
        trusted_cidrs      受信反代的 CIDR 网段（默认空）

    ## 规则（fail-closed）
        对端**不在**受信列表里 → 直接返回 peer，**完全忽略 XFF**。
        对端在受信列表里 → 取 XFF 最左边那一节作为客户端 IP；
        XFF 缺失或为空 → 回落到 peer。

    ## 为什么不能无条件采信 XFF
    XFF 是普通请求头，客户端可以随便写。无条件采信等于
    "攻击者自己声明自己是谁"，限流与审计全部失效。
    nginx 的 `set_real_ip_from` 是同一件事：信任链只能从已知节点起算。

    ## 最左边那一节
    XFF 的格式是 `client, proxy1, proxy2`（**逗号分隔**），
    每一跳代理都会在**右边**追加自己的上游。客户端在最左边。
    只有当 peer 本身受信时才可信；否则最左边那一节也可能是伪造的。
    """
    p = (peer or '').strip()
    if not p:
        return ''
    if not _is_trusted(p, trusted_proxies, trusted_cidrs, ipaddress_module):
        return p
    if not forwarded_for:
        return p
    first = forwarded_for.split(',')[0].strip()
    return first or p


def _is_trusted(addr: str, exact=frozenset(), cidrs=(), ipaddress_module=None) -> bool:
    """地址是否在受信集合里。"""
    if addr in exact:
        return True
    if not cidrs:
        return False
    try:
        if ipaddress_module is None:
            import ipaddress as ipaddress_module
        ip = ipaddress_module.ip_address(addr)
        for cidr in cidrs:
            try:
                if ip in ipaddress_module.ip_network(cidr, strict=False):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- 令牌提取


def bearer_token(headers) -> str:
    """从请求头里取 Bearer 令牌。

    **只认 Authorization 头**，不认 query string、不认 cookie。
    理由（令牌卫生第 2 条）：URL 会被浏览器存进历史、会被 Referer 头发出去、
    会被代理与服务器日志记下。放头里才安全。
    """
    if headers is None:
        return ''
    try:
        raw = headers.get('Authorization') or headers.get('authorization') or ''
    except Exception:
        return ''
    raw = raw.strip()
    if not raw.lower().startswith('bearer '):
        return ''
    tok = raw[7:].strip()
    return tok if tok.startswith('tok_') else ''


# ---------------------------------------------------------------- 账号


class Account:
    """单账号。身份模型只有一个账号（docs §0.3），所以不需要用户表。

    口令哈希来自配置（环境变量 `QBANK_PASSWORD_HASH`）。
    改口令 = 换环境变量 + 重启；重启后旧令牌全部失效
    （内存表本来就是空的），这正好是想要的语义。
    """

    def __init__(self, cfg: Config, token_store: TokenStore = None):
        self.cfg = cfg
        self.name = (cfg.env.get('QBANK_ACCOUNT_NAME') or 'mech').strip() or 'mech'
        self.tokens = token_store or TokenStore(cfg.token_secret, cfg.token_ttl)

    def check_password(self, password: str) -> bool:
        return verify_password(password or '', self.cfg.password_hash)

    def login(self, password: str) -> dict:
        """登录。成功返回令牌信息，失败返回 {}。

        **不区分"用户名错"与"口令错"**：单账号没有用户名概念，
        失败就是失败，提示越模糊越好（减少信息泄露）。
        """
        if not self.check_password(password):
            return {}
        return self.tokens.issue()

    def authenticate(self, token: str) -> bool:
        return self.tokens.verify(token)

    def rotate(self, token: str) -> dict:
        return self.tokens.rotate(token)

    def logout(self, token: str) -> bool:
        return self.tokens.revoke(token)
