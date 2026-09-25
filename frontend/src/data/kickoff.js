// Shared kickoff-time formatting -- extracted from MatchesPage.jsx so every
// place that shows a fixture time (Matches, and now the Starting XI page's
// Tactical Swap eligibility panel) reads the same "Today / Tomorrow / Sun 21
// Sep" label in the viewer's own local timezone, rather than each screen
// growing its own Intl.DateTimeFormat call that could drift from this one.
export function kickoffLabel(kickoffTime) {
  if (!kickoffTime) return 'Time TBC'
  const date = new Date(kickoffTime)
  if (Number.isNaN(date.getTime())) return 'Time TBC'

  const now = new Date()
  const sameDay = date.toDateString() === now.toDateString()
  const tomorrow = new Date(now)
  tomorrow.setDate(now.getDate() + 1)
  const isTomorrow = date.toDateString() === tomorrow.toDateString()

  const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return `Today ${time}`
  if (isTomorrow) return `Tomorrow ${time}`
  return `${date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })} ${time}`
}
