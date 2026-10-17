/* WrongView.vue — 错题本：按错误次数排序，可整本重练 */
<template>
  <section>
    <div class="card">
      <div class="row">
        <b>错题本</b><span class="sub">{{ items.length }} 道</span>
        <button class="ghost" style="margin-left:auto" @click="redo">重练本页错题</button>
      </div>
    </div>
    <div v-if="items.length" class="card">
      <div v-for="q in items" :key="q.id" class="card-item">
        <h4>
          {{ q.id }}
          <span class="lv-badge" :style="{ borderColor: lvMeta(q.level).c, color: lvMeta(q.level).c }">L{{ q.level ?? '?' }} {{ lvMeta(q.level).name }}</span>
          <span class="pill red">错 {{ q.wrong }} 次</span>
          <span v-if="q.state === 'correct'" class="pill green">已掌握</span>
          <span v-else class="pill gray">待巩固</span>
        </h4>
        <p>{{ q.stem }}</p>
        <div class="meta">答案：{{ q.answer || '（待补）' }}</div>
      </div>
    </div>
    <div v-else class="card empty">错题本是空的 ✓</div>
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';
import { api } from '@/api';
import { startSession } from '@/store/session';
import { lvMeta } from '@/util';

const router = useRouter();
const items = ref([]);

async function load() {
  const r = await api('/wrong');
  items.value = (r && r.items) || [];
}
function redo() {
  if (!items.value.length) { ElMessage.error('错题本是空的'); return; }
  startSession(items.value, '错题重练');
  router.push('/');
}
onMounted(load);
</script>
