/* FilterBar.vue — 组卷条件：难度/题型/数量/题库/来源 + 只出没做过的 + 搜题 + 条件 chips */
<template>
  <div class="card">
    <div class="sec-title">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
      组卷条件
    </div>
    <div class="row">
      <label>难度
        <el-select v-model="f.level" placeholder="不限难度" clearable style="width:150px" @change="touch">
          <el-option v-for="lv in ['1','2','3','4','5']" :key="lv" :value="lv"
            :label="lv + ' ' + lvMeta(lv).name + '（' + (byLevel[lv] || 0) + ' 题）'" />
        </el-select>
      </label>
      <label>题型
        <el-select v-model="f.qtype" placeholder="不限题型" clearable style="width:150px" @change="touch">
          <el-option v-for="[t, n] in typeEntries" :key="t" :value="t" :label="t + '（' + n + '）'" />
        </el-select>
      </label>
      <label>数量
        <el-select v-model="f.n" style="width:96px" @change="touch">
          <el-option v-for="n in [5, 10, 20, 50]" :key="n" :value="n" :label="String(n)" />
        </el-select>
      </label>
      <label style="display:flex;align-items:center;gap:6px">
        <el-checkbox v-model="f.onlyNew" @change="touch">只出没做过的</el-checkbox>
      </label>
      <el-button type="primary" @click="emit('start')">开始组卷</el-button>
    </div>
    <div class="row" style="margin-top:10px">
      <label>题库
        <el-select v-model="f.bank" placeholder="全部题库" clearable style="width:220px" @change="touch">
          <el-option v-for="b in banks" :key="b.bank" :value="b.bank" :label="b.bank + '（' + b.n + '）'" />
        </el-select>
      </label>
      <label>来源
        <el-select v-model="f.section" placeholder="全部来源" clearable style="width:220px" @change="touch">
          <el-option v-for="s in sections" :key="s.section" :value="s.section"
            :label="(s.section.length > 26 ? s.section.slice(0, 26) + '…' : s.section) + '（' + s.n + '）'" />
        </el-select>
      </label>
      <el-input v-model="f.q" placeholder="搜题干关键词，如：溢流阀 / 公差配合" style="width:240px"
                clearable @keyup.enter="emit('search')" />
      <el-button @click="emit('search')">搜题</el-button>
    </div>
    <div class="cond-bar">
      <span class="lab">当前条件</span>
      <template v-if="chips.length">
        <span v-for="c in chips" :key="c.k" class="cond-chip">
          <template v-if="c.label">{{ c.label }} <b :style="{ color: c.color || 'inherit' }">{{ c.value }}</b></template>
          <b v-else>{{ c.value }}</b>
          <x title="清除此条件" @click="clearChip(c.k)">×</x>
        </span>
      </template>
      <span v-else class="cond-chip"><i>不限难度 · 不限题型 · 全库随机</i></span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { lvMeta } from '@/util'
import { banks, sections, byType, byLevel } from '@/store/meta'

const props = defineProps({ modelValue: { type: Object, required: true } });
const emit = defineEmits(['update:modelValue', 'start', 'search']);
const f = computed(() => props.modelValue);

const typeEntries = computed(() => Object.entries(byType.value).sort((a, b) => b[1] - a[1]));

/* chips：结构化字段（不用 v-html 拼 label，Vue 自动转义更稳） */
const chips = computed(() => {
  const out = [];
  if (f.value.level) {
    const m = lvMeta(f.value.level);
    out.push({ k: 'level', label: '难度', value: f.value.level + ' ' + m.name, color: m.c });
  }
  if (f.value.qtype) out.push({ k: 'qtype', label: '题型', value: f.value.qtype });
  if (f.value.bank) {
    const b = banks.value.find(x => x.bank === f.value.bank);
    out.push({ k: 'bank', label: '题库', value: b ? b.bank : f.value.bank });
  }
  if (f.value.section) {
    const s = sections.value.find(x => x.section === f.value.section);
    out.push({ k: 'section', label: '来源', value: (s ? s.section : f.value.section).slice(0, 12) });
  }
  out.push({ k: 'n', value: f.value.n + ' 题' });
  if (f.value.onlyNew) out.push({ k: 'onlyNew', value: '只出没做过的' });
  return out;
});

function touch() { emit('update:modelValue', { ...f.value }); }
function clearChip(k) {
  const next = { ...f.value };
  if (k === 'level') next.level = '';
  if (k === 'qtype') next.qtype = '';
  if (k === 'bank') next.bank = '';
  if (k === 'section') next.section = '';
  if (k === 'onlyNew') next.onlyNew = false;
  if (k === 'n') next.n = 10;
  emit('update:modelValue', next);
}
</script>
