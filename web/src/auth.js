/* auth.js — 前端鉴权层（公网模式 W3）
 *
 * ## 令牌存哪（令牌卫生第 2 条的直接后果）
 * ---------------------------------------------------------------------------
 * **绝对不进 localStorage。** 理由不是洁癖：
 *   · localStorage 里的东西**跨会话永存**，也**能被任何同源脚本读到**。
 *     一次 XSS（哪怕是本项目自己将来引入的一个第三方脚本的漏洞）就等于
 *     令牌长期泄露。
 *   · 令牌是短时的（默认 12 小时），本来就不需要长期存储。
 *
 * 实际方案：
 *   · **access token**（短时，默认 12h）→ 只放**内存**（模块级变量）。
 *     刷新页面就没了 —— 这是特性不是缺陷。
 *   · **refresh token**（同一张令牌表里，短时）→ 放 **sessionStorage**。
 *     sessionStorage 的生命周期 = 标签页会话，关掉就没了；
 *     而且它**不是** localStorage，不跨会话永存。
 *   · 页面加载时：用 sessionStorage 里的 token 调 /auth/refresh 换新的
 *     access token（顺带把 sessionStorage 里的也换掉 → 轮换）。
 *
 * ## 令牌绝不出现在 URL
 * 只走 `Authorization: Bearer` 头。URL 会被浏览器历史、Referer 头、
 * 代理与服务器日志记下 —— 那等于把令牌抄送给一堆第三方。
 *
 * ## 本机模式（默认）完全不受影响
 * `/auth/status` 返回 `auth_required: false` → 本模块不拦截任何请求，
 * 前端行为与 M2 之前**一字不差**。
 */

import { ElMessage } from 'element-plus'

const V1 = '/api/v1'

/** 是否需要登录。null = 还没问过服务端。 */
let authRequired = null
/** access token：只在内存里。 */
let accessToken = ''
/** 正在等 refresh 的 promise —— 并发请求共用一次刷新，别打一串 refresh。 */
let refreshing = null
/** 401 时通知 UI（由 App.vue 挂上）。 */
let onUnauthorized = null

const SESSION_KEY = 'qbank.auth.token'

function safeSession() {
  try { return sessionStorage; } catch (e) { return null; }
}

/** 挂上「未授权」的回调（App.vue 用它弹出登录框）。 */
export function setUnauthorizedHandler(fn) { onUnauthorized = fn }

/** 是否需要登录。null 表示未知（还没问过）。 */
export function isAuthRequired() { return authRequired === true }

/** 当前有没有可用令牌。 */
export function hasToken() { return !!accessToken }

/**
 * 问服务端「要不要登录」。
 * @returns {Promise<{auth_required:boolean, authenticated:boolean}>}
 */
export async function checkAuth() {
  if (authRequired === false) return { auth_required: false, authenticated: true }
  try {
    const r = await fetch(V1 + '/auth/status', { headers: authHeaders() })
    if (!r.ok) return { auth_required: false, authenticated: true }
    const d = await r.json()
    authRequired = !!d.auth_required
    if (!authRequired) return d
    if (!d.authenticated) {
      // 服务端要鉴权但我们没令牌 → 试着静默刷新一次
      const ok = await refreshToken()
      return { auth_required: true, authenticated: ok }
    }
    return d
  } catch (e) {
    // 网络不通：不该把用户锁在登录页外面（也可能是本机离线）
    return { auth_required: false, authenticated: true }
  }
}

/** 请求头：带 access token（若有）。 */
export function authHeaders() {
  return accessToken ? { Authorization: 'Bearer ' + accessToken } : {}
}

/** 登录。成功返回 true。 */
export async function login(password) {
  try {
    const r = await fetch(V1 + '/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password || '' }),
    })
    if (!r.ok) {
      let msg = '登录失败'
      try {
        const d = await r.json()
        msg = (d.error && d.error.message) || d.detail || msg
        if (r.status === 429) {
          const ra = r.headers.get('retry-after')
          msg = ra ? '尝试过于频繁，请 ' + ra + ' 秒后再试' : '尝试过于频繁，请稍后再试'
        }
      } catch (e) { /* 非 JSON */ }
      ElMessage.error(msg)
      return false
    }
    const d = await r.json()
    if (d.auth_required) {
      authRequired = true
      setToken(d.token)
    }
    return true
  } catch (e) {
    ElMessage.error('连不上服务器：' + e.message)
    return false
  }
}

/** 登出：撤销服务端令牌 + 清本地。 */
export async function logout() {
  try {
    await fetch(V1 + '/auth/logout', { method: 'POST', headers: authHeaders() })
  } catch (e) { /* 服务不可达也照样清本地 */ }
  clearToken()
  authRequired = null
}

/**
 * 用 sessionStorage 里的令牌换一个新的。
 * 并发调用共用一次请求（refresh 不是幂等的，多打几次会互相把令牌作废）。
 */
function refreshToken() {
  if (refreshing) return refreshing
  const s = safeSession()
  const old = s ? s.getItem(SESSION_KEY) : ''
  if (!old) return Promise.resolve(false)
  refreshing = (async () => {
    try {
      const r = await fetch(V1 + '/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: old }),
      })
      if (!r.ok) {
        clearToken()
        return false
      }
      const d = await r.json()
      setToken(d.token)
      return true
    } catch (e) {
      return false
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

/** 存令牌：内存 + sessionStorage（**不是 localStorage**）。 */
function setToken(tok) {
  accessToken = tok || ''
  const s = safeSession()
  try {
    if (accessToken) s && s.setItem(SESSION_KEY, accessToken)
    else s && s.removeItem(SESSION_KEY)
  } catch (e) { /* 隐私模式：只有内存能用 */ }
}

function clearToken() {
  accessToken = ''
  const s = safeSession()
  try { s && s.removeItem(SESSION_KEY) } catch (e) { /* ignore */ }
}

/**
 * 处理 401：先试静默刷新，再失败就通知 UI 弹登录框。
 * @returns {boolean} true = 已恢复（调用方可以重试原请求）
 */
export async function handleUnauthorized() {
  if (!authRequired) return false
  const ok = await refreshToken()
  if (ok) return true
  if (onUnauthorized) onUnauthorized()
  return false
}
