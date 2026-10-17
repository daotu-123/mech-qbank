/* qbank-static.js — mech-qbank 纯前端静态 Store（APK / file:// 离线模式）
 *
 * 存在意义：WebView 里 file:// 页面不能 fetch 本地 JSON（CORS 拦），
 * 于是数据由 qbank-data.js 内联成 window.QBANK_DATA，本文件提供与
 * app/server.py 同源的 Store（grade/mark/pick/search/cards/stats/进度存取），
 * 进度写 localStorage。UI 层 index.html 的 api() 在 file:// 下路由到这里。
 *
 * 判分与间隔逻辑照抄 server.py（2026-10-16 版），改动必须两边同步：
 *   · 判断题：对/正确/√/T/1 → '对'，错/×/F/0 → '错'；带 A.对/B.错 选项时按选项文本映射
 *   · 选择题：答案为纯字母串时集合严格相等（多选必须全选对）；历史数据兜底取开头字母串
 *   · 其余（问答/计算/长答案）自评：correct=null
 *   · 间隔：答对按 1/3/7/15/30/60 天（streak 封顶 6 档），答错次日必回，跳过 3 天后回
 *
 * 自定义题库导入（2026-10-17 加）：merge_questions() 把用户 JSON 并入内存题库，
 * 持久化在 localStorage 的 mech-qbank-custom 键；id 撞内置题一律拒绝，防覆盖。
 *
 * 零依赖；Node 下可 require 做无头测试（tools/smoke_static.js）。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;  // Node 测试
  root.QBankStatic = api;                                                    // 浏览器/WebView
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var INTERVALS = [1, 3, 7, 15, 30, 60];   // 与 server.py INTERVALS 一致
  var WRONG_BACK = 1;                       // 答错次日必回
  var SKIP_BACK = 3;                        // 跳过 3 天后回
  var PROG_KEY = 'mech-qbank-progress';     // localStorage 键（进度按题号存）
  var CUSTOM_KEY = 'mech-qbank-custom';     // localStorage 键（导入的自定义题）
  var CKEY = 'mech-qbank-checkin';          // localStorage 键（手动打卡日期，答题打卡从 progress 聚合）
  var TRUE_SET = { '对': 1, '正确': 1, '是': 1, '√': 1, '✓': 1, 'true': 1, 't': 1, 'yes': 1, 'y': 1, '1': 1 };
  var FALSE_SET = { '错': 1, '错误': 1, '否': 1, '×': 1, '✗': 1, 'x': 1, 'false': 1, 'f': 1, 'no': 1, '0': 1, '2': 1 };

  function pad(n) { return String(n).padStart(2, '0'); }
  function isoLocal(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
           'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  function nowIso() { return isoLocal(new Date()); }
  function isoPlusDays(days) { return isoLocal(new Date(Date.now() + days * 86400000)); }
  function todayStr() { var d = new Date(); return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function clampInt(v, def, lo, hi) {
    var n = parseInt(v, 10);
    if (isNaN(n)) return def;
    return Math.max(lo, Math.min(hi, n));
  }
  function uniq(arr) {
    var s = {}, out = [];
    (arr || []).forEach(function (x) { if (!s[x]) { s[x] = 1; out.push(x); } });
    return out;
  }
  function sameSet(a, b) {
    if (a.length !== b.length) return false;
    var s = {}; a.forEach(function (x) { s[x] = 1; });
    return b.every(function (x) { return s[x]; });
  }
  function normJudge(s) {
    s = String(s == null ? '' : s).trim().toLowerCase().replace(/[。.]+$/, '');
    if (TRUE_SET[s]) return '对';
    if (FALSE_SET[s]) return '错';
    return null;
  }
  function sec0(sp) { return String(sp || '').split(' › ')[0].trim(); }   // server.py：section_path 第一段
  function monthStr(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1); }
  function dayKey(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
  // 真实日期校验：格式对不算数，2026-13-99 这种要 round-trip 拒绝（与 server 的 try/except 同效）
  function validDay(d) {
    if (typeof d !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(d)) return false;
    var p = d.split('-'), dt = new Date(+p[0], +p[1] - 1, +p[2]);
    return dt.getFullYear() === +p[0] && dt.getMonth() === +p[1] - 1 && dt.getDate() === +p[2];
  }

  function make(DATA, storage) {
    DATA = DATA || {};
    var builtin = DATA.questions || [];
    var cardsList = DATA.cards || [];
    var builtinById = {};
    builtin.forEach(function (q) { builtinById[q.id] = q; });

    // ---- 自定义题库（导入的用户题；与内置合并后参与全部查询）----
    function loadCustom() {
      try {
        var raw = storage && storage.getItem(CUSTOM_KEY);
        var arr = raw ? JSON.parse(raw) : [];
        return Array.isArray(arr) ? arr : [];
      } catch (e) { return []; }
    }
    function saveCustom() {
      try { storage && storage.setItem(CUSTOM_KEY, JSON.stringify(custom)); } catch (e) { /* 存满不打断 */ }
    }
    var custom = loadCustom();
    var questions = builtin.concat(custom);   // 导入时拒绝撞内置 id，两段无重叠
    var byId = {};
    function reindex() {
      byId = {};
      questions.forEach(function (q) { byId[q.id] = q; });
    }
    reindex();

    function normCustomQ(raw) {
      if (!raw || typeof raw !== 'object') return { err: '不是合法题目对象' };
      var id = String(raw.id == null ? '' : raw.id).trim();
      var stem = String(raw.stem == null ? '' : raw.stem).trim();
      if (!id) return { err: '缺少 id（题号）' };
      if (!stem) return { err: '缺少题干（stem）：' + id };
      if (builtinById[id]) return { err: 'id 与内置题重复，换一个 id：' + id };
      return { ok: true, q: {
        id: id, stem: stem,
        qtype: String(raw.qtype == null ? '' : raw.qtype).trim() || '问答',
        level: (raw.level == null || raw.level === '') ? null : clampInt(raw.level, 0, 1, 5),
        options: Array.isArray(raw.options) ? raw.options.map(function (x) { return String(x); }) : [],
        answer: String(raw.answer == null ? '' : raw.answer),
        explanation: String(raw.explanation == null ? '' : raw.explanation),
        bank: String(raw.bank == null ? '' : raw.bank).trim() || '我的题库',
        section_path: String(raw.section_path == null ? '' : raw.section_path).trim(),
        cat_name: String(raw.cat_name == null ? '' : raw.cat_name).trim(),
        source_class: '自定义'
      } };
    }

    // 合并导入：mode 'skip'（默认，撞已导入题号跳过）/ 'cover'（覆盖）。
    // 返回 {added, updated, skipped, rejected:[{id, err}]}，任一拒绝都列出来，不静默。
    function mergeQuestions(list, mode) {
      mode = mode === 'cover' ? 'cover' : 'skip';
      var res = { added: 0, updated: 0, skipped: 0, rejected: [] };
      var changed = false;
      (Array.isArray(list) ? list : []).forEach(function (raw) {
        var rid = raw && typeof raw === 'object' ? String(raw.id == null ? '' : raw.id).trim() : '';
        var r = normCustomQ(raw);
        if (r.err) { res.rejected.push({ id: rid || '（无 id）', err: r.err }); return; }
        var hit = -1;
        for (var i = 0; i < custom.length; i++) if (custom[i].id === r.q.id) { hit = i; break; }
        if (hit >= 0) {
          if (mode === 'cover') { custom[hit] = r.q; res.updated++; changed = true; }
          else res.skipped++;
        } else {
          custom.push(r.q); res.added++; changed = true;
        }
      });
      if (changed) {
        questions = builtin.concat(custom);
        reindex();
        saveCustom();
      }
      return res;
    }

    function customList() {
      return custom.map(function (q) {
        return { id: q.id, qtype: q.qtype, level: q.level, stem: q.stem, bank: q.bank };
      });
    }

    function clearCustom() {
      var n = custom.length;
      custom = [];
      questions = builtin.concat(custom);
      reindex();
      saveCustom();
      return n;
    }

    function exportTemplate() {
      return {
        _readme: 'mech-qbank 题库导入模板。questions 数组里每行一题：id 必填且不能与内置题重复；qtype 建议取 判断/单选/多选/问答/填空/计算；level 为 1-5 难度（可省）；有 options 时按选择题渲染，answer 写正确选项字母（如 "A" 或 "AB"）；answer 留空表示答案待补（自评练习）。bank 可用来分组，导入后出现在「题库」筛选里。',
        questions: [
          { id: 'MY001', qtype: '单选', level: 2, bank: '我的题库', section_path: '机械制图',
            stem: '表面粗糙度评定参数中，最常用的是（　）。',
            options: ['A. Ra', 'B. Rx', 'C. Ry'], answer: 'A',
            explanation: 'Ra 为轮廓算术平均偏差，是国标最常用的评定参数。' },
          { id: 'MY002', qtype: '判断', level: 1, bank: '我的题库', section_path: '机械制图',
            stem: '游标卡尺的读数由主尺整数部分与游标小数部分相加得到。',
            options: [], answer: '对', explanation: '' },
          { id: 'MY003', qtype: '问答', level: 3, bank: '我的题库', section_path: '液压与气动',
            stem: '简述先导式溢流阀相对于直动式溢流阀的主要优点。',
            options: [], answer: '', explanation: '先导式先导阀先动作、主阀口后开启，压力更稳定，适合高压大流量。' }
        ]
      };
    }

    // ---- 进度（localStorage；隐私模式/存满时退化为内存，本会话仍可刷题）----
    function loadProg() {
      try {
        var raw = storage && storage.getItem(PROG_KEY);
        return raw ? (JSON.parse(raw) || {}) : {};
      } catch (e) { return {}; }
    }
    var prog = loadProg();
    function saveProg() {
      try { storage && storage.setItem(PROG_KEY, JSON.stringify(prog)); } catch (e) { /* 不打断刷题 */ }
    }

    function pubRow(q) {
      return { id: q.id, stem: q.stem, qtype: q.qtype, level: q.level,
               options: q.options || [], cat: q.cat_name, bank: q.bank,
               source_class: q.source_class, sec: sec0(q.section_path) };
    }

    // ---- 判分（照抄 server.py Store.grade）----
    function grade(qid, given) {
      var q = byId[qid];
      if (!q) return { error: 'not found' };
      var ansRaw = (q.answer || '').trim();
      if (!ansRaw || ansRaw.indexOf('待补') >= 0) {
        return { answer: ansRaw || '（答案待补）', explanation: q.explanation, correct: null, self: true };
      }
      var ans = ansRaw.replace(/\*\*/g, '').trim();     // 兜底剥离历史数据的加粗标记
      var qt = q.qtype || '';
      var opts = q.options || [];
      // 1) 判断题：答案是对/错，且（无选项 或 题型标注判断）
      var j = normJudge(ans);
      if (j && (!opts.length || qt.indexOf('判断') >= 0)) {
        var gj = normJudge(given);
        if (gj === null && opts.length) {
          // 'A. 对' / 'B. 错' 形式：按选项文本映射用户点的字母
          var m = /^\s*([A-H])/.exec(String(given || '').toUpperCase());
          if (m) {
            var idx = m[1].charCodeAt(0) - 65;
            if (idx >= 0 && idx < opts.length) {
              var t = String(opts[idx]);
              if (/对|正确|√|✓/.test(t)) gj = '对';
              else if (/错|错误|×|✗/.test(t)) gj = '错';
            }
          }
        }
        return { answer: j, explanation: q.explanation, correct: gj === j };
      }
      // 2) 选择题：答案是纯字母串（A / AB / A、B、C），且题型是选择类或带选项
      var letters = null;
      if (/^[A-H][A-H、,，\s\/]{0,15}$/.test(ans)) letters = uniq(ans.toUpperCase().match(/[A-H]/g));
      if (!letters && /单选|多选|选择|选型/.test(qt)) {
        // 历史数据兜底：`A（单选·标准答案…）`这类剥了 ** 仍带注解的，取开头字母串
        var m2 = /^([A-H](?:\s*[、,，/]?\s*[A-H])*)\s*[（(：:．.]/.exec(ans);
        if (m2) letters = uniq(m2[1].toUpperCase().match(/[A-H]/g));
      }
      if (letters && (/单选|多选|选择|选型/.test(qt) || opts.length)) {
        var g = uniq(String(given || '').toUpperCase().match(/[A-H]/g));
        return { answer: ans, explanation: q.explanation, correct: g.length > 0 && sameSet(g, letters) };
      }
      // 3) 其余（问答/计算/长文本答案）自评
      return { answer: ans, explanation: q.explanation, correct: null, self: true };
    }

    // ---- 进度（照抄 server.py Store.mark）----
    function mark(qid, result) {
      var st = prog[qid] || { state: 'new', streak: 0, wrong: 0 };
      var streak = st.streak || 0, wrong = st.wrong || 0, state = st.state || 'new';
      var days;
      if (result === 'correct') {
        streak += 1; state = 'correct';
        days = INTERVALS[Math.min(streak, INTERVALS.length) - 1];   // 1→1天 2→3天 … 6+→60天
      } else if (result === 'wrong') {
        streak = 0; wrong += 1; state = 'wrong'; days = WRONG_BACK;  // 次日必回
      } else {
        days = SKIP_BACK;                                            // 跳过 3 天后回
      }
      var due = isoPlusDays(days);
      prog[qid] = { state: state, streak: streak, wrong: wrong, due: due, last_seen: nowIso() };
      saveProg();
      return { ok: true, state: state, streak: streak, wrong: wrong, due: due };
    }

    // ---- 组卷（照抄 server.py Store.pick：过滤 + 洗牌/到期排序）----
    function pick(o) {
      o = o || {};
      var n = clampInt(o.n, 10, 1, 100);
      var level = o.level, qtype = o.qtype, bank = o.bank, section = o.section;
      var onlyNew = !!o.only_new, review = !!o.review;
      var lv = 0;
      if (level != null && level !== '') lv = clampInt(level, 0, 1, 5);   // 夹紧失败不过滤（同服务端）
      var seg = String(section == null ? '' : section).trim();
      var now = nowIso();
      var list = questions.filter(function (q) {
        if (lv && (q.level || 0) !== lv) return false;
        if (qtype && String(q.qtype || '').indexOf(qtype) < 0) return false;
        if (bank && (q.bank || '') !== bank) return false;
        if (seg && sec0(q.section_path) !== seg) return false;
        var p = prog[q.id];
        if (review) {
          if (!p || !p.due || p.due > now) return false;
        } else if (onlyNew) {
          if (p && (p.state === 'correct' || p.state === 'wrong')) return false;
        }
        return true;
      });
      if (review) {
        list.sort(function (a, b) {
          var da = prog[a.id].due, db = prog[b.id].due;
          return da < db ? -1 : da > db ? 1 : (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
        });
        return list.slice(0, n).map(pubRow);
      }
      // Fisher-Yates 洗牌，模拟服务端 ORDER BY RANDOM()
      for (var i = list.length - 1; i > 0; i--) {
        var j2 = Math.floor(Math.random() * (i + 1));
        var t = list[i]; list[i] = list[j2]; list[j2] = t;
      }
      return list.slice(0, n).map(pubRow);
    }

    function search(kw, limit) {
      limit = clampInt(limit, 50, 1, 100);
      kw = String(kw == null ? '' : kw).trim();
      if (!kw) return [];
      var k = kw.toLowerCase();
      return questions.filter(function (q) {
        return String(q.stem || '').toLowerCase().indexOf(k) >= 0;
      }).sort(byIdAsc).slice(0, limit).map(pubRow);
    }

    function banks() {
      var m = {};
      questions.forEach(function (q) { var b = q.bank || ''; m[b] = (m[b] || 0) + 1; });
      return Object.keys(m).map(function (b) { return { bank: b, n: m[b] }; })
        .sort(function (a, b) { return b.n - a.n || (a.bank < b.bank ? -1 : a.bank > b.bank ? 1 : 0); });
    }

    function sections() {
      var m = {};
      questions.forEach(function (q) {
        var s = sec0(q.section_path);
        if (s) m[s] = (m[s] || 0) + 1;
      });
      return Object.keys(m).map(function (s) { return { section: s, n: m[s] }; })
        .sort(function (a, b) { return b.n - a.n; });
    }

    function cards(kw, chapter, limit) {
      limit = clampInt(limit, 500, 1, 1000);
      kw = String(kw == null ? '' : kw).trim().toLowerCase();
      chapter = chapter || '';
      return cardsList.filter(function (c) {
        if (chapter && (c.chapter || '') !== chapter) return false;
        if (kw) {
          var blob = [c.name, c.body, c.topic].map(function (x) { return String(x || '').toLowerCase(); }).join(' ');
          if (blob.indexOf(kw) < 0) return false;
        }
        return true;
      }).slice(0, limit).map(function (c) {
        return { chapter: c.chapter, topic: c.topic, name: c.name, body: c.body,
                 qids: c.qids || [], source_class: c.source_class };
      });
    }

    function chapters() {
      var m = {};
      cardsList.forEach(function (c) { var ch = c.chapter || ''; m[ch] = (m[ch] || 0) + 1; });
      return Object.keys(m).sort().map(function (ch) { return { chapter: ch, n: m[ch] }; });
    }

    function wrongList(limit) {
      limit = clampInt(limit, 200, 1, 500);
      return Object.keys(prog).filter(function (id) { return (prog[id].wrong || 0) > 0 && byId[id]; })
        .map(function (id) {
          var d = pubRow(byId[id]);
          d.wrong = prog[id].wrong; d.streak = prog[id].streak; d.state = prog[id].state;
          return d;
        })
        .sort(function (a, b) { return b.wrong - a.wrong || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0); })
        .slice(0, limit);
    }

    function stats() {
      var byLevel = {}, byClass = {}, byType = {};
      questions.forEach(function (q) {
        var lv = q.level == null ? '?' : String(q.level);
        byLevel[lv] = (byLevel[lv] || 0) + 1;
        var sc = q.source_class || '未标注';
        byClass[sc] = (byClass[sc] || 0) + 1;
        var t = (q.qtype || '未标注').split('/')[0].split('·')[0];   // 主类型聚合，同服务端
        byType[t] = (byType[t] || 0) + 1;
      });
      var answered = 0, ok = 0, wrong = 0, due = 0, today = 0, todayOk = 0;
      var now = nowIso(), td = todayStr();
      Object.keys(prog).forEach(function (id) {
        var p = prog[id];
        if (p.state === 'correct' || p.state === 'wrong') answered++;
        if (p.state === 'correct') ok++;
        if ((p.wrong || 0) > 0) wrong++;
        if (p.due && p.due <= now) due++;
        if (p.last_seen && p.last_seen >= td) { today++; if (p.state === 'correct') todayOk++; }
      });
      return { mode: 'static', total: questions.length, cards: cardsList.length,
               answered: answered, correct: ok, wrong: wrong, due: due, today: today, today_ok: todayOk,
               by_level: byLevel, by_class: byClass, by_type: byType, intervals: INTERVALS.slice() };
    }

    function exportProgress() {
      var out = {};
      Object.keys(prog).forEach(function (id) {
        var p = prog[id];
        out[id] = { state: p.state, streak: p.streak, wrong: p.wrong, due: p.due, last_seen: p.last_seen };
      });
      return out;
    }

    function importProgress(data) {
      var n = 0;
      Object.keys(data || {}).forEach(function (pid) {
        var v = data[pid];
        if (!pid || !v || typeof v !== 'object') return;
        prog[pid] = {
          state: String(v.state == null ? 'new' : v.state),
          streak: clampInt(v.streak, 0, 0, 9999),
          wrong: clampInt(v.wrong, 0, 0, 99999),
          due: String(v.due || '') || null,
          last_seen: String(v.last_seen || '') || null
        };
        n++;
      });
      if (n) saveProg();
      return n;
    }

    // ---- 学习打卡：答题自动打卡（progress.last_seen 聚合）+ 手动补签 ----
    function loadManual() {
      try {
        var raw = storage && storage.getItem(CKEY);
        var arr = raw ? JSON.parse(raw) : [];
        return Array.isArray(arr) ? arr.filter(validDay) : [];
      } catch (e) { return []; }
    }
    function saveManual(days) {
      try { storage && storage.setItem(CKEY, JSON.stringify(days)); } catch (e) { /* 存满不打断 */ }
    }
    var manualDays = loadManual();

    // 按日聚合答题量（自动打卡的计数来源）
    function perDay() {
      var m = {};
      Object.keys(prog).forEach(function (id) {
        var p = prog[id];
        var d = (p.last_seen || '').slice(0, 10);
        if (!validDay(d)) return;
        var e = m[d] || (m[d] = { n: 0, ok: 0 });
        e.n++;
        if (p.state === 'correct') e.ok++;
      });
      return m;
    }

    function checkin(ym) {
      ym = ym || monthStr(new Date());
      if (!/^\d{4}-\d{2}$/.test(ym)) ym = monthStr(new Date());   // 非法月份回落当月（同 server）
      var per = perDay();
      var mset = {};
      manualDays.forEach(function (d) { mset[d] = 1; });
      var all = {};
      Object.keys(per).forEach(function (d) { all[d] = 1; });
      Object.keys(mset).forEach(function (d) { all[d] = 1; });
      var today = todayStr();
      var todayInfo = per[today] || { n: 0, ok: 0 };
      var checkedToday = todayInfo.n > 0 || !!mset[today];
      // 连续天数：今天打了从今天往前数；今天没打则算截至昨天的（今天补上即续上）
      var d = new Date();
      if (!all[today]) d = addDays(d, -1);
      var streak = 0;
      while (all[dayKey(d)]) { streak++; d = addDays(d, -1); }
      // 当月日历格子
      var parts = ym.split('-'), y = +parts[0], mo = +parts[1];
      var ndays = new Date(y, mo, 0).getDate();           // 当月天数
      var lead = (new Date(y, mo - 1, 1).getDay() + 6) % 7;  // 周一起始的前导格数
      var cells = [];
      for (var i = 1; i <= ndays; i++) {
        var key = ym + '-' + pad(i);
        var e = per[key] || { n: 0, ok: 0 };
        cells.push({ day: i, n: e.n, ok: e.ok, manual: !!mset[key], today: key === today });
      }
      return { today: today, checked_today: checkedToday, today_n: todayInfo.n, today_ok: todayInfo.ok,
               streak: streak, total_days: Object.keys(all).length, manual_count: manualDays.length,
               month: ym, lead: lead, days: cells };
    }

    function manualCheckin() {
      var t = todayStr();
      var ti = perDay()[t];
      // 今天已答题（自动打卡）则手动不重复记；纯手动补签才落 manual
      if (manualDays.indexOf(t) < 0 && !(ti && ti.n > 0)) {
        manualDays.push(t);
        manualDays.sort();
        saveManual(manualDays);
      }
      return checkin();
    }

    function exportCheckin() { return { manual: manualDays.slice() }; }

    function importCheckin(data) {
      var n = 0;
      var list = data && Array.isArray(data.manual) ? data.manual : (Array.isArray(data) ? data : []);
      list.forEach(function (d) {
        if (validDay(d) && manualDays.indexOf(d) < 0) {
          manualDays.push(d); n++;
        }
      });
      if (n) { manualDays.sort(); saveManual(manualDays); }
      return n;
    }

    function byIdAsc(a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; }

    return { grade: grade, mark: mark, pick: pick, search: search, banks: banks, sections: sections,
             cards: cards, chapters: chapters, wrong_list: wrongList, stats: stats,
             export_progress: exportProgress, import_progress: importProgress,
             merge_questions: mergeQuestions, custom_list: customList, clear_custom: clearCustom,
             export_template: exportTemplate,
             checkin: checkin, manual_checkin: manualCheckin,
             export_checkin: exportCheckin, import_checkin: importCheckin };
  }

  return { make: make, INTERVALS: INTERVALS.slice(), WRONG_BACK: WRONG_BACK, SKIP_BACK: SKIP_BACK, PROG_KEY: PROG_KEY };
});
