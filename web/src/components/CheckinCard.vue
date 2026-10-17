/* CheckinCard.vue — 学习打卡：连续天数 + 月历（答题打卡/手动补签/今日）+ 补签按钮 */
<template>
  <div class="card" style="margin-top:14px">
    <div class="row">
      <div>
        <div class="ck-streak"><b>{{ data ? data.streak : 0 }}</b><span>天连续打卡</span></div>
        <div class="meta" style="margin-top:4px">{{ showMonth }} 打卡 {{ monthDays }} 天 · 累计 {{ data ? data.total_days : 0 }} 天</div>
      </div>
      <div class="row" style="margin-left:auto">
        <span class="pill" :class="data && data.checked_today ? 'green' : 'gray'">{{ todayText }}</span>
        <button class="act" :disabled="data && data.checked_today" @click="checkin">{{ data && data.checked_today ? '今日已打卡 ✓' : '打卡' }}</button>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <button class="ghost" style="padding:3px 10px" @click="shift(-1)">‹ 上月</button>
      <b style="font-size:13.5px;min-width:70px;text-align:center">{{ showMonth }}</b>
      <button class="ghost" style="padding:3px 10px" :disabled="!ckMonth" @click="shift(1)">下月 ›</button>
      <span class="sub" style="margin-left:auto">{{ monthDays }} 天有记录</span>
    </div>
    <div class="ck-cal">
      <div v-for="w in ['一','二','三','四','五','六','日']" :key="w" class="wd">{{ w }}</div>
      <div v-for="i in (data ? data.lead : 0)" :key="'b' + i" class="cell blank"></div>
      <div v-for="c in (data ? data.days : [])" :key="c.day" class="cell"
           :class="{ on: c.n > 0, manual: !c.n && c.manual, today: c.today }"
           :title="c.n > 0 ? '答题 ' + c.n + ' 题' + (c.ok ? ' · 答对 ' + c.ok : '') : (c.manual ? '手动补签' : '未打卡')">{{ c.day }}</div>
    </div>
    <div class="meta ck-legend">
      <span><i style="background:var(--steel)"></i>答题打卡</span>
      <span><i style="background:var(--amber)"></i>手动补签</span>
      <span><i style="background:#f1f2f4;border:1px solid var(--line)"></i>未打卡</span>
      <span>每日答任意 1 题即自动打卡，断天连续数重新计</span>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus';
import { api } from '@/api';
import { bump } from '@/store/bus';

const data = ref(null);
const ckMonth = ref('');           // 空 = 当月；'YYYY-MM' 翻历史
const curMonth = computed(() => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0'); });
const showMonth = computed(() => (data.value && data.value.month) || curMonth.value);
const monthDays = computed(() => data.value ? data.value.days.filter(d => d.n > 0 || d.manual).length : 0);
const todayText = computed(() => {
  if (!data.value) return '今日未打卡';
  if (!data.value.checked_today) return '今日未打卡';
  return data.value.today_n > 0 ? '今日已打卡 · 答题 ' + data.value.today_n + ' 题' : '今日已手动补签';
});

async function load() {
  const r = await api('/checkins' + (ckMonth.value ? '?month=' + ckMonth.value : ''));
  if (r) data.value = r;
}
async function checkin() {
  const r = await api('/checkins', {}, 'POST');
  if (r) { ElMessage.success('打卡成功 · 连续 ' + r.streak + ' 天'); data.value = r; bump(); }
}
function shift(delta) {
  const [y, m] = (ckMonth.value || curMonth.value).split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  ckMonth.value = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  if (ckMonth.value >= curMonth.value) ckMonth.value = '';
  load();
}
onMounted(load);
</script>
