import { useEffect, useMemo, useState } from 'react'
import BottomNav from '../../components/BottomNav/BottomNav'
import FplHeader from '../../components/FplHeader/FplHeader'
import PitchView from '../../components/PitchView/PitchView'
import PositionTabs from '../../components/PositionTabs/PositionTabs'
import PlayerCard from '../../components/PlayerCard/PlayerCard'
import ValidationBar from '../../components/ValidationBar/ValidationBar'
import { fetchPlayers } from '../../api/players'
import { fetchCurrentSquad, submitSquad } from '../../api/squad'
import { LockedError } from '../../api/client'
import { useAuth } from '../../auth/AuthContext'
import { useGameweek } from '../../config/gameweek'
import { normalizePremierLeaguePlayer } from '../../data/premierLeague2026'

const SQUAD_SIZE = 15
const BUDGET_CAP = 100.0
const POSITION_REQUIREMENTS = { GK: 2, DEF: 5, MID: 5, FWD: 3 }
const MAX_PER_CLUB = 3

// Prices arrive from the API already converted from ml.players.cost_start's
// x10 scale to a decimal (see Game_logic/players.py) -- summing the raw floats
// can drift, so budget maths here re-scales to integer tenths first and only
// converts back to a display decimal at the end.
function toTenths(price) {
  return Math.round(price * 10)
}

