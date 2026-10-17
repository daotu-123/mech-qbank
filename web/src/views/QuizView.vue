/* QuizView.vue — 刷题：组卷条件 + 题卡序列 + 组末小结
 * 会话状态在外部 session store：复习/错题/备注跳转都是 startSession 后 push 到本视图。 */
<template>
  <section>
    <FilterBar v-model="filters" @start="start" @search="search" />
    <div id="quiz-area">
      <QuestionCard v-if="cur" :key="cur.id" :q="cur" :index="session.i"
                    :total="session.items.length" :label="session.label" @next="next" />
      <div v-else-if="session.items.length" class="card">
        <div class="row" style="justify-content:space-between"><b>本组完成 ✓</b><span class="sub">{{ session.label }}</span></div>
        <div class="grid" style="margin-top:10px">
          <div class="kpi"><b>{{ session.items.length }}</b><span>总题数</span></div>
          <div class="kpi"><b>{{ session.res.ok }}</b><span>答对</span></div>
          <div class="kpi"><b>{{ session.res.bad }}</b><span>答错</span></div>
          <div class="kpi"><b>{{ session.res.self }}</b><span>自评</span></div>
          <div class="kpi"><b>{{ session.res.skip }}</b><span>跳过</span></div>
          <div class="kpi"><b>{{ rate }}%</b><span>正确率（自动判分）</span></div>
        </div>
        <div class="row" style="margin-top:12px">
          <button class="act" @click="start">再来一组（同条件）</button>
          <button class="ghost" @click="router.push('/wrong')">去看错题本</button>
          <button class="ghost" @click="router.push('/review')">去今日复习</button>
        </div>
      </div>
      <div v-else class="card empty">{{ hint || '选好条件，点「开始组卷」；或直接搜题干关键词' }}</div>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router';
import { api } from '@/api';
import { session, startSession } from '@/store/session';
import { loadNotes } from '@/store/notes';
import { loadMeta } from '@/store/meta';
import { lvMeta } from '@/util';
import FilterBar from '@/components/FilterBar.vue';
import QuestionCard from '@/components/QuestionCard.vue';

const router = useRouter();
const filters = ref({ level: '', qtype: '', n: 10, onlyNew: true, bank: '', section: '', q: '' });
const hint = ref('');

const cur = computed(() => session.items[session.i] || null);
const rate = computed(() => {
  const auto = session.res.ok + session.res.bad;
  return auto ? Math.round(100 * session.res.ok / auto) : 0;
});

function filterLabel() {
  const f = filters.value, parts = [];
  if (f.level) parts.push(lvMeta(f.level).name + '难度');
  if (f.qtype) parts.push(f.qtype);
  if (f.bank) parts.push(f.bank);
  if (f.section) parts.push(f.section.slice(0, 14));
  if (f.onlyNew) parts.push('只出没做过的');
  return parts.length ? parts.join(' · ') : '随机组卷';
}

function beginSession(items, label) {
  if (!items.length) {
    const f = filters.value;
    hint.value = f.qtype || f.level
      ? '当前题库没有「' + [f.level ? lvMeta(f.level).name + '难度' : '', f.qtype].filter(Boolean).join(' + ') +
        '」的题目，放宽条件试试；也可以在「进度」页导入自己的题库补充'
      : '没有匹配的题目，换个条件试试';
    startSession([], '');
    return;
  }
  hint.value = '';
  startSession(items, label);
}

async function start() {
  await loadMeta();
  const f = filters.value, p = new URLSearchParams({ limit: String(f.n) });
  if (f.level) p.set('level', f.level);
  if (f.qtype) p.set('qtype', f.qtype);
  if (f.onlyNew) p.set('only_new', '1');
  if (f.bank) p.set('bank', f.bank);
  if (f.section) p.set('section', f.section);
  const r = await api('/questions?' + p.toString());
  if (!r) return;
  beginSession(r.items || [], filterLabel());
}

async function search() {
  const kw = (filters.value.q || '').trim();
  if (!kw) { hint.value = '先输入题干关键词'; return; }
  const r = await api('/questions?q=' + encodeURIComponent(kw) + '&limit=50');
  if (!r) return;
  if (!(r.items || []).length) { hint.value = '没有匹配「' + kw + '」的题目'; startSession([], ''); return; }
  beginSession(r.items, '搜索：' + kw);
}

function next() { session.i++; }

onMounted(() => {
  loadMeta();     // 题型/难度下拉按真实题库分布即时填充（不点「开始」也能选题）
  loadNotes();    // 备注缓存：题卡显示「有备注」与备注内容
});
</script>
