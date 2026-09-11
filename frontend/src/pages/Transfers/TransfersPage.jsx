import { useEffect, useMemo, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import FplHeader from '../../components/FplHeader/FplHeader'
import { fetchCurrentSquad } from '../../api/squad'
import { fetchPlayers } from '../../api/players'
import { fetchTransfersUsed, submitTransfers } from '../../api/transfers'
import {
  deleteTransferDraft,
  fetchTransferDrafts,
  putTransferDraft,
} from '../../api/transferDrafts'
import { LockedError } from '../../api/client'
import PlayerJersey from '../../components/PlayerJersey/PlayerJersey'
import { normalizePremierLeaguePlayer } from '../../data/premierLeague2026'

const POSITION_ORDER = ['GK', 'DEF', 'MID', 'FWD']
const POINTS_PER_EXTRA_TRANSFER = 4

function netSpend(transfers, squadById, playersById) {
  return transfers.reduce((sum, t) => {
    const outP = squadById.get(t.outId)
    const inP = playersById.get(t.inId)
    return sum + (outP?.price ?? 0) - (inP?.price ?? 0)
  }, 0)
}

function opponentLabel(player) {
  if (!player.next_opponent) return `${player.club} • No fixture yet`
  return `${player.club} • Next: ${player.next_opponent} (${player.next_opponent_is_home ? 'H' : 'A'})`
}

function playerPoints(player) {
  return Number(player?.points ?? player?.fpl_points ?? 0)
}

// The cart lives on the server (GET/PUT/DELETE /transfer-drafts) so it
// survives a refresh or a move to another device. Local shape stays
// {outId, inId} as the rest of this page already expects, plus the row id
// the delete endpoint needs.
function toDraft(row) {
  return { id: row.id, outId: row.player_out_id, inId: row.player_in_id }
}

// Mirrors the server's ORDER BY created_at, id: re-drafting a player already
// in the cart is an upsert that keeps its place, a new one lands at the end.
function mergeDraft(drafts, incoming) {
  const existing = drafts.findIndex((d) => d.outId === incoming.outId)
  if (existing === -1) return [...drafts, incoming]
  return drafts.map((d, i) => (i === existing ? incoming : d))
}

function TransfersPage() {
  const { settings } = useOutletContext()
  const { user_id, season, gameweek } = settings

  const [squad, setSquad] = useState(null)
  const [allPlayers, setAllPlayers] = useState([])
  const [transfersUsed, setTransfersUsed] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  const [pendingTransfers, setPendingTransfers] = useState([]) // [{id, outId, inId}], server-backed
  const [draftError, setDraftError] = useState(null)
  const [focusedOutId, setFocusedOutId] = useState(null)
  const [browseExpanded, setBrowseExpanded] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [sortMode, setSortMode] = useState('price_desc')

  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)
  // Set once the backend rejects a batch because the deadline passed.
  const [locked, setLocked] = useState(false)
  const [submitSuccess, setSubmitSuccess] = useState(null)

  function reloadTransfersUsed() {
    return fetchTransfersUsed({ season, gameweek }).then(setTransfersUsed)
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    Promise.all([
      fetchCurrentSquad({ season, gameweek }),
      fetchPlayers({ season, gameweek }),
      fetchTransfersUsed({ season, gameweek }),
      fetchTransferDrafts({ season, gameweek }),
    ])
      .then(([squadData, playersData, transfersUsedData, draftsData]) => {
        if (cancelled) return
        setSquad({
          ...squadData,
          players: (squadData.players ?? []).map(normalizePremierLeaguePlayer),
        })
        setAllPlayers(playersData.map(normalizePremierLeaguePlayer))
        setTransfersUsed(transfersUsedData)
        // Whatever was staged before the last refresh comes back here.
        setPendingTransfers((draftsData.drafts ?? []).map(toDraft))
      })
      .catch((err) => {
        if (cancelled) return
        setLoadError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [user_id, season, gameweek])

  const squadById = useMemo(
    () => new Map((squad?.players ?? []).map((p) => [p.player_id, p])),
    [squad]
  )
  const playersById = useMemo(() => new Map(allPlayers.map((p) => [p.id, p])), [allPlayers])
  const ownedIds = useMemo(() => new Set(squadById.keys()), [squadById])
  const pendingInIds = useMemo(() => new Set(pendingTransfers.map((t) => t.inId)), [pendingTransfers])
  const pendingByOutId = useMemo(() => new Map(pendingTransfers.map((t) => [t.outId, t])), [pendingTransfers])

  const bankAfterAll = (squad?.budget_remaining ?? 0) + netSpend(pendingTransfers, squadById, playersById)

  // Real free-slot arithmetic from GET /transfers/used -- reflects any
  // transfers already committed this gameweek, not just what's pending
  // here, and honors an active wildcard/free_hit (uncapped free).
  const chipActive = transfersUsed?.chip_active ?? false
  const freeRemaining = transfersUsed?.free_transfers_remaining ?? 0
  const freeAppliedToPending = chipActive ? pendingTransfers.length : Math.min(pendingTransfers.length, freeRemaining)
  const paidCount = chipActive ? 0 : Math.max(0, pendingTransfers.length - freeRemaining)
  const costEstimate = paidCount * POINTS_PER_EXTRA_TRANSFER

  const groupedSquad = useMemo(() => {
    const groups = { GK: [], DEF: [], MID: [], FWD: [] }
    for (const p of squad?.players ?? []) groups[p.position]?.push(p)
    return groups
  }, [squad])

  const focusedOutPlayer = focusedOutId != null ? squadById.get(focusedOutId) : null

  const candidatePool = useMemo(() => {
    if (!focusedOutPlayer) return []
    return allPlayers.filter(
      (p) => p.position === focusedOutPlayer.position && !ownedIds.has(p.id) && !pendingInIds.has(p.id)
    )
  }, [allPlayers, focusedOutPlayer, ownedIds, pendingInIds])

  const maxAffordable = useMemo(() => {
    if (!focusedOutPlayer || !squad) return 0
    const otherPending = pendingTransfers.filter((t) => t.outId !== focusedOutId)
    const bankExcludingThisPair = squad.budget_remaining + netSpend(otherPending, squadById, playersById)
    return bankExcludingThisPair + focusedOutPlayer.price
  }, [focusedOutPlayer, squad, pendingTransfers, focusedOutId, squadById, playersById])

  const suggested = useMemo(() => {
    return [...candidatePool]
      .filter((p) => p.price <= maxAffordable)
      .sort((a, b) => b.price - a.price)
      .slice(0, 2)
  }, [candidatePool, maxAffordable])

  const browseResults = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    let list = candidatePool.filter((p) => !q || p.name.toLowerCase().includes(q))
    list = [...list].sort((a, b) => {
      if (sortMode === 'price_asc') return a.price - b.price
      if (sortMode === 'opponent') return (a.next_opponent ?? '￿').localeCompare(b.next_opponent ?? '￿')
      return b.price - a.price // price_desc, default
    })
    return list
  }, [candidatePool, searchQuery, sortMode])

  function focusOut(playerId) {
    setFocusedOutId((current) => (current === playerId ? null : playerId))
    setBrowseExpanded(false)
    setSearchQuery('')
  }

  async function cancelPending(outId) {
    const draft = pendingByOutId.get(outId)
    if (!draft) return
    setDraftError(null)
    try {
      const remaining = await deleteTransferDraft({ id: draft.id, season, gameweek })
      setPendingTransfers((remaining.drafts ?? []).map(toDraft))
    } catch (err) {
      setDraftError(`Couldn't remove that transfer: ${err.message}`)
    }
  }

  async function selectReplacement(inPlayer) {
    const outId = focusedOutId
    // Collapse the picker immediately -- the staged pair is what the manager
    // is now looking at, and the request below is fast enough not to warrant
    // a spinner in the list.
    setFocusedOutId(null)
    setBrowseExpanded(false)
    setSearchQuery('')
    setDraftError(null)
    try {
      const saved = await putTransferDraft({
        season,
        gameweek,
        player_out_id: outId,
        player_in_id: inPlayer.id,
      })
      setPendingTransfers((prev) => mergeDraft(prev, toDraft(saved)))
    } catch (err) {
      setDraftError(`Couldn't stage that transfer: ${err.message}`)
    }
  }

  async function handleConfirm() {
    setSubmitting(true)
    setSubmitError(null)
    setSubmitSuccess(null)
    try {
      const payload = pendingTransfers.map((t) => ({ player_out_id: t.outId, player_in_id: t.inId }))
      const result = await submitTransfers({ season, gameweek, transfers: payload })
      setSubmitSuccess(`${result.transfers.length} transfer(s) confirmed.`)
      // The batch is committed, so the cart it came from is spent. Cleared
      // one row at a time (there are at most 15) and then re-read, so the
      // displayed cart is the server's answer rather than an assumption --
      // a delete that failed would otherwise leave a phantom pair behind.
      await Promise.allSettled(
        pendingTransfers.map((t) => deleteTransferDraft({ id: t.id, season, gameweek }))
      )
      const remaining = await fetchTransferDrafts({ season, gameweek })
      setPendingTransfers((remaining.drafts ?? []).map(toDraft))
      const refreshed = await fetchCurrentSquad({ season, gameweek })
      setSquad({
        ...refreshed,
        players: (refreshed.players ?? []).map(normalizePremierLeaguePlayer),
      })
      await reloadTransfersUsed()
    } catch (err) {
      // Past the deadline the backend rejects the batch with a clean 422 and
      // writes nothing (transfers are all-or-nothing) -- an expected state, so
      // it becomes a locked notice rather than a red error.
      if (err instanceof LockedError) setLocked(true)
      setSubmitError(err.errors ?? [err.message])
    } finally {
      setSubmitting(false)
    }
  }

  if (loading) {
    return <div className="w-full px-safe-margin py-lg font-body-md text-body-md text-on-surface-variant">Loading squad…</div>
  }
  if (loadError) {
    return <div className="w-full px-safe-margin py-lg font-body-md text-body-md text-on-surface-variant" role="alert">Couldn't load your squad: {loadError}</div>
  }
  if (!squad || squad.players.length === 0) {
    return <div className="w-full px-safe-margin py-lg font-body-md text-body-md text-on-surface-variant">No squad found for this season yet — select your squad first.</div>
  }

  return (
    <>
      <FplHeader title="Transfers" />
      <main className="w-full px-safe-margin py-md flex flex-col gap-lg pb-[200px]">
        <div className="flex justify-between items-center bg-surface-container rounded-lg p-sm">
          <span className="font-label-md text-label-md text-primary">Gameweek {gameweek}</span>
          <div className="flex items-center gap-1 text-on-surface-variant">
            <span className="material-symbols-outlined text-[14px]">schedule</span>
            <span className="font-label-md text-label-md">
              {chipActive ? 'Chip active' : `${freeRemaining} free left`}
            </span>
          </div>
        </div>

        <div className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm relative overflow-hidden">
          <div className="absolute top-0 right-0 w-16 h-16 bg-primary-container/5 rounded-bl-full pointer-events-none" />
          <h2 className="font-headline-sm text-headline-sm text-primary mb-1">
            {chipActive
              ? 'Unlimited Free Transfers (chip active)'
              : `${freeRemaining} Free Transfer${freeRemaining === 1 ? '' : 's'} Available`}
          </h2>
          <p className="font-body-md text-body-md text-on-surface-variant">
            Cost per extra transfer:{' '}
            <span className="font-stats-number text-error">-{POINTS_PER_EXTRA_TRANSFER} pts</span>
          </p>
        </div>

        <section className="flex flex-col gap-sm">
          <h3 className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider px-2">
            Transfers Out
          </h3>
          <div className="bg-surface-container-lowest rounded-xl border border-outline-variant divide-y divide-outline-variant shadow-sm overflow-hidden">
            {POSITION_ORDER.flatMap((pos) =>
              groupedSquad[pos].map((p) => {
                const pending = pendingByOutId.get(p.player_id)
                return (
                  <div
                    className={`flex items-center justify-between p-sm gap-sm transition-colors squad-row ${
                      pending
                        ? 'bg-error-container/20'
                        : focusedOutId === p.player_id
                          ? 'bg-surface-container-low'
                          : 'hover:bg-surface-container-low'
                    }`}
                    key={p.player_id}
                  >
                    <div className="flex items-center gap-sm min-w-0">
                      <PlayerJersey player={p} size="xs" showName={false} />
                      <div className="min-w-0">
                        <div
                          className={`font-headline-sm text-[16px] leading-tight truncate ${
                            pending ? 'text-error line-through' : 'text-primary'
                          }`}
                        >
                          {p.name}
                        </div>
                        <div className="font-label-md text-[10px] text-on-surface-variant">
                          {p.club}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-3 shrink-0">
                      <div className="flex flex-col items-end leading-none">
                        <span className="font-stats-number text-stats-number text-on-background">
                          £{p.price.toFixed(1)}m
                        </span>
                        <span className="mt-1 font-label-md text-[10px] uppercase tracking-wide text-on-surface-variant">
                          {playerPoints(p)} PTS
                        </span>
                      </div>
                      {pending ? (
                        <button
                          aria-label={`Cancel transfer out for ${p.name}`}
                          className="w-8 h-8 rounded-full bg-error text-on-error flex items-center justify-center transition-colors squad-row__btn"
                          onClick={() => cancelPending(p.player_id)}
                          type="button"
                        >
                          <span className="material-symbols-outlined text-[18px]">close</span>
                        </button>
                      ) : (
                        <button
                          aria-label={`Transfer out ${p.name}`}
                          className="w-8 h-8 rounded-full bg-surface-variant text-on-surface-variant flex items-center justify-center hover:bg-error-container hover:text-error transition-colors squad-row__btn"
                          onClick={() => focusOut(p.player_id)}
                          type="button"
                        >
                          <span className="material-symbols-outlined text-[18px]">swap_horiz</span>
                        </button>
                      )}
                    </div>
                  </div>
                )
              })
            )}
          </div>
        </section>

        {focusedOutPlayer && (
          <section className="flex flex-col gap-sm bg-surface-container-low p-md rounded-xl border border-dashed border-outline-variant">
            <div className="flex justify-between items-end gap-sm mb-1">
              <h3 className="font-label-md text-label-md text-primary uppercase tracking-wider">
                Suggested ({focusedOutPlayer.position})
              </h3>
              <span className="font-label-md text-label-md text-on-surface-variant whitespace-nowrap">
                Max £{maxAffordable.toFixed(1)}m
              </span>
            </div>

            {suggested.length === 0 && (
              <p className="font-body-md text-body-md text-on-surface-variant">
                No affordable replacements found.
              </p>
            )}

            <div className="flex flex-col gap-2">
              {suggested.map((p) => (
                <ReplacementRow key={p.id} onSelect={() => selectReplacement(p)} player={p} />
              ))}
            </div>

            <button
              className="w-full py-2 mt-1 text-primary font-label-md text-label-md border border-outline-variant rounded-lg bg-surface-container-lowest hover:bg-surface-container-low transition-colors flex items-center justify-center gap-2"
              onClick={() => setBrowseExpanded((v) => !v)}
              type="button"
            >
              <span>Browse All Replacements</span>
              <span className="material-symbols-outlined text-[16px]">
                {browseExpanded ? 'expand_less' : 'expand_more'}
              </span>
            </button>

            {browseExpanded && (
              <div className="mt-2 flex flex-col gap-3">
                <div className="relative">
                  <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-on-surface-variant text-[20px]">
                    search
                  </span>
                  <input
                    className="w-full pl-10 pr-4 py-2 bg-surface-container-lowest border border-outline-variant rounded-lg font-body-md text-body-md text-on-surface focus:outline-none focus:border-secondary focus:ring-1 focus:ring-secondary"
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="Search players..."
                    type="text"
                    value={searchQuery}
                  />
                </div>
                <div className="flex gap-2 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                  {[
                    { key: 'price_desc', label: 'Price: High to Low' },
                    { key: 'price_asc', label: 'Price: Low to High' },
                    { key: 'opponent', label: 'Next Opponent' },
                  ].map((opt) => (
                    <button
                      className={`px-3 py-1 rounded-full font-label-md text-label-md whitespace-nowrap transition-colors ${
                        sortMode === opt.key
                          ? 'bg-primary-container text-on-primary'
                          : 'bg-surface-container-lowest border border-outline-variant text-on-surface'
                      }`}
                      key={opt.key}
                      onClick={() => setSortMode(opt.key)}
                      type="button"
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
                {/* Capped height: the pool can be hundreds of players, and an
                    unbounded list pushes the pending-summary bar off-screen. */}
                <div className="flex flex-col gap-2 max-h-[320px] overflow-y-auto">
                  {browseResults.length === 0 ? (
                    <p className="font-body-md text-body-md text-on-surface-variant">
                      No players match.
                    </p>
                  ) : (
                    browseResults.map((p) => (
                      <ReplacementRow
                        disabled={p.price > maxAffordable}
                        key={p.id}
                        onSelect={() => selectReplacement(p)}
                        player={p}
                      />
                    ))
                  )}
                </div>
              </div>
            )}
          </section>
        )}

        {draftError && (
          <div
            className="bg-error-container text-on-error-container rounded-lg p-sm font-label-md text-label-md leading-normal"
            data-testid="transfers-draft-error"
            role="alert"
          >
            {draftError}
          </div>
        )}
        {submitError && (
          <div
            className="bg-error-container text-on-error-container rounded-lg p-sm flex flex-col gap-xs"
            role="alert"
          >
            {submitError.map((msg) => (
              <p className="font-label-md text-label-md leading-normal" key={msg}>
                {msg}
              </p>
            ))}
          </div>
        )}
        {submitSuccess && (
          <div
            className="bg-secondary-container text-on-secondary-container rounded-lg p-sm font-body-md text-body-md transfers-page__success"
            data-testid="transfers-success"
          >
            {submitSuccess}
          </div>
        )}
      </main>

      {/* Pending summary sits above Layout's BottomNav. */}
      <div className="fixed bottom-[88px] left-1/2 -translate-x-1/2 w-full max-w-[600px] px-md z-40 pointer-events-none">
        <div className="bg-surface-container-lowest rounded-xl border border-outline-variant shadow-[0_-4px_12px_rgba(0,0,0,0.08)] p-md pointer-events-auto flex flex-col gap-sm">
          <div className="flex justify-between items-center pb-2 border-b border-outline-variant">
            <div className="text-center">
              <div className="font-label-md text-[10px] text-on-surface-variant uppercase">
                Transfers
              </div>
              <div className="font-stats-number text-[16px] text-primary">
                {pendingTransfers.length}{' '}
                <span className="text-[12px] font-normal text-on-surface-variant">
                  ({freeAppliedToPending} Free)
                </span>
              </div>
            </div>
            <div className="text-center">
              <div className="font-label-md text-[10px] text-on-surface-variant uppercase">Cost</div>
              <div className="font-stats-number text-[16px] text-primary">{costEstimate} pts</div>
            </div>
            <div className="text-center">
              <div className="font-label-md text-[10px] text-on-surface-variant uppercase">Bank</div>
              <div className="font-stats-number text-[16px] text-secondary">
                £{bankAfterAll.toFixed(1)}m
              </div>
            </div>
          </div>
          {locked ? (
            <div
              className="w-full bg-surface-container-high text-on-surface-variant rounded-lg py-3 font-headline-sm text-[16px] flex items-center justify-center gap-2"
              data-testid="transfers-locked"
            >
              <span className="material-symbols-outlined">lock</span>
              Deadline passed — transfers locked
            </div>
          ) : (
            <button
              className="w-full bg-primary-container text-on-primary rounded-lg py-3 font-headline-sm text-[16px] flex items-center justify-center gap-2 shadow-sm active:scale-95 transition-all disabled:opacity-50 disabled:active:scale-100 disabled:cursor-not-allowed"
              data-testid="confirm-transfers"
              disabled={pendingTransfers.length === 0 || submitting}
              onClick={handleConfirm}
              type="button"
            >
              <span>{submitting ? 'Confirming…' : 'Confirm Transfers'}</span>
              <span className="material-symbols-outlined text-[18px]">check_circle</span>
            </button>
          )}
        </div>
      </div>
    </>
  )
}

function ReplacementRow({ player, onSelect, disabled }) {
  return (
    <button
      className="replacement-row flex items-center justify-between p-sm bg-surface-container-lowest rounded-lg border border-outline-variant shadow-sm hover:border-secondary transition-colors disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:border-outline-variant w-full text-left"
      disabled={disabled}
      onClick={onSelect}
      type="button"
    >
      <div className="flex items-center gap-sm min-w-0">
        <PlayerJersey player={player} size="xs" showName={false} />
        <div className="min-w-0">
          <div className="font-headline-sm text-[16px] leading-tight text-primary truncate">
            {player.name}
          </div>
          <div className="font-label-md text-[10px] text-on-surface-variant truncate">
            {opponentLabel(player)}
          </div>
        </div>
      </div>
      <div className="flex flex-col items-end leading-none shrink-0 ml-2">
        <span className="font-stats-number text-stats-number text-on-background">
          £{player.price.toFixed(1)}m
        </span>
        <span className="mt-1 font-label-md text-[10px] uppercase tracking-wide text-on-surface-variant">
          {playerPoints(player)} PTS
        </span>
      </div>
    </button>
  )
}

export default TransfersPage
