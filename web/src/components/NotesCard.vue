/* NotesCard.vue — 我的备注汇总：每条标注属于哪道题（题号 + 题干摘要），可跳回原题或删除 */
<template>
  <div class="card" style="margin-top:14px">
    <b>我的备注</b><span class="sub" style="margin-left:8px">{{ list.length }} 条</span>
    <div class="meta" style="margin-top:2px">每条备注都标着它属于哪道题（题号 + 题干摘要），可跳回原题或删除</div>
    <div style="margin-top:10px">
      <div v-if="!list.length" class="meta">还没有备注。刷题时点题目下方的「＋ 给这道题添加备注」就能记下来。</div>
      <div v-for="n in list" :key="n.id" class="note-item">
        <div class="note-item-head">
          <span class="note-id">{{ n.id }}</span>
          <span v-if="n.qtype" class="qtype-badge" :style="{ background: qtMeta(n.qtype).color, fontSize: '11px', padding: '2px 8px 2px 6px' }"><span v-html="qtMeta(n.qtype).icon"></span>{{ n.qtype }}</span>
          <span v-else class="pill gray" style="font-size:11px">题目已不存在</span>
          <span class="note-stem" :title="n.stem || '（该题已不在当前题库中，备注仍保留）'">{{ n.stem || '（该题已不在当前题库中，备注仍保留）' }}</span>
          <button class="ghost" style="padding:2px 9px;font-size:12px" @click="jump(n.id)">跳到该题</button>
          <button class="ghost" style="padding:2px 9px;font-size:12px" @click="remove(n.id)">删除</button>
        </div>
        <div class="note-text">{{ n.note }}</div>
        <div class="meta">{{ n.updated_at ? '更新于 ' + String(n.updated_at).replace('T', ' ').slice(0, 16) : '' }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '@/api';
import { notesList, deleteNote } from '@/store/notes';
import { startSession } from '@/store/session';
import { qtMeta } from '@/util';

const router = useRouter();
const list = computed(() => notesList.value);

async function jump(qid) {
  const r = await api('/questions/' + encodeURIComponent(qid));
  const item = r && r.item;
  if (!item) { ElMessage.error('题目已不存在：' + qid); return; }
  startSession([item], '备注 · ' + qid);
  router.push('/');
  setTimeout(() => document.querySelector('#quiz-area')?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 50);
}
async function remove(qid) {
  try {
    await ElMessageBox.confirm('删除「' + qid + '」这条备注？', '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' });
  } catch (e) { return; }
  await deleteNote(qid);
}
</script>
