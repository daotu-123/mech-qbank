/* util.js — 题型/难度元数据与格式化助手（与旧版 index.html 内联脚本同源） */

export const QT_ICON = {
  '判断': '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>',
  '单选': '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3.6" fill="currentColor" stroke="none"/></svg>',
  '多选': '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="17" height="17" rx="3.5"/><path d="M8 12.2l2.8 2.8L16.5 9"/></svg>',
  '问答': '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M9.5 9a2.5 2.5 0 1 1 3.4 2.3c-.9.5-1.4 1-1.4 2M12 17.2v.2"/><circle cx="12" cy="12" r="9"/></svg>',
  '填空': '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M4 9h16M4 15h16M8 5v8M16 11v8"/></svg>',
  '计算': '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><rect x="5" y="3" width="14" height="18" rx="2.5"/><path d="M8.5 7.5h7M9 11.5v.2M12 11.5v.2M15 11.5v.2M9 15v.2M12 15v.2M15 15v.2"/></svg>',
  '识记': '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M4 20.5V5.5"/></svg>',
};
export const QT_COLOR = { '判断': '#7c3aed', '单选': '#2563eb', '多选': '#0891b2', '问答': '#475569',
  '填空': '#059669', '计算': '#ea580c', '识记': '#b45309' };
const QT_FALLBACK = { color: '#475569', icon: QT_ICON['问答'] };

export function qtMeta(t) {
  const key = Object.keys(QT_COLOR).find(k => String(t || '').indexOf(k) >= 0);
  return key ? { key, color: QT_COLOR[key], icon: QT_ICON[key] }
             : { key: '', color: QT_FALLBACK.color, icon: QT_FALLBACK.icon };
}

export const LV_META = { '1': { name: '入门', c: 'var(--lv1)' }, '2': { name: '基础', c: 'var(--lv2)' },
  '3': { name: '中等', c: 'var(--lv3)' }, '4': { name: '较难', c: 'var(--lv4)' }, '5': { name: '困难', c: 'var(--lv5)' } };

export function lvMeta(v) {
  const k = String(v == null ? '' : v);
  return LV_META[k] || { name: '未分级', c: '#6b7280' };
}

export function fmtDue(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = n => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* 选项字母：选项文本自带 "A." 时取其字母，否则按位次生成 */
export function optLetter(o, k, isJudge) {
  if (isJudge) return o;
  const m = String(o).match(/^([A-Ha-h])/);
  return m ? m[1].toUpperCase() : String.fromCharCode(65 + k);
}
/* 去掉选项开头的 "A." 前缀——徽章里已显示字母，避免「A A. xxx」重复 */
export function optText(o, letter) {
  const s = String(o);
  const m = s.match(/^([A-Ha-h])\s*[.、．:：]\s*/);
  return m && m[1].toUpperCase() === letter ? s.slice(m[0].length) : s;
}

export const MODE_TEXT = { sqlite: '本地全量库', static: '离线模式' };
