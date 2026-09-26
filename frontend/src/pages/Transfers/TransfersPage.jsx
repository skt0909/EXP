import { useEffect, useMemo, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import FplHeader from '../../components/FplHeader/FplHeader'
import { fetchCurrentSquad } from '../../api/squad'
import { fetchPlayers } from '../../api/players'
import { fetchTransferHistory, fetchTransfersUsed, submitTransfers } from '../../api/transfers'
import {
  deleteTransferDraft,
  fetchTransferDrafts,
  putTransferDraft,
} from '../../api/transferDrafts'
import { LockedError } from '../../api/client'
import PlayerJersey from '../../components/PlayerJersey/PlayerJersey'
import DashboardIcon from '../../components/DashboardIcon/DashboardIcon'
import { normalizePremierLeaguePlayer } from '../../data/premierLeague2026'
import { kickoffLabel } from '../../data/kickoff'

const POSITION_ORDER = ['GK', 'DEF', 'MID', 'FWD']

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

// The pitch shows the whole 15-man squad (2/5/5/3), not a Starting XI +
// bench split -- Transfers operates on the squad as a unit, independent of
// who's in this gameweek's XI, so there's no bench-vs-starting distinction
// to draw here at all.
function PitchPlayer({ player, isOutgoing, isPending, onClick }) {
  return (
    <button
      className="flex flex-col items-center relative active:scale-95 transition-transform"
      onClick={onClick}
      type="button"
    >
      {isOutgoing && (
        <span className="absolute -top-4 left-1/2 -translate-x-1/2 z-20 bg-[#B3261E] text-white text-[9px] font-bold px-2 py-1 rounded-full shadow-md flex items-center gap-1 whitespace-nowrap">
          <DashboardIcon name="transfers" size={11} strokeWidth={2.4} />
          OUT
        </span>
      )}
      <div className={`relative ${isPending ? 'opacity-60 grayscale' : ''}`}>
        <PlayerJersey player={player} size={isOutgoing ? 'lg' : 'md'} />
      </div>
      <span
        className={`mt-0.5 px-1.5 py-0.5 rounded font-label-md text-[9px] font-bold uppercase truncate max-w-[64px] ${
          isOutgoing
            ? 'bg-[#B3261E] text-white ring-2 ring-white/80'
            : isPending
              ? 'bg-error-container text-on-error-container line-through'
              : 'bg-surface-container-lowest text-on-surface'
        }`}
      >
        £{player.price.toFixed(1)}m
      </span>
      <span className="font-label-md text-[9px] text-white drop-shadow">{player.position}</span>
    </button>
  )
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
  const [transferHistory, setTransferHistory] = useState([])
  const [historyOpen, setHistoryOpen] = useState(true)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  const [pendingTransfers, setPendingTransfers] = useState([]) // [{id, outId, inId}], server-backed
  const [draftError, setDraftError] = useState(null)
  const [focusedOutId, setFocusedOutId] = useState(null)
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

  function reloadTransferHistory() {
    return fetchTransferHistory({ season }).then((data) => setTransferHistory(data.transfers ?? []))
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
      fetchTransferHistory({ season }),
    ])
      .then(([squadData, playersData, transfersUsedData, draftsData, historyData]) => {
        if (cancelled) return
        setSquad({
          ...squadData,
          players: (squadData.players ?? []).map(normalizePremierLeaguePlayer),
        })
        setAllPlayers(playersData.map(normalizePremierLeaguePlayer))
        setTransfersUsed(transfersUsedData)
        setTransferHistory(historyData.transfers ?? [])
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
  // transfers already committed this gameweek, not just what's pending here.
  const freeRemaining = transfersUsed?.free_transfers_remaining ?? 0
  const freeAppliedToPending = Math.min(pendingTransfers.length, freeRemaining)
  const overLimitCount = Math.max(0, pendingTransfers.length - freeRemaining)

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
      await reloadTransferHistory()
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
      <FplHeader helpTo="/scoring" showHelp title="Transfers" />
      <main className="w-full px-4 py-3 flex flex-col gap-3 pb-[200px] bg-[#FBF9F5] min-h-screen">
        <div className="bg-white rounded-[20px] p-3 border border-[#E5E6E1] shadow-sm flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="font-label-md text-label-md px-3 py-1.5 rounded-full bg-[#F0F0EA] text-on-surface font-bold">
              GW {gameweek} Active
            </span>
            <span className="font-label-md text-label-md px-3 py-1.5 rounded-full bg-[#EDF2DF] text-[#667D28] font-bold">
              {freeRemaining} Free Transfer{freeRemaining === 1 ? '' : 's'}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3 pt-1">
            <div className="bg-white rounded-[20px] border border-[#E5E6E1] p-4 text-left shadow-sm">
              <span className="font-label-md text-[10px] uppercase tracking-wider text-on-surface-variant block">Bank Balance</span>
              <span className="font-stats-number text-stats-number text-secondary">£{bankAfterAll.toFixed(1)}m</span>
            </div>
            <div className="bg-white rounded-[20px] border border-[#E5E6E1] p-4 text-left shadow-sm">
              <span className="font-label-md text-[10px] uppercase tracking-wider text-on-surface-variant block">Transfers Remaining</span>
              <span className={`mt-2 block font-stats-number text-stats-number tabular-nums ${overLimitCount ? 'text-error' : 'text-[#667D28]'}`}>
                {pendingTransfers.length > 0 ? `${pendingTransfers.length} used` : `${freeRemaining} Free`}
              </span>
            </div>
          </div>
        </div>

        {/* Tap any player to transfer him out. The pitch shows the whole
            15-man squad (2/5/5/3) -- Transfers acts on the squad, not this
            gameweek's Starting XI, so there's no bench/XI split to draw. */}
        <section
          className="relative min-h-[590px] rounded-[24px] overflow-hidden shadow-sm border border-[#E5E6E1] bg-gradient-to-b from-[#086834] to-[#0F7B42] flex flex-col justify-around py-6 gap-sm"
        >
          <div aria-hidden="true" className="pointer-events-none absolute inset-3 rounded-[18px] border border-white/40">
            <span className="absolute left-0 right-0 top-1/2 border-t border-white/40" />
            <span className="absolute left-1/2 top-1/2 h-24 w-24 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/40" />
            <span className="absolute left-1/2 top-0 h-16 w-36 -translate-x-1/2 border-x border-b border-white/40" />
            <span className="absolute bottom-0 left-1/2 h-16 w-36 -translate-x-1/2 border-x border-t border-white/40" />
            <span className="absolute left-1/2 top-1/2 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white/60" />
          </div>
          {POSITION_ORDER.map((pos) => (
            <div className="relative z-10 flex justify-around flex-wrap gap-x-2 gap-y-3 px-sm" key={pos}>
              {groupedSquad[pos].map((p) => {
                const pending = pendingByOutId.get(p.player_id)
                return (
                  <PitchPlayer
                    isOutgoing={focusedOutId === p.player_id}
                    isPending={Boolean(pending)}
                    key={p.player_id}
                    onClick={() => (pending ? cancelPending(p.player_id) : focusOut(p.player_id))}
                    player={p}
                  />
                )
              })}
            </div>
          ))}
        </section>

        {transferHistory.length > 0 && (
          <section className="bg-surface-container-lowest rounded-xl border border-outline-variant shadow-sm overflow-hidden">
            <button
              className="w-full flex items-center justify-between p-md"
              onClick={() => setHistoryOpen((prev) => !prev)}
              type="button"
            >
              <div className="flex items-center gap-xs">
                <span className="material-symbols-outlined text-[18px] text-on-surface-variant">history</span>
                <span className="font-headline-sm text-body-md text-on-surface">Transfer History</span>
                <span className="font-label-md text-[10px] px-1.5 py-0.5 rounded-full bg-surface-container text-on-surface-variant">
                  {transferHistory.length}
                </span>
              </div>
              <span className="material-symbols-outlined text-[20px] text-on-surface-variant">
                {historyOpen ? 'expand_less' : 'expand_more'}
              </span>
            </button>
            {historyOpen && (
              <div className="flex flex-col gap-2 px-md pb-md max-h-[320px] overflow-y-auto">
                {transferHistory.map((t, i) => (
                  <div
                    className="flex items-center justify-between gap-sm p-sm rounded-lg bg-surface-container-low"
                    key={`${t.gameweek}-${t.player_in_id}-${t.player_out_id}-${i}`}
                  >
                    <div className="flex items-center gap-xs min-w-0">
                      <span className="font-body-md text-body-md font-bold text-on-surface truncate">
                        {t.player_out_name}
                      </span>
                      <span className="material-symbols-outlined text-[14px] text-on-surface-variant shrink-0">
                        arrow_forward
                      </span>
                      <span className="font-body-md text-body-md font-bold text-secondary truncate">
                        {t.player_in_name}
                      </span>
                    </div>
                    <div className="flex items-center gap-xs shrink-0">
                      <span className="font-label-md text-[10px] text-on-surface-variant whitespace-nowrap">
                        GW{t.gameweek} · {kickoffLabel(t.transferred_at)}
                      </span>
                      <span
                        className={`font-label-md text-[10px] px-1.5 py-0.5 rounded-full whitespace-nowrap ${
                          t.is_free
                            ? 'bg-secondary-container text-on-secondary-container'
                            : 'bg-error-container text-on-error-container'
                        }`}
                      >
                        {t.is_free ? 'Free' : 'Paid'}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

        {focusedOutPlayer && (
          <section className="flex flex-col gap-sm bg-surface-container-low p-md rounded-xl border border-dashed border-outline-variant">
            <div className="flex justify-between items-end gap-sm mb-1">
              <h3 className="font-headline-sm text-headline-sm text-on-surface">
                Replacement for {focusedOutPlayer.name}
              </h3>
              <span className="font-label-md text-label-md text-on-surface-variant whitespace-nowrap">
                Max £{maxAffordable.toFixed(1)}m
              </span>
            </div>

            {/* Always visible -- no collapsed "Browse All" step. candidatePool
                is already every player at this position not owned or already
                staged in; sort/search only reorder or narrow it. */}
            <div className="relative">
              <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-on-surface-variant text-[20px]">
                search
              </span>
              <input
                className="w-full pl-10 pr-4 py-2 bg-surface-container-lowest border border-outline-variant rounded-lg font-body-md text-body-md text-on-surface focus:outline-none focus:border-secondary focus:ring-1 focus:ring-secondary"
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder={`Search ${focusedOutPlayer.position} replacements...`}
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
            <span className="font-label-md text-[11px] text-on-surface-variant">
              {browseResults.length} {focusedOutPlayer.position} available
              {searchQuery ? ` matching "${searchQuery}"` : ''}
            </span>
            {/* Capped height, not hidden: the pool can be hundreds of
                players, and an unbounded list pushes the pending-summary
                bar off-screen -- it still scrolls, it's just never
                collapsed behind an extra tap. */}
            <div className="flex flex-col gap-2 max-h-[400px] overflow-y-auto">
              {browseResults.length === 0 ? (
                <p className="font-body-md text-body-md text-on-surface-variant">
                  No players match.
                </p>
              ) : (
                browseResults.map((p) => (
                  <ReplacementRow
                    disabled={p.price > maxAffordable}
                    gameweek={gameweek}
                    key={p.id}
                    onSelect={() => selectReplacement(p)}
                    outgoingPrice={focusedOutPlayer.price}
                    player={p}
                    selected={pendingByOutId.get(focusedOutId)?.inId === p.id}
                  />
                ))
              )}
            </div>
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
          {pendingTransfers.length > 0 && (
            <div className="flex flex-col gap-xs pb-2 border-b border-outline-variant">
              <span className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
                Transfer Summary
              </span>
              {pendingTransfers.map((t) => {
                const outP = squadById.get(t.outId)
                const inP = playersById.get(t.inId)
                if (!outP || !inP) return null
                return (
                  <div className="flex items-center justify-between" key={t.id}>
                    <div className="flex items-center gap-xs min-w-0">
                      <span className="w-5 h-5 rounded-full bg-error-container text-on-error-container flex items-center justify-center text-[12px] font-bold shrink-0">
                        −
                      </span>
                      <span className="font-body-md text-body-md font-bold text-on-surface truncate">
                        {outP.name}
                      </span>
                    </div>
                    <span className="material-symbols-outlined text-[16px] text-primary-container shrink-0 mx-xs">
                      sync_alt
                    </span>
                    <div className="flex items-center gap-xs min-w-0 justify-end">
                      <span className="font-body-md text-body-md font-bold text-secondary truncate">
                        {inP.name}
                      </span>
                      <span className="w-5 h-5 rounded-full bg-secondary-container text-on-secondary-container flex items-center justify-center text-[12px] font-bold shrink-0">
                        +
                      </span>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
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
              <div className="font-label-md text-[10px] text-on-surface-variant uppercase">Limit</div>
              <div className={`font-stats-number text-[16px] ${overLimitCount ? 'text-error' : 'text-primary'}`}>
                {overLimitCount ? `${overLimitCount} over` : 'OK'}
              </div>
            </div>
            <div className="text-center">
              <div className="font-label-md text-[10px] text-on-surface-variant uppercase">New Bank</div>
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
              disabled={pendingTransfers.length === 0 || overLimitCount > 0 || submitting}
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

function ReplacementRow({ player, onSelect, disabled, outgoingPrice, selected, gameweek }) {
  // Positive = cheaper than the outgoing player (frees up bank); negative =
  // costs more. Real numbers only -- this app has no FPL-style rolling
  // "form" stat, so the card shows the same recent-form proxy Transfers
  // already used (this gameweek's points) plus the season total, rather
  // than inventing a decimal form rating the mockup showed.
  const delta = outgoingPrice != null ? outgoingPrice - player.price : null

  return (
    <button
      className={`replacement-row flex items-center justify-between p-md rounded-xl shadow-sm transition-colors disabled:opacity-40 disabled:cursor-not-allowed w-full text-left ${
        selected
          ? 'bg-gradient-to-r from-secondary-container/20 to-surface-container-lowest border border-secondary-container'
          : 'bg-surface-container-lowest border border-outline-variant hover:bg-surface-container-low'
      }`}
      disabled={disabled}
      onClick={onSelect}
      type="button"
    >
      <div className="flex items-center gap-md min-w-0">
        <PlayerJersey player={player} size="sm" showName={false} />
        <div className="min-w-0">
          <div className="font-headline-sm text-body-lg font-bold text-on-surface truncate">
            {player.name}
          </div>
          <div className="flex items-center gap-sm mt-0.5 text-on-surface-variant font-label-md text-[11px]">
            <span>
              GW{gameweek}: <strong className="text-on-surface">{playerPoints(player)}</strong>
            </span>
            <span>
              Season: <strong className="text-on-surface">{player.season_points ?? 0}</strong>
            </span>
          </div>
          <div className="font-label-md text-[10px] text-on-surface-variant truncate">
            {opponentLabel(player)}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-md shrink-0 ml-2">
        <div className="text-right">
          <span className="font-stats-number text-stats-number text-on-surface block">
            £{player.price.toFixed(1)}m
          </span>
          {delta != null && delta !== 0 && (
            <span className={`font-label-md text-[10px] ${delta > 0 ? 'text-secondary' : 'text-error'}`}>
              {delta > 0 ? `Saves £${delta.toFixed(1)}m` : `Costs £${Math.abs(delta).toFixed(1)}m`}
            </span>
          )}
        </div>
        <span
          className={`w-8 h-8 rounded-full flex items-center justify-center shadow-sm ${
            selected
              ? 'bg-secondary-container text-on-secondary-container'
              : 'bg-surface-container-high text-on-surface'
          }`}
        >
          <span className="material-symbols-outlined text-[20px]" style={selected ? { fontVariationSettings: "'FILL' 1" } : undefined}>
            {selected ? 'check' : 'add'}
          </span>
        </span>
      </div>
    </button>
  )
}

export default TransfersPage
