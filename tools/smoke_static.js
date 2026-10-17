#!/usr/bin/env node
/* smoke_static.js — 静态模式（APK / file://）纯前端 Store 无头冒烟
 *
 * 覆盖（与 tools/smoke_test.py 的断言集对齐）：
 *   判分：判断/带选项判断/单选/单选带注解/多选集合/多选分隔符/自评/待补/历史加粗/不存在题号
 *   间隔：1/3/7/15/30/60 天、封顶、答错次日回、跳过 3 天回
 *   组卷：数量 clamp、难度/题型/题库/来源过滤、只出没做过的、到期复习排序
 *   搜索：命中、空词、limit clamp、大小写、通配符字面量
 *   其他：banks/sections/cards/chapters、错题本、stats 聚合、进度导出导入 round-trip、
 *         localStorage 持久化（换实例不丢）、真实公开子集体检
 *
 * 运行：node tools/smoke_static.js
 */
'use strict';
const path = require('path');
const QBankStatic = require(path.join(__dirname, '..', 'web', 'qbank-static.js'));
const PUB = require(path.join(__dirname, '..', 'data', 'public', 'qbank.json'));

let pass = 0, fail = 0;
const fails = [];
function ok(cond, name, extra) {
  if (cond) pass++;
  else { fail++; fails.push(name + (extra !== undefined ? ' → ' + JSON.stringify(extra) : '')); }
}
function eq(a, b, name) { ok(JSON.stringify(a) === JSON.stringify(b), name, { got: a, want: b }); }

