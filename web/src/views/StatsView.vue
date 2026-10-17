/* StatsView.vue — 进度总览：KPI + 打卡 + 备注 + 分布 + 导入 + 备份 */
<template>
  <section>
    <div class="grid">
      <div v-for="[k, v] in kpis" :key="k" class="kpi"><b>{{ v }}</b><span>{{ k }}</span></div>
    </div>

    <CheckinCard />

    <NotesCard />

    <div class="card" style="margin-top:14px"><b>难度分布</b>
      <div style="margin-top:10px">
        <div v-for="lv in ['1','2','3','4','5']" :key="lv" class="row" style="gap:8px;margin-bottom:6px">
          <span class="sub" style="width:64px">{{ lvNames(lv) }}</span>
          <div class="bar" style="flex:1"><i :style="{ width: barW(s.by_level, lv) + '%' }"></i></div>
          <span class="sub" style="width:52px;text-align:right">{{ (s.by_level || {})[lv] || 0 }}</span>
        </div>
      </div>
      <div class="meta" style="margin-top:8px">整体作答进度 {{ pct }}% ｜ 今日答对 {{ s.today_ok }} 题</div>
    </div>

    <div class="card"><b>题型分布</b>
      <div style="margin-top:10px">
        <div v-for="t in typeTop" :key="t" class="row" style="gap:8px;margin-bottom:6px">
          <span class="sub" style="width:64px">{{ t }}</span>
          <div class="bar" style="flex:1"><i :style="{ width: barW(s.by_type, t) + '%' }"></i></div>
          <span class="sub" style="width:52px;text-align:right">{{ (s.by_type || {})[t] || 0 }}</span>
        </div>
      </div>
    </div>

    <div class="card"><b>来源分级</b><div class="meta">{{ classText }}</div></div>
    <div class="card"><b>复习规则</b><div class="meta">{{ ruleText }}</div></div>

    <ImportCard />
    <ProgressCard />
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '@/api';
import { dataVersion } from '@/store/bus';
import { loadNotes } from '@/store/notes';
import { lvMeta } from '@/util';
import CheckinCard from '@/components/CheckinCard.vue';
import NotesCard from '@/components/NotesCard.vue';
import ImportCard from '@/components/ImportCard.vue';
import ProgressCard from '@/components/ProgressCard.vue';

const s = ref({ by_level: {}, by_type: {}, by_class: {}, intervals: [] });
const lvNames = k => k + ' ' + lvMeta(k).name;

const kpis = computed(() => [['题库总量', s.value.total], ['已作答', s.value.answered],
  ['掌握', s.value.correct], ['错题', s.value.wrong], ['待复习', s.value.due],
  ['今日已学', s.value.today], ['知识点卡', s.value.cards]]);
const pct = computed(() => s.value.total ? Math.round(100 * s.value.answered / s.value.total) : 0);
const typeTop = computed(() => Object.entries(s.value.by_type || {}).sort((a, b) => b[1] - a[1]).slice(0, 10).map(x => x[0]));
const classText = computed(() => JSON.stringify(s.value.by_class));
const ruleText = computed(() => '答对后按 ' + (s.value.intervals || []).join(' / ') +
  ' 天间隔复习；答错次日必回；跳过 3 天后回。问答题/计算题不自动判分，由你自评。');

function barW(obj, key) {
  const m = Math.max(1, ...Object.values(obj || {}));
  return Math.round(100 * ((obj || {})[key] || 0) / m);
}

async function load() {
  const r = await api('/stats');
  if (r) s.value = r;
}

/* 答题/打卡/导入等任何数据变更都触发刷新（bump 见 store/bus.js） */
watch(dataVersion, load);
onMounted(async () => { await Promise.all([load(), loadNotes()]); });
</script>
