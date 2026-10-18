// ts-fsrs：与 fsrs-ref.py 完全同场景（到期即复习），用于跨语言比对。
import { fsrs, generatorParameters, createEmptyCard, Rating } from 'ts-fsrs'

const RATINGS = ['Good', 'Good', 'Good', 'Again', 'Good', 'Hard', 'Easy', 'Good', 'Again', 'Good']
const STATE_NAME = { 0: 'New', 1: 'Learning', 2: 'Review', 3: 'Relearning' }
const round = (x, n = 6) => Math.round(x * 10 ** n) / 10 ** n

function run() {
  const f = fsrs(generatorParameters({ enable_fuzz: false }))
  let card = createEmptyCard(new Date('2026-10-18T12:00:00+00:00'))
  let now = new Date('2026-10-18T12:00:00+00:00')
  const out = []
  for (const rname of RATINGS) {
    card = f.repeat(card, now)[Rating[rname]].card
    out.push({
      at: now.toISOString(),
      rating: rname,
      stability: round(card.stability),
      difficulty: round(card.difficulty),
      state: STATE_NAME[card.state] ?? String(card.state),
      due: card.due.toISOString(),
    })
    now = card.due
  }
  return out
}

console.log(JSON.stringify({ js: run() }, null, 1))