function SquadSelectionPage() {
  // Identity is the bearer token now -- the API modules send no user id at
  // all. `user` is still read here because the load effect below keys on
  // user.id: the page must refetch when the signed-in manager changes.
  const { user } = useAuth()
  const { season, gameweek } = useGameweek()

  const [players, setPlayers] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  const [selectedIds, setSelectedIds] = useState([])
  const [activePosition, setActivePosition] = useState('ALL')
  const [search, setSearch] = useState('')
  const [sortByPrice, setSortByPrice] = useState(null) // null | 'asc' | 'desc'

  const [submitting, setSubmitting] = useState(false)
  const [serverErrors, setServerErrors] = useState(null)
  const [locked, setLocked] = useState(false)
  const [submitSuccess, setSubmitSuccess] = useState(false)

  // Player pool and any already-saved squad load together: the saved squad's
  // ids are meaningless until the players are in hand (selectedPlayers resolves
  // ids through playersById), so one settled load avoids a flash of "0 / 15"
  // on a page that actually has a squad.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)

    Promise.all([
      fetchPlayers({ season, gameweek }),
      fetchCurrentSquad({ season, gameweek }),
    ])
      .then(([playersData, squadData]) => {
        if (cancelled) return
        // Squad Selection shows season-to-date form, not one gameweek's score
        // -- override points with season_points here, page-locally, rather
        // than in normalizePremierLeaguePlayer (shared with Transfers/
        // Dashboard/StartingXI, which must keep showing the single-gameweek
        // number they already rely on).
        setPlayers(
          playersData.map((p) => normalizePremierLeaguePlayer({ ...p, points: p.season_points }))
        )
        setSelectedIds((squadData.players ?? []).map((p) => p.player_id))
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [user.id, season, gameweek])

  const playersById = useMemo(() => new Map(players.map((p) => [p.id, p])), [players])

  const selectedPlayers = useMemo(
    () => selectedIds.map((id) => playersById.get(id)).filter(Boolean),
    [selectedIds, playersById]
  )

  const totalCostTenths = useMemo(
    () => selectedPlayers.reduce((sum, p) => sum + toTenths(p.price), 0),
    [selectedPlayers]
  )
  const budgetRemaining = (BUDGET_CAP * 10 - totalCostTenths) / 10
  const budgetValid = totalCostTenths <= BUDGET_CAP * 10

  const positionCounts = useMemo(() => {
    const counts = { GK: 0, DEF: 0, MID: 0, FWD: 0 }
    for (const p of selectedPlayers) counts[p.position] = (counts[p.position] || 0) + 1
    return counts
  }, [selectedPlayers])
  const formationValid = Object.entries(POSITION_REQUIREMENTS).every(
    ([pos, required]) => positionCounts[pos] === required
  )

  const clubCounts = useMemo(() => {
    const counts = {}
    for (const p of selectedPlayers) counts[p.teamId ?? p.club] = (counts[p.teamId ?? p.club] || 0) + 1
    return counts
  }, [selectedPlayers])
  const clubValid = Object.values(clubCounts).every((n) => n <= MAX_PER_CLUB)

  const countValid = selectedPlayers.length === SQUAD_SIZE
  const isValid = countValid && formationValid && budgetValid && clubValid

  const filteredPlayers = useMemo(() => {
    let list = players
    if (activePosition !== 'ALL') list = list.filter((p) => p.position === activePosition)
    if (search.trim()) {
      const q = search.trim().toLowerCase()
      list = list.filter(
        (p) => p.name.toLowerCase().includes(q) || p.club.toLowerCase().includes(q)
      )
    }
    if (sortByPrice) {
      list = [...list].sort((a, b) => (sortByPrice === 'asc' ? a.price - b.price : b.price - a.price))
    }
    return list
  }, [players, activePosition, search, sortByPrice])

  function toggleSelect(id) {
    setServerErrors(null)
    setSubmitSuccess(false)
    setSelectedIds((prev) => {
      if (prev.includes(id)) return prev.filter((pid) => pid !== id)
      if (prev.length >= SQUAD_SIZE) return prev
      return [...prev, id]
    })
  }

  function toggleSortByPrice() {
    setSortByPrice((prev) => (prev === 'asc' ? 'desc' : prev === 'desc' ? null : 'asc'))
  }

  async function handleSubmit() {
    if (!isValid) return
    setSubmitting(true)
    setServerErrors(null)
    setSubmitSuccess(false)

    try {
      await submitSquad({ season, player_ids: selectedIds })
      setSubmitSuccess(true)
    } catch (err) {
      // Client-side checks above are for instant feedback only -- the backend's
      // validation (Game_logic/squad_selection.py) is the source of truth, so
      // its errors always win if they ever disagree.
      if (err instanceof LockedError) setLocked(true)
      setServerErrors(err.errors ?? [err.message])
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="bg-background text-on-background font-body-md antialiased min-h-screen">
      <div className="max-w-[600px] mx-auto bg-surface min-h-screen relative">
        <FplHeader title="Squad Selection" />

        <div className="sticky top-16 z-40 bg-surface/95 backdrop-blur-md border-b border-outline-variant px-md py-sm flex justify-between items-end shadow-sm">
          <div className="flex flex-col">
            <span className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-1">
              Remaining Budget
            </span>
            <span
              className={`font-display-lg text-display-lg ${budgetValid ? 'text-primary-container' : 'text-error'}`}
              data-testid="budget"
            >
              £{budgetRemaining.toFixed(1)}m
            </span>
          </div>
          <div className="flex flex-col items-end pb-1">
            <span className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-1">
              Squad
            </span>
            <div className="bg-surface-container-highest px-3 py-1 rounded-full">
              <span className="font-stats-number text-stats-number text-primary" data-testid="squad-count">
                {selectedPlayers.length} / 15
              </span>
            </div>
          </div>
        </div>

        {/* pb clears the ValidationBar (above) and BottomNav (below it). */}
        <main className="pb-[220px]">
          <section className="p-md">
            <PitchView onEmptySlotClick={setActivePosition} selectedPlayers={selectedPlayers} />
          </section>

          <section className="sticky top-[132px] z-30 bg-surface/95 backdrop-blur-md pt-sm pb-md border-b border-outline-variant shadow-sm">
            <PositionTabs active={activePosition} onChange={setActivePosition} />
            <div className="px-md flex gap-sm">
              <div className="relative flex-grow">
                <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-outline">
                  search
                </span>
                <input
                  className="w-full pl-10 pr-4 py-2 rounded-lg bg-surface-container-lowest border border-outline-variant focus:border-secondary focus:ring-1 focus:ring-secondary outline-none font-body-md text-body-md text-on-surface placeholder:text-outline transition-all"
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search players..."
                  type="text"
                  value={search}
                />
              </div>
              <button
                aria-label="Sort by price"
                className={`px-4 py-2 rounded-lg border flex items-center justify-center transition-colors ${
                  sortByPrice
                    ? 'bg-primary-container text-on-primary border-primary-container'
                    : 'bg-surface-container-lowest border-outline-variant text-on-surface hover:bg-surface-container-low'
                }`}
                onClick={toggleSortByPrice}
                title={
                  sortByPrice === 'asc'
                    ? 'Price: low to high'
                    : sortByPrice === 'desc'
                      ? 'Price: high to low'
                      : 'Sort by price'
                }
                type="button"
              >
                <span className="material-symbols-outlined">filter_list</span>
              </button>
            </div>
          </section>

          <section className="px-md py-sm flex flex-col gap-sm">
            {loading && (
              <p className="font-body-md text-body-md text-on-surface-variant">Loading players…</p>
            )}
            {loadError && (
              <p className="font-body-md text-body-md text-error" role="alert">
                {loadError}
              </p>
            )}
            {!loading && !loadError && filteredPlayers.length === 0 && (
              <p className="font-body-md text-body-md text-on-surface-variant">
                No players match your search.
              </p>
            )}
            {!loading &&
              !loadError &&
              filteredPlayers.map((player) => (
                <PlayerCard
                  addDisabled={selectedPlayers.length >= SQUAD_SIZE}
                  key={player.id}
                  onToggle={toggleSelect}
                  player={player}
                  selected={selectedIds.includes(player.id)}
                  showPoints
                />
              ))}
          </section>
        </main>

        {submitSuccess && (
          <div
            className="fixed bottom-[190px] left-1/2 -translate-x-1/2 z-50 w-[calc(100%-32px)] max-w-[520px] bg-secondary-container text-on-secondary-container px-md py-sm rounded-[20px] shadow-md text-center"
            data-testid="squad-saved-toast"
          >
            <p className="font-body-md text-body-md leading-relaxed">
              <strong>Squad saved — £{budgetRemaining.toFixed(1)}m remaining.</strong>{' '}
              Now go back to the Home screen and select your Starting XI squad.
            </p>
          </div>
        )}

        <ValidationBar
          budgetValid={budgetValid}
          clubValid={clubValid}
          countValid={countValid}
          isValid={isValid}
          locked={locked}
          onSubmit={handleSubmit}
          selectedCount={selectedPlayers.length}
          serverErrors={serverErrors}
          submitting={submitting}
        />
        <BottomNav />
      </div>
    </div>
  )
}

export default SquadSelectionPage
