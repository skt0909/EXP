// What state a Quick 11 contest is in, for the badge and for every
// "can the user still change things" decision on the contest screens.
//
// is_locked alone can't answer either. It stays TRUE forever once set, so a
// match in play and one that ended months ago look the same; and it's set
// by a sweep that runs every 5 minutes, so for a few minutes after kickoff
// it is still FALSE. The backend refuses team changes from kickoff itself
// (dream11.enforce_contest_lock_fn), so the screens treat the contest as
// locked from kickoff too, rather than offering an edit that will bounce.

export const CONTEST_STATUS = {
  OPEN: 'open',
  LIVE: 'live',
  COMPLETED: 'completed',
  CANCELLED: 'cancelled',
}

const CANCEL_REASONS = {
  postponed: 'Match postponed',
  abandoned: 'Match abandoned',
}

export function hasKickedOff(kickoffTime, now = new Date()) {
  if (!kickoffTime) return false
  return new Date(kickoffTime).getTime() <= now.getTime()
}

// True once teams can no longer be submitted or edited.
export function isContestLocked(contest, now = new Date()) {
  if (!contest) return false
  return Boolean(
    contest.is_locked || contest.is_finalized || contest.void_reason || hasKickedOff(contest.kickoff_time, now),
  )
}

export function contestStatus(contest, now = new Date()) {
  if (contest?.void_reason) {
    return {
      status: CONTEST_STATUS.CANCELLED,
      label: 'Cancelled',
      detail: CANCEL_REASONS[contest.void_reason] ?? 'Match not played',
    }
  }
  if (contest?.is_finalized) {
    return { status: CONTEST_STATUS.COMPLETED, label: 'Completed', detail: null }
  }
  if (isContestLocked(contest, now)) {
    return { status: CONTEST_STATUS.LIVE, label: 'Live', detail: null }
  }
  return { status: CONTEST_STATUS.OPEN, label: 'Open', detail: null }
}

// Badge colours per status, from the app's existing token classes.
export const CONTEST_STATUS_CLASSES = {
  [CONTEST_STATUS.OPEN]: 'bg-surface-container-high text-on-surface-variant',
  [CONTEST_STATUS.LIVE]: 'bg-secondary text-on-secondary',
  [CONTEST_STATUS.COMPLETED]: 'bg-primary text-on-primary',
  [CONTEST_STATUS.CANCELLED]: 'bg-error-container text-on-error-container',
}