// ---- localStorage 垫片 ----
function shimStorage() {
  const m = new Map();
  return { getItem: k => (m.has(k) ? m.get(k) : null),
           setItem: (k, v) => m.set(k, String(v)),
           removeItem: k => m.delete(k), _map: m };
}
function pad(n) { return String(n).padStart(2, '0'); }
function dateAfter(days) {
  const d = new Date(Date.now() + days * 86400000);
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

// ---- 合成数据（题型与答案形态覆盖，公开子集里没有选择题）----
function synthetic() {
  const q = (id, stem, qtype, answer, options, level, bank, section_path) =>
    ({ id, bank: bank || '测试库', section_path: section_path || 'T. 测试 › T1', cat_name: '', stem,
       qtype, level: level == null ? 2 : level, options: options || [], answer,
       explanation: '', source_class: '自出' });
  return { questions: [
    q('TJ1', '水往低处流', '判断', '对'),
    q('TJ2', '地球是方的', '判断', '错'),
    q('TJ3', '带选项判断：下列说法正确的是', '判断', '对', ['A. 对', 'B. 错']),
    q('TS1', '溢流阀在液压系统中的作用', '单选', 'A', ['A. 稳压溢流', 'B. 调速', 'C. 换向']),
    q('TS2', '单选答案带注解形态', '单选', 'C（单选·标准答案）', ['A. 甲', 'B. 乙', 'C. 丙', 'D. 丁']),
    q('TM1', '多选：下列属于标准件的有', '多选', 'AB', ['A. 螺栓', 'B. 轴承', 'C. 专用轴']),
    q('TM2', '多选：顿号分隔答案', '多选', 'A、B、C', ['A. 甲', 'B. 乙', 'C. 丙', 'D. 丁']),
    q('TW1', '问答：简述数控机床的组成', '问答', '数控装置、伺服系统、机床本体'),
    q('TN1', '答案待补的题', '问答', '待补'),
    q('TB1', '历史加粗答案', '单选', '**B**', ['A. 甲', 'B. 乙', 'C. 丙']),
    q('TL3', '难度三的题', '问答', 'x', null, 3),
    q('TB2', '另一个题库的题', '问答', 'y', null, 2, '测试库B'),
    q('TU1', '别的来源的题', '问答', 'z', null, 2, '测试库', 'U. 别的 › U1'),
    q('TE1', 'Python print 函数的 sep 参数作用', '填空/问答', '指定输出分隔符'),
    q('TP1', '机械效率 100% 可能吗', '问答', '不可能'),
  ], cards: [
    { chapter: 'T. 机械制造', topic: '夹具', name: 'V形块', body: '定位外圆表面', qids: ['TS1'], source_class: '通用知识点' },
    { chapter: 'T. 机械制造', topic: '轴承', name: '深沟球轴承', body: '主要承受径向载荷', qids: [], source_class: '通用知识点' },
    { chapter: 'T. 液压', topic: '阀', name: '溢流阀', body: '稳压与溢流', qids: ['TS1'], source_class: '通用知识点' },
  ] };
}

// ================= 判分 =================
(function () {
  const S = QBankStatic.make(synthetic(), shimStorage());
  ok(S.grade('TJ1', '对').correct === true, '判断：对 → 正确');
  ok(S.grade('TJ1', '√').correct === true, '判断：√ → 正确');
  ok(S.grade('TJ1', '正确').correct === true, '判断：正确 → 正确');
  ok(S.grade('TJ1', '1').correct === true, '判断：1 → 正确');
  ok(S.grade('TJ1', '错').correct === false, '判断：错 → 错误');
  ok(S.grade('TJ1', '×').correct === false, '判断：× → 错误');
  ok(S.grade('TJ1', 'A').correct === false, '判断：无法归一的输入 → 错误');
  ok(S.grade('TJ2', '错').correct === true, '判断：答案为错，答错 → 正确');
  ok(S.grade('TJ2', '对').correct === false, '判断：答案为错，答对 → 错误');
  ok(S.grade('TJ3', 'A').correct === true, '带选项判断：点 A.对 → 正确');
  ok(S.grade('TJ3', 'B').correct === false, '带选项判断：点 B.错 → 错误');
  ok(S.grade('TS1', 'A').correct === true, '单选：A → 正确');
  ok(S.grade('TS1', 'B').correct === false, '单选：B → 错误');
  ok(S.grade('TS1', 'AB').correct === false, '单选：多字母 → 错误');
  ok(S.grade('TS1', '').correct === false, '单选：空作答 → 错误');
  ok(S.grade('TS2', 'C').correct === true, '单选带注解：C → 正确');
  ok(S.grade('TS2', 'A').correct === false, '单选带注解：A → 错误');
  ok(S.grade('TM1', 'AB').correct === true, '多选：AB → 正确');
  ok(S.grade('TM1', 'BA').correct === true, '多选：顺序无关 BA → 正确');
  ok(S.grade('TM1', 'A').correct === false, '多选：漏选 → 错误');
  ok(S.grade('TM1', 'ABC').correct === false, '多选：多选 → 错误');
  ok(S.grade('TM2', 'ABC').correct === true, '多选：无分隔 ABC → 正确');
  ok(S.grade('TM2', 'A、B、C').correct === true, '多选：顿号分隔 → 正确');
  ok(S.grade('TB1', 'B').correct === true, '历史加粗 **B**：B → 正确');
  const tw = S.grade('TW1', '数控装置');
  ok(tw.self === true && tw.correct === null, '问答：自评 correct=null', tw);
  ok(tw.answer === '数控装置、伺服系统、机床本体', '问答：返回参考答案', tw.answer);
  const tn = S.grade('TN1', 'whatever');
  ok(tn.self === true && tn.answer === '待补', '待补答案：自评且回原文', tn.answer);
  eq(S.grade('NOPE', 'x'), { error: 'not found' }, '不存在题号 → not found');
})();

// ================= 间隔与 mark =================
(function () {
  const S = QBankStatic.make(synthetic(), shimStorage());
  let m = S.mark('TS1', 'correct');
  eq([m.state, m.streak, m.wrong, m.due.slice(0, 10)], ['correct', 1, 0, dateAfter(1)], '连对 1 次 → 1 天后');
  m = S.mark('TS1', 'correct');
  eq([m.state, m.streak, m.due.slice(0, 10)], ['correct', 2, dateAfter(3)], '连对 2 次 → 3 天后');
  m = S.mark('TS1', 'correct');
  eq(m.due.slice(0, 10), dateAfter(7), '连对 3 次 → 7 天后');
  m = S.mark('TS1', 'correct');
  eq(m.due.slice(0, 10), dateAfter(15), '连对 4 次 → 15 天后');
  m = S.mark('TS1', 'correct');
  eq(m.due.slice(0, 10), dateAfter(30), '连对 5 次 → 30 天后');
  m = S.mark('TS1', 'correct');
  eq(m.due.slice(0, 10), dateAfter(60), '连对 6 次 → 60 天后');
  m = S.mark('TS1', 'correct');
  eq([m.streak, m.due.slice(0, 10)], [7, dateAfter(60)], '连对 7 次 → 封顶 60 天');
  m = S.mark('TS1', 'wrong');
  eq([m.state, m.streak, m.wrong, m.due.slice(0, 10)], ['wrong', 0, 1, dateAfter(1)], '答错 → 次日必回');
  m = S.mark('TS1', 'skip');
  eq([m.streak, m.wrong, m.due.slice(0, 10)], [0, 1, dateAfter(3)], '跳过 → 3 天后回且不错次增加');
})();

// ================= 组卷 pick =================
(function () {
  const S = QBankStatic.make(synthetic(), shimStorage());
  const all = S.pick({ n: 100 });
  eq(all.length, 15, 'pick 全量');
  eq(S.pick({ n: 0 }).length, 1, 'pick n=0 → clamp 到 1');
  eq(S.pick({ n: 999 }).length, 15, 'pick n=999 → 返回全部（不足 100）');
  eq(S.pick({ level: 3, n: 100 }).map(x => x.id), ['TL3'], 'pick 按难度过滤');
  eq(S.pick({ level: 'abc', n: 100 }).length, 15, 'pick 难度非法 → 不过滤');
  eq(S.pick({ qtype: '单选', n: 100 }).map(x => x.id).sort(), ['TB1', 'TS1', 'TS2'], 'pick 按题型过滤');
  eq(S.pick({ bank: '测试库B', n: 100 }).map(x => x.id), ['TB2'], 'pick 按题库过滤');
  eq(S.pick({ section: 'T. 测试', n: 100 }).length, 14, 'pick 按来源第一段过滤');
  eq(S.pick({ section: 'U. 别的', n: 100 }).map(x => x.id), ['TU1'], 'pick 按来源过滤（另一段）');
  eq(S.pick({ qtype: '单选', bank: '测试库B', n: 100 }).length, 0, 'pick 多条件交集为空');
  const row = all[0];
  eq(Object.keys(row).sort(), ['bank', 'cat', 'id', 'level', 'options', 'qtype', 'sec', 'source_class', 'stem'], 'pick 行字段与服务端 _pub_row 一致');
  // only_new
  S.mark('TS1', 'correct'); S.mark('TS2', 'wrong');
  const fresh = S.pick({ only_new: true, n: 100 }).map(x => x.id);
  ok(!fresh.includes('TS1') && !fresh.includes('TS2'), 'only_new 排除已作答');
  ok(fresh.includes('TJ1'), 'only_new 保留未作答');
  // review
  eq(S.pick({ review: true, n: 100 }).length, 0, '刚 mark 的题未到期 → 复习为空');
  S.import_progress({ 'TJ2': { state: 'wrong', streak: 0, wrong: 2, due: '2020-01-01T00:00:00', last_seen: '2020-01-01T00:00:00' },
                      'TM1': { state: 'wrong', streak: 0, wrong: 1, due: '2020-06-01T00:00:00', last_seen: '2020-06-01T00:00:00' } });
  eq(S.pick({ review: true, n: 100 }).map(x => x.id), ['TJ2', 'TM1'], '复习按到期时间排序');
  eq(S.pick({ review: true, n: 1 }).map(x => x.id), ['TJ2'], '复习 limit 生效');
})();

// ================= 搜索 =================
(function () {
  const S = QBankStatic.make(synthetic(), shimStorage());
  eq(S.search('溢流阀').map(x => x.id), ['TS1'], '搜索命中');
  eq(S.search('溢流').length, 1, '搜索部分命中');
  eq(S.search(''), [], '搜索空词 → 空');
  eq(S.search('溢流阀', 0).length, 1, '搜索 limit=0 → clamp 到 1');
  eq(S.search('python').map(x => x.id), ['TE1'], '搜索大小写不敏感（小写问）');
  eq(S.search('PYTHON').map(x => x.id), ['TE1'], '搜索大小写不敏感（大写问）');
  eq(S.search('%').map(x => x.id), ['TP1'], '搜索 % 按字面量（不全表命中）');
  eq(S.search('_').length, 0, '搜索 _ 按字面量');
})();

// ================= banks / sections / cards / chapters =================
(function () {
  const S = QBankStatic.make(synthetic(), shimStorage());
  const bs = S.banks();
  eq(bs.map(x => x.bank), ['测试库', '测试库B'], 'banks 按数量降序');
  eq(bs[0].n, 14, 'banks 计数');
  const ss = S.sections();
  eq(ss.map(x => x.section), ['T. 测试', 'U. 别的'], 'sections 聚合第一段');
  eq(ss[0].n, 14, 'sections 计数');
  eq(S.cards('', '', 500).length, 3, 'cards 全量');
  eq(S.cards('V形块', '', 500).map(c => c.name), ['V形块'], 'cards 关键词');
  eq(S.cards('V形块', '', 500)[0].qids, ['TS1'], 'cards qids 已解析为数组');
  eq(S.cards('', 'T. 液压', 500).map(c => c.name), ['溢流阀'], 'cards 按章节过滤');
  eq(S.cards('轴承', '', 500).length, 1, 'cards 正文也参与匹配');
  eq(S.cards('', '', 0).length, 1, 'cards limit=0 → clamp 到 1');
  eq(S.chapters(), [{ chapter: 'T. 机械制造', n: 2 }, { chapter: 'T. 液压', n: 1 }], 'chapters 聚合排序');
})();

// ================= 错题本 / stats =================
(function () {
  const S = QBankStatic.make(synthetic(), shimStorage());
  S.mark('TS1', 'wrong'); S.mark('TS1', 'wrong'); S.mark('TM1', 'wrong'); S.mark('TJ1', 'correct');
  const w = S.wrong_list();
  eq(w.map(x => [x.id, x.wrong]), [['TS1', 2], ['TM1', 1]], '错题本按错次降序');
  ok(w[0].state === 'wrong' && w[0].streak === 0, '错题本带进度字段');
  const st = S.stats();
  eq(st.mode, 'static', 'stats mode');
  eq([st.total, st.cards], [15, 3], 'stats 总量');
  eq([st.answered, st.correct, st.wrong], [3, 1, 2], 'stats 已答/掌握/错题');
  eq(st.by_level['3'], 1, 'stats 难度分布');
  eq(st.by_type, { '判断': 3, '单选': 3, '多选': 2, '问答': 6, '填空': 1 }, 'stats 题型按主类型聚合');
  eq(st.by_class, { '自出': 15 }, 'stats 来源分级');
  eq(st.intervals, [1, 3, 7, 15, 30, 60], 'stats 间隔');
  eq(st.due, 0, 'stats 待复习（刚答的未到期）');
  ok(st.today >= 3 && st.today_ok === 1, 'stats 今日已学/答对', { today: st.today, today_ok: st.today_ok });
})();

// ================= 进度导出 / 导入 / 持久化 =================
(function () {
  const storage1 = shimStorage();
  const S1 = QBankStatic.make(synthetic(), storage1);
  S1.mark('TS1', 'correct'); S1.mark('TS1', 'correct'); S1.mark('TM1', 'wrong');
  const exp = S1.export_progress();
  eq(Object.keys(exp).sort(), ['TM1', 'TS1'], '导出包含两条进度');
  eq([exp['TS1'].state, exp['TS1'].streak], ['correct', 2], '导出字段完整');
  ok(storage1._map.has('mech-qbank-progress'), 'mark 后已写入 localStorage');
  // 换存储导入（换机恢复）
  const storage2 = shimStorage();
  const S2 = QBankStatic.make(synthetic(), storage2);
  eq(S2.import_progress(exp), 2, '导入 2 条');
  eq(S2.export_progress()['TS1'].streak, 2, '导入后 streak 恢复');
  // 合并语义：导入不覆盖未出现的题，但覆盖已出现的
  S2.mark('TJ1', 'correct');
  S2.import_progress({ 'TS1': { state: 'wrong', streak: 0, wrong: 5, due: '2030-01-01T00:00:00', last_seen: '2030-01-01T00:00:00' } });
  eq([S2.export_progress()['TS1'].state, S2.export_progress()['TS1'].wrong], ['wrong', 5], '导入覆盖同题号');
  eq(S2.export_progress()['TJ1'].state, 'correct', '导入不影响未出现的题');
  eq(S2.import_progress({ '': { state: 'correct' }, 'X': null }), 0, '导入跳过坏行');
  eq(S2.import_progress({ 'BAD': { streak: 'abc', wrong: 'xyz' } }), 1, '导入一条（值非法走 clamp）');
  eq([S2.export_progress()['BAD'].streak, S2.export_progress()['BAD'].wrong], [0, 0], '导入 streak/wrong 非法 → 0');
  // 持久化：同存储新实例进度仍在
  const S3 = QBankStatic.make(synthetic(), storage2);
  eq(S3.stats().answered >= 3, true, '新实例从 localStorage 恢复进度');
  eq(S3.wrong_list().map(x => x.id), ['TS1', 'TM1'], '新实例错题本恢复');
  // 存储不可用（隐私模式垫片）不崩
  const S4 = QBankStatic.make(synthetic(), null);
  ok(S4.mark('TS1', 'correct').ok === true, 'storage 为 null 时仍可刷题（内存进度）');
})();

// ================= 真实公开子集体检 =================
(function () {
  const S = QBankStatic.make(PUB, shimStorage());
  const st = S.stats();
  eq([st.total, st.cards, st.mode], [76, 387, 'static'], '公开子集：76 题 387 卡');
  eq(S.pick({ n: 100 }).length, 76, '公开子集可全量组卷');
  ok(S.banks().length > 0 && S.sections().length > 0, '公开子集 banks/sections 非空');
  ok(S.search('Python').length >= 1, '公开子集搜索可用');
  const first = PUB.questions[0];
  const g = S.grade(first.id, '随便答');
  ok(g.self === true || typeof g.correct === 'boolean', '公开子集判分可用', g);
  const exp = S.export_progress();
  eq(S.import_progress(exp), Object.keys(exp).length, '公开子集进度 round-trip');
})();

// ================= 自定义题库导入 =================
(function () {
  const storage = shimStorage();
  const S = QBankStatic.make(synthetic(), storage);
  const base = S.stats().total;                    // 合成库 15 题
  eq(base, 15, '合成库 15 题');
  // 模板结构：_readme + questions 数组，3 道示例题
  const tpl = S.export_template();
  ok(typeof tpl._readme === 'string' && tpl._readme.length > 20, '模板含说明文字');
  eq(tpl.questions.length, 3, '模板 3 道示例题');
  ok(tpl.questions.every(q => q.id && q.stem), '模板题都有 id 与题干');
  eq([tpl.questions[0].qtype, tpl.questions[1].qtype, tpl.questions[2].qtype], ['单选', '判断', '问答'], '模板覆盖三种题型');
  // 正常导入
  const r1 = S.merge_questions(tpl.questions, 'skip');
  eq([r1.added, r1.updated, r1.skipped, r1.rejected.length], [3, 0, 0, 0], '导入 3 题全部新增');
  eq(S.stats().total, base + 3, '导入后总量 +3');
  eq(S.custom_list().length, 3, '自定义列表 3 题');
  // 题型分布：合成库原有 单选3/判断3/问答6/填空1/多选2，导入后 单选+1 判断+1 问答+1
  const tp = S.stats().by_type;
  eq([tp['单选'], tp['判断'], tp['问答']], [4, 4, 7], '导入改变题型分布');
  // 重复导入：skip 跳过 / cover 覆盖（覆盖目标选 MY003，保留 MY001 单选供后续断言）
  const r2 = S.merge_questions(tpl.questions, 'skip');
  eq([r2.added, r2.skipped], [0, 3], '重复导入 skip 全部跳过');
  const r3 = S.merge_questions([{ id: 'MY003', stem: '覆盖后的问答题', qtype: '问答', answer: '' }], 'cover');
  eq([r3.updated, r3.added], [1, 0], 'cover 覆盖已导入题');
  eq(S.stats().total, base + 3, '覆盖不增加总量');
  eq(S.grade('MY003', '任意答案').self, true, '覆盖后的 MY003 走自评');
  // 撞内置题号一律拒绝（防止覆盖内置题）
  const r4 = S.merge_questions([{ id: 'TS1', stem: '伪装题' }], 'skip');
  eq([r4.rejected.length, r4.added], [1, 0], '撞内置 id 拒绝');
  ok(r4.rejected[0].err.indexOf('重复') >= 0, '拒绝原因说明 id 重复');
  const r4b = S.merge_questions([{ id: 'TS1', stem: '伪装题' }], 'cover');
  eq([r4b.rejected.length, r4b.updated], [1, 0], 'cover 模式也拒绝撞内置 id');
  eq(S.grade('TS1', 'A').correct, true, '内置题未被覆盖（TS1 仍可正常判分）');
  // 字段不全逐条拒绝并列出原因
  const r5 = S.merge_questions([{ id: 'X1' }, { stem: '无 id' }, { id: '', stem: '空 id' }, null, '字符串'], 'skip');
  eq(r5.rejected.length, 5, '5 条坏数据全部拒绝');
  eq(r5.added, 0, '坏数据不入库');
  // level 归一化：字符串 "3" → 数字 3；越界 9 → 夹紧 5；空 → null
  S.merge_questions([
    { id: 'LV1', stem: '字符串难度', level: '3' },
    { id: 'LV2', stem: '越界难度', level: 9 },
    { id: 'LV3', stem: '空难度', level: '' },
  ], 'skip');
  const byId = {};
  S.custom_list().forEach(q => byId[q.id] = q);
  eq(byId['LV1'].level, 3, 'level 字符串归一为数字');
  eq(byId['LV2'].level, 5, 'level 越界夹紧到 5');
  eq(byId['LV3'].level, null, 'level 空串 → null（未分级）');
  // 导入题参与筛选与判分
  eq(S.pick({ n: 10, qtype: '单选' }).some(q => q.id === 'MY001'), true, '导入题可按题型筛出');
  eq(S.pick({ n: 10, level: 5 }).some(q => q.id === 'LV2'), true, '导入题可按难度筛出');
  eq(S.pick({ n: 10, bank: '我的题库' }).length, 6, '导入题默认归入「我的题库」（MY001-3 + LV1-3）');
  eq(S.grade('MY001', 'A').correct, true, '导入的单选可判分（A 为正确答案）');
  eq(S.grade('MY001', 'B').correct, false, '导入的单选判错');
  eq(S.grade('MY002', '对').correct, true, '导入的判断题可判分');
  // 清空
  eq(S.clear_custom(), 6, '清空返回自定义题数');
  eq(S.stats().total, base, '清空后恢复内置总量');
  eq(S.custom_list().length, 0, '清空后自定义列表为空');
  // 持久化：自定义题写入 localStorage，换实例仍在
  const S2 = QBankStatic.make(synthetic(), storage);
  eq(S2.custom_list().length, 0, '清空后新实例无自定义题（已持久化清空）');
  S2.merge_questions(tpl.questions, 'skip');
  const S3 = QBankStatic.make(synthetic(), storage);
  eq(S3.stats().total, base + 3, '自定义题持久化：换实例仍在');
  eq(S3.pick({ n: 10, qtype: '单选' }).some(q => q.id === 'MY001'), true, '持久化的导入题可筛出');
  // 存储损坏/不可读不崩
  const badStore = { getItem: () => '{坏 json', setItem: () => {}, removeItem: () => {} };
  const S5 = QBankStatic.make(synthetic(), badStore);
  eq(S5.stats().total, 15, 'localStorage 内容损坏时降级为空自定义库');
  ok(S5.merge_questions(tpl.questions, 'skip').added === 3, '存储损坏时仍可导入（内存生效）');
  // 空数组/非数组入参
  eq(S.merge_questions(null, 'skip').rejected.length, 0, 'null 入参不拒绝不崩溃');
  eq(S.merge_questions([], 'skip').added, 0, '空数组无新增');
})();

// ================= 学习打卡 =================
(function () {
  const storage = shimStorage();
  const S = QBankStatic.make(synthetic(), storage);
  const today = new Date();
  const tk = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  const daysAgo = n => tk(new Date(today.getFullYear(), today.getMonth(), today.getDate() - n)) + 'T10:00:00';
  // 1. 空状态
  const c0 = S.checkin();
  eq([c0.streak, c0.total_days, c0.checked_today, c0.today_n], [0, 0, false, 0], '打卡：空状态');
  eq(c0.today, tk(today), '打卡：today 字段');
  eq(c0.days.length, new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate(), '打卡：当月格数');
  eq(c0.lead, (new Date(today.getFullYear(), today.getMonth(), 1).getDay() + 6) % 7, '打卡：周一起始前导格');
  eq(c0.days.filter(c => c.today).length, 1, '打卡：today 标记唯一');
  // 2. 答题自动打卡
  S.mark('TS1', 'correct');
  S.mark('TM1', 'wrong');
  const c1 = S.checkin();
  eq([c1.checked_today, c1.today_n, c1.today_ok, c1.streak, c1.total_days], [true, 2, 1, 1, 1], '答题后自动打卡（n=2 ok=1）');
  // 3. 历史连续：昨天+前天 → streak=3（今天+昨天+前天）
  S.import_progress({
    '__y__': { state: 'correct', streak: 1, wrong: 0, due: null, last_seen: daysAgo(1) },
    '__d2__': { state: 'correct', streak: 1, wrong: 0, due: null, last_seen: daysAgo(2) },
  });
  eq(S.checkin().streak, 3, '连续三天 streak=3');
  eq(S.checkin().total_days, 3, '累计 3 天');
  // 4. 手动打卡（今天已答题 → 不记 manual）
  const m1 = S.manual_checkin();
  eq([m1.manual_count, m1.streak], [0, 3], '已答题时手动打卡不重复记');
  const m2 = S.manual_checkin();
  eq(m2.manual_count, 0, '重复手动打卡幂等');
  // 5. 断天：清掉昨天记录（重新 import 覆盖为空进度不行，用新实例）
  const S2 = QBankStatic.make(synthetic(), shimStorage());
  S2.import_progress({
    '__t__': { state: 'correct', streak: 1, wrong: 0, due: null, last_seen: daysAgo(0) },
    '__d2b__': { state: 'correct', streak: 1, wrong: 0, due: null, last_seen: daysAgo(2) },
    '__d3b__': { state: 'correct', streak: 1, wrong: 0, due: null, last_seen: daysAgo(3) },
  });
  const c5 = S2.checkin();
  eq([c5.streak, c5.total_days], [1, 3], '昨天断天 → streak=1，累计仍 3');
  // 6. 今天未打卡：只有昨天+前天 → streak 算截至昨天 = 2
  const S3 = QBankStatic.make(synthetic(), shimStorage());
  S3.import_progress({
    '__y3__': { state: 'correct', streak: 1, wrong: 0, due: null, last_seen: daysAgo(1) },
    '__d2c__': { state: 'correct', streak: 1, wrong: 0, due: null, last_seen: daysAgo(2) },
  });
  const c6 = S3.checkin();
  eq([c6.checked_today, c6.streak, c6.total_days], [false, 2, 2], '今天未打 → 显示截至昨天连续 2 天');
  // 7. 纯手动补签（今天无答题）→ manual_count=1，streak=1
  const storage4 = shimStorage();
  const S4 = QBankStatic.make(synthetic(), storage4);
  const c7 = S4.manual_checkin();
  eq([c7.checked_today, c7.manual_count, c7.streak, c7.total_days], [true, 1, 1, 1], '纯手动补签生效');
  const c8 = S4.checkin();
  eq(c8.days.find(c => c.today).manual, true, '日历今天标记为手动补签');
  // 8. 历史月查询
  const c9 = S4.checkin('2026-01');
  eq([c9.month, c9.days.length, c9.lead], ['2026-01', 31, 3], '历史月 2026-01：31 天、周一起始 lead=3');
  const c10 = S4.checkin('bad-month');
  eq(c10.month, c10.today.slice(0, 7), '非法 month 回落当月');
  // 9. 持久化：手动补签换实例仍在（与 S4 同存储）
  const S5 = QBankStatic.make(synthetic(), storage4);
  eq(S5.checkin().manual_count, 1, '手动补签持久化（同存储新实例）');
  eq(S5.checkin().streak, 1, '持久化的补签计入连续天数');
  // 10. 导出/导入 round-trip
  const exp = S5.export_checkin();
  const S6 = QBankStatic.make(synthetic(), shimStorage());
  eq(S6.import_checkin(exp), 1, '打卡导入返回条数');
  eq(S6.import_checkin(exp), 0, '重复导入跳过');
  eq(S6.checkin().manual_count, 1, '导入后 manual 生效');
  eq(S6.import_checkin(null), 0, 'null 导入不炸');
  eq(S6.import_checkin({ manual: ['2026-13-99', 'x'] }), 0, '非法日期拒绝');
  // 11. 存储损坏降级
  const badStore = { getItem: k => k === 'mech-qbank-checkin' ? '{坏' : null, setItem: () => {}, removeItem: () => {} };
  const S7 = QBankStatic.make(synthetic(), badStore);
  eq(S7.checkin().manual_count, 0, 'checkin 存储损坏降级为空');
  ok(S7.manual_checkin().checked_today === true, '存储损坏时手动打卡仍可用（内存）');
})();

// ---- 汇总 ----
console.log('静态模式冒烟（qbank-static.js）：通过 %d ｜ 失败 %d', pass, fail);
if (fail) { fails.forEach(f => console.log('  ✗ ' + f)); process.exit(1); }
