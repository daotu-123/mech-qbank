/* api.js — 前后端唯一通道：在线模式打 /api/v1（FastAPI），file:// 走静态 Store
 *
 * 分流规则与旧版 index.html 一致：location.protocol === 'file:' 时用 public/qbank-data.js
 * 内联的数据 + 纯前端 Store（localStorage）；否则走 HTTP。UI 层不关心差异。
 *
 * RESTful 方法语义（与 app/api.py 一致）：
 *   GET    /questions?…            组卷/复习/搜索（集合查询参数）
 *   POST   /questions/{id}/grade   判分
 *   GET    /questions/{id}         单题
 *   GET    /questions/{id}/note    读备注
 *   PUT    /questions/{id}/note    写备注（空串 = 删除）
 *   DELETE /questions/{id}/note    删备注
 *   GET    /notes                  备注列表
 *   GET    /progress               导出进度
 *   POST   /progress               导入进度
 *   PUT    /progress/{id}          记录结果（mark）
 *   POST   /import_questions       批量导入自定义题（PDF 摘录编辑器保存）
 *   GET    /custom                 自定义题列表
 *   DELETE /custom/{id}            删除一道自定义题
 *   GET    /wrong /stats /banks /sections /chapters /cards
 *   GET    /checkins?month=        打卡日历
 *   POST   /checkins               手动补签
 *   POST   /system/shutdown        退出服务（仅在线）
 */
import { ElMessage } from 'element-plus'
import { make as makeStaticStore } from './store/static-store.js'
import { authHeaders, handleUnauthorized, isAuthRequired } from './auth'

export const STATIC = location.protocol === 'file:'

function safeStorage() { try { return localStorage; } catch (e) { return null; } }

export const SAPI = STATIC
  ? makeStaticStore(window.QBANK_DATA || { questions: [], cards: [] }, safeStorage())
  : null

const V1 = '/api/v1'

/** 统一请求入口。body 为空默认 GET，有 body 默认 POST，也可显式传 method（PUT/DELETE）。 */
export async function api(path, body, method) {
  if (STATIC) {
    try { return staticRoute(path, body, method || (body ? 'POST' : 'GET')); }
    catch (e) { ElMessage.error('本地处理失败：' + e.message); return null; }
  }
  const m = method || (body ? 'POST' : 'GET')
  const payload = m === 'GET' ? undefined
    : { method: m, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }
  // 带令牌（公网模式）。本机模式 authHeaders() 返回空对象，行为不变。
  const withAuth = (opts) => ({
    ...opts,
    headers: { ...(opts && opts.headers ? opts.headers : {}), ...authHeaders() },
  })
  try {
    let r = await fetch(V1 + path, payload === undefined ? undefined : withAuth(payload))
    // 401 → 先试静默刷新（令牌可能只是过期了），成功就重试一次原请求
    if (r.status === 401 && isAuthRequired()) {
      if (await handleUnauthorized()) {
        r = await fetch(V1 + path, payload === undefined ? undefined : withAuth(payload))
      }
    }
    if (!r.ok) {
      let d = 'HTTP ' + r.status;
      try {
        const j = await r.json();
        // 统一错误格式（app/errors.py）：{"error":{"code","message"}}
        d = (j.error && j.error.message) || j.detail || d;
      } catch (e) { /* 非 JSON 错误体 */ }
      ElMessage.error(d);
      return null;
    }
    return await r.json();
  } catch (e) { ElMessage.error('请求失败：' + e.message); return null; }
}

/* RESTful 路径 → 静态 Store 调用（与 app/api.py 的路由一一对应） */
function staticRoute(path, body, method) {
  const u = new URL('http://static' + path), p = u.pathname, q = u.searchParams, S = SAPI;
  const mq = /^\/questions\/([^/]+)(?:\/(note|grade))?$/.exec(p);
  const mp = /^\/progress\/([^/]+)$/.exec(p);
  if (p === '/questions') {
    if (q.get('q')) return { items: S.search(q.get('q'), q.get('limit') || 50) };
    return { items: S.pick({ n: q.get('limit') || 10, level: q.get('level'), qtype: q.get('qtype'),
      bank: q.get('bank'), section: q.get('section'), only_new: q.get('only_new') === '1',
      review: q.get('review') === '1' }) };
  }
  if (mq && mq[2] === 'grade') return S.grade(mq[1], (body || {}).given || '');
  if (mq && mq[2] === 'note') {
    if (method === 'DELETE') return S.set_note(mq[1], '');
    if (body) return S.set_note(mq[1], body.note || '');
    for (const it of S.notes_list()) if (it.id === mq[1]) return it;
    return { id: mq[1], note: '', updated_at: '' };
  }
  if (mq && !mq[2]) return { item: S.question(mq[1]) };
  if (mp) return S.mark(mp[1], (body || {}).result || 'skip');
  if (p === '/notes') return { items: S.notes_list() };
  if (p === '/progress') {
    if (body) return { ok: true, imported: S.import_progress(body.progress || {}) };
    return { exported_at: new Date().toISOString(), mode: 'static', progress: S.export_progress() };
  }
  if (p === '/wrong') return { items: S.wrong_list(q.get('limit') || 200) };
  if (p === '/stats') return S.stats();
  if (p === '/banks') return { items: S.banks() };
  if (p === '/sections') return { items: S.sections() };
  if (p === '/chapters') return { items: S.chapters() };
  if (p === '/cards') return { items: S.cards(q.get('q') || '', q.get('chapter') || '', q.get('limit') || 500) };
  if (p === '/checkins') return body ? S.manual_checkin() : S.checkin(q.get('month') || '');
  if (p === '/template') return S.export_template();
  if (p === '/import_questions') {
    const r = S.merge_questions((body || {}).questions || [], (body || {}).mode || 'skip');
    return { ok: true, added: r.added, updated: r.updated, skipped: r.skipped, rejected: r.rejected };
  }
  if (p === '/custom') return { items: S.custom_list() };
  const md = /^\/custom\/([^/]+)$/.exec(p);
  if (md) {
    if (method === 'DELETE') return S.remove_custom(decodeURIComponent(md[1]))
      ? { ok: true, removed: true } : { error: 'not found' };
    return { error: 'not found' };
  }
  return { error: 'not found' };
}

/* 在线模式才有的操作：退出服务（RESTful：POST /api/v1/system/shutdown） */
export async function quitApp() {
  if (STATIC) return;
  try { await fetch(V1 + '/system/shutdown', { method: 'POST' }); }
  catch (e) { /* 服务先一步断开，也算退出 */ }
}
