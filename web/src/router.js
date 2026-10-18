/* router.js — hash 路由（file:// 双击打开时 history 模式不可用，hash 是唯一可靠选择） */
import { createRouter, createWebHashHistory } from 'vue-router'
import QuizView from '@/views/QuizView.vue'
import TodayView from '@/views/TodayView.vue'
import ReviewView from '@/views/ReviewView.vue'
import WrongView from '@/views/WrongView.vue'
import CardsView from '@/views/CardsView.vue'
import StatsView from '@/views/StatsView.vue'
import ExcerptView from '@/views/ExcerptView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    // 首页 = 今日学习台（只回答「今天做什么」），刷题移到 /quiz。
    // 依据竞品调研：成功的学习产品首页都在回答「今天该做什么」，而不是给筛选器。
    { path: '/', name: 'today', component: TodayView },
    { path: '/quiz', name: 'quiz', component: QuizView },
    { path: '/review', name: 'review', component: ReviewView },
    { path: '/wrong', name: 'wrong', component: WrongView },
    { path: '/cards', name: 'cards', component: CardsView },
    { path: '/stats', name: 'stats', component: StatsView },
    { path: '/excerpt', name: 'excerpt', component: ExcerptView },
  ],
  scrollBehavior() { return { top: 0 }; }
});

export default router;
