/* ReviewView.vue — 今日复习：到期题列表，一键开始（复用刷题视图的题卡） */
<template>
  <section>
    <div class="card">
      <div class="row">
        <b>今日到期的复习题</b><span class="sub">{{ items.length }} 道</span>
        <button class="ghost" style="margin-left:auto" @click="load">刷新</button>
      </div>
    </div>
    <div v-if="items.length" class="card">
      <div class="row">
        <button class="act" @click="begin">开始复习</button>
        <span class="sub">按到期时间排序，答错次日必回</span>
      </div>
    </div>
    <div v-else class="card empty">今天没有到期的复习题 ✓</div>
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router';
import { api } from '@/api';
import { startSession } from '@/store/session';

const router = useRouter();
const items = ref([]);

async function load() {
  const r = await api('/questions?review=1&limit=30');
  items.value = (r && r.items) || [];
}
function begin() {
  startSession(items.value, '今日复习');
  router.push('/');
}
onMounted(load);
</script>
