/* router.js — hash 路由（file:// 双击打开时 history 模式不可用，hash 是唯一可靠选择） */
import { createRouter, createWebHashHistory } from 'vue-router'
import QuizView from '@/views/QuizView.vue'
import ReviewView from '@/views/ReviewView.vue'
import WrongView from '@/views/WrongView.vue'
import CardsView from '@/views/CardsView.vue'
import StatsView from '@/views/StatsView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'quiz', component: QuizView },
    { path: '/review', name: 'review', component: ReviewView },
    { path: '/wrong', name: 'wrong', component: WrongView },
    { path: '/cards', name: 'cards', component: CardsView },
    { path: '/stats', name: 'stats', component: StatsView },
  ],
  scrollBehavior() { return { top: 0 }; }
});

export default router;
