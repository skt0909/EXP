import { useEffect, useMemo, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import FplHeader from '../../components/FplHeader/FplHeader'
import DashboardIcon from '../../components/DashboardIcon/DashboardIcon'
import {
  createLeague,
  fetchLeagueTable,
  fetchUserLeagues,
  joinLeague,
  LeagueValidationError,
} from '../../api/leagues'

function normalizeErrors(err) {
  if (err instanceof LeagueValidationError) return err.errors
  return [err.message || 'Something went wrong']
}

const FIELD =
  'rounded-lg border border-outline-variant bg-surface px-3 py-2 font-body-md text-body-md text-on-surface ' +
  'focus:border-secondary focus:ring-1 focus:ring-secondary outline-none h-10'

function LeaguesPage() {
  const { settings } = useOutletContext()
  const { user_id, season } = settings

  const [leagues, setLeagues] = useState([])
  const [selectedLeagueId, setSelectedLeagueId] = useState(null)
  const [table, setTable] = useState(null)
  const [loading, setLoading] = useState(true)
  const [tableLoading, setTableLoading] = useState(false)
  const [errors, setErrors] = useState([])
  const [notice, setNotice] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [createForm, setCreateForm] = useState({
    name: '',
    league_type: 'private',
    scoring_type: 'classic',
    max_members: 50,
  })
  const [joinCode, setJoinCode] = useState('')

  async function loadLeagues(nextSelectedId = null) {
    const data = await fetchUserLeagues({ season })
    setLeagues(data)

    const desiredId = nextSelectedId ?? selectedLeagueId
    const nextLeague = data.find((league) => league.league_id === desiredId) ?? data[0] ?? null
    setSelectedLeagueId(nextLeague?.league_id ?? null)
    if (!nextLeague) setTable(null)
    return nextLeague
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setErrors([])

    fetchUserLeagues({ season })
      .then((data) => {
        if (cancelled) return
        setLeagues(data)
        setSelectedLeagueId(data[0]?.league_id ?? null)
      })
      .catch((err) => {
        if (!cancelled) setErrors(normalizeErrors(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [user_id, season])

  useEffect(() => {
    if (!selectedLeagueId) return

    let cancelled = false
    setTableLoading(true)
    setErrors([])

    fetchLeagueTable({ league_id: selectedLeagueId })
      .then((data) => {
        if (!cancelled) setTable(data)
      })
      .catch((err) => {
        if (!cancelled) setErrors(normalizeErrors(err))
      })
      .finally(() => {
        if (!cancelled) setTableLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [selectedLeagueId, user_id])

  const selectedLeague = useMemo(
    () => leagues.find((league) => league.league_id === selectedLeagueId) ?? null,
    [leagues, selectedLeagueId]
  )

  async function handleCreate(event) {
    event.preventDefault()
    setSubmitting(true)
    setErrors([])
    setNotice('')

    try {
      const created = await createLeague({
        season,
        name: createForm.name.trim(),
        league_type: createForm.league_type,
        scoring_type: createForm.scoring_type,
        max_members: Number(createForm.max_members),
      })
      await loadLeagues(created.league_id)
      setCreateForm((form) => ({ ...form, name: '' }))
      setNotice(`Created ${created.name}. Code ${created.code}`)
    } catch (err) {
      setErrors(normalizeErrors(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleJoin(event) {
    event.preventDefault()
    setSubmitting(true)
    setErrors([])
    setNotice('')

    try {
      const joined = await joinLeague({ code: joinCode.trim().toUpperCase() })
      await loadLeagues(joined.league_id)
      setJoinCode('')
      setNotice(`Joined ${joined.name}.`)
    } catch (err) {
      setErrors(normalizeErrors(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <FplHeader title="Leagues" />
      <main className="w-full px-4 py-3 pb-28 flex flex-col gap-4 bg-[#FBF9F5] min-h-screen">
        {/* Season label and the joined-count subtitle are real, useful
            context -- just not what the <h1> is for. They now sit below
            the header bar as their own element, same as any other page's
            secondary descriptive text. */}
        <div>
          <p className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
            {season}
          </p>
          <p className="font-body-md text-body-md text-on-surface-variant mt-sm">
            {leagues.length} joined · manage your private and public FPL leagues.
          </p>
        </div>

        {errors.length > 0 && (
        <div
          className="bg-error-container text-on-error-container rounded-lg p-sm flex flex-col gap-xs"
          role="alert"
        >
          {errors.map((message) => (
            <p className="font-label-md text-label-md leading-normal" key={message}>
              {message}
            </p>
          ))}
        </div>
      )}
      {notice && (
        <div
          className="bg-secondary-container text-on-secondary-container rounded-lg p-sm font-body-md text-body-md"
          data-testid="leagues-notice"
        >
          {notice}
        </div>
      )}

      <section aria-label="League actions" className="grid grid-cols-1 gap-md">
        <form
          className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm flex flex-col gap-sm"
          onSubmit={handleJoin}
        >
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-full bg-surface-container-high flex items-center justify-center text-primary">
              <span className="material-symbols-outlined text-[20px]">login</span>
            </div>
            <h2 className="font-headline-sm text-headline-sm text-primary">Join League</h2>
          </div>
          <div className="flex gap-2">
            <input
              className={`${FIELD} flex-1 min-w-0 uppercase placeholder:normal-case placeholder:text-outline`}
              data-testid="join-code"
              onChange={(event) => setJoinCode(event.target.value.toUpperCase())}
              placeholder="Enter code (e.g. X7Y9Z2)"
              required
              value={joinCode}
            />
            <button
              className="bg-primary text-on-primary font-label-md text-label-md px-4 rounded-lg h-10 hover:opacity-90 transition-opacity flex items-center justify-center shrink-0 disabled:opacity-50"
              disabled={submitting}
              type="submit"
            >
              {submitting ? 'Joining…' : 'Join'}
            </button>
          </div>
        </form>

        <form
          className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm flex flex-col gap-sm"
          onSubmit={handleCreate}
        >
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-full bg-primary-container text-on-primary-container flex items-center justify-center">
              <span className="material-symbols-outlined text-[20px]">add</span>
            </div>
            <h2 className="font-headline-sm text-headline-sm text-primary">Create League</h2>
          </div>
          <input
            className={`${FIELD} w-full`}
            data-testid="league-name"
            minLength={1}
            onChange={(event) => setCreateForm((form) => ({ ...form, name: event.target.value }))}
            placeholder="League name"
            required
            value={createForm.name}
          />
          <div className="grid grid-cols-3 gap-2">
            <select
              aria-label="League type"
              className={`${FIELD} px-2`}
              onChange={(event) =>
                setCreateForm((form) => ({ ...form, league_type: event.target.value }))
              }
              value={createForm.league_type}
            >
              <option value="private">Private</option>
              <option value="public">Public</option>
            </select>
            <select
              aria-label="Scoring type"
              className={`${FIELD} px-2`}
              onChange={(event) =>
                setCreateForm((form) => ({ ...form, scoring_type: event.target.value }))
              }
              value={createForm.scoring_type}
            >
              <option value="classic">Classic</option>
              <option value="head_to_head">H2H</option>
            </select>
            <input
              aria-label="Maximum members"
              className={`${FIELD} px-2`}
              min="1"
              onChange={(event) =>
                setCreateForm((form) => ({ ...form, max_members: event.target.value }))
              }
              type="number"
              value={createForm.max_members}
            />
          </div>
          <button
            className="w-full bg-primary-container text-on-primary rounded-lg py-2.5 font-label-md text-label-md flex items-center justify-center gap-2 shadow-sm active:scale-[0.98] transition-all disabled:opacity-50"
            data-testid="create-league"
            disabled={submitting}
            type="submit"
          >
            <span className="material-symbols-outlined text-[18px]">add_circle</span>
            {submitting ? 'Saving…' : 'Create'}
          </button>
        </form>
      </section>

      {loading ? (
        <p className="font-body-md text-body-md text-on-surface-variant">Loading leagues…</p>
      ) : (
        <>
          <section aria-label="My leagues" className="flex flex-col gap-3">
            <h2 className="font-headline-sm text-headline-sm text-on-surface">My Leagues</h2>
            {leagues.length === 0 ? (
              <div className="flex flex-col items-center rounded-[20px] border border-[#E5E6E1] bg-white px-5 py-8 text-center shadow-sm">
                <span className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-[#F0F0EA] text-on-surface-variant">
                  <DashboardIcon name="trophy" size={23} />
                </span>
                <h3 className="font-headline-sm text-body-md text-on-surface">No leagues joined yet</h3>
                <p className="mt-2 max-w-[280px] font-body-md text-body-md leading-relaxed text-on-surface-variant">
                  Create your own competition or enter an invite code above.
                </p>
              </div>
            ) : (
              <div className="flex flex-col gap-sm">
                {leagues.map((league) => (
                  <button
                    className={`bg-surface-container-lowest rounded-xl p-md border shadow-sm hover:shadow-md transition-shadow relative overflow-hidden text-left ${
                      league.league_id === selectedLeagueId
                        ? 'border-primary-container'
                        : 'border-outline-variant'
                    }`}
                    data-testid="league-card"
                    key={league.league_id}
                    onClick={() => setSelectedLeagueId(league.league_id)}
                    type="button"
                  >
                    {/* Accent bar distinguishes the two scoring types at a glance. */}
                    <div
                      className={`absolute left-0 top-0 bottom-0 w-1 ${
                        league.scoring_type === 'head_to_head' ? 'bg-secondary' : 'bg-primary'
                      }`}
                    />
                    <div className="flex justify-between items-start gap-sm pl-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 mb-1 flex-wrap">
                          <h3 className="font-headline-sm text-headline-sm text-primary truncate">
                            {league.name}
                          </h3>
                          <span className="bg-surface-container-high text-on-surface-variant px-2 py-0.5 rounded text-[10px] font-bold tracking-wider uppercase whitespace-nowrap">
                            {league.scoring_type === 'head_to_head' ? 'H2H' : 'Classic'}
                          </span>
                        </div>
                        <p className="font-body-md text-body-md text-on-surface-variant">
                          {league.user_rank != null
                            ? `Rank ${league.user_rank} of ${league.member_count}`
                            : `${league.member_count}/${league.max_members} members`}
                        </p>
                        <p className="font-label-md text-label-md text-on-surface-variant mt-1">
                          Code {league.code}
                        </p>
                      </div>
                      <div className="text-right shrink-0">
                        <p className="font-stats-number text-stats-number text-primary">
                          {league.user_season_points}
                        </p>
                        <p className="font-label-md text-label-md text-on-surface-variant opacity-70">
                          Total Pts
                        </p>
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </section>

          <button
            className="flex w-full items-center justify-between rounded-[20px] border border-[#E5E6E1] bg-white p-4 text-left shadow-sm transition-all hover:bg-[#F7F7F2] active:scale-[0.99] disabled:cursor-default disabled:opacity-60"
            disabled={!selectedLeague}
            onClick={() => document.getElementById('league-standings')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
            type="button"
          >
            <span className="flex min-w-0 items-center gap-3">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-[#EDF2DF] text-[#78952D]">
                <DashboardIcon name="leagues" size={20} />
              </span>
              <span className="min-w-0">
                <span className="block font-label-md text-label-md font-bold text-on-surface">League Table</span>
                <span className="mt-0.5 block truncate font-body-md text-body-md text-on-surface-variant">
                  {selectedLeague ? `View ${selectedLeague.name} standings` : 'Choose a league to view its table'}
                </span>
              </span>
            </span>
            <DashboardIcon className="shrink-0 text-on-surface-variant" name="chevronRight" size={20} />
          </button>

          {selectedLeague && (
          <section className="flex flex-col gap-md scroll-mt-20" id="league-standings">
            <h2 className="font-headline-sm text-headline-sm text-primary flex items-center gap-2">
              <span className="material-symbols-outlined text-[20px] text-outline">table_rows</span>
              {selectedLeague?.name ?? 'League Table'}
            </h2>

            {tableLoading ? (
              <p className="font-body-md text-body-md text-on-surface-variant">Loading table…</p>
            ) : table?.rows?.length ? (
              <div className="bg-surface-container-lowest rounded-xl border border-outline-variant shadow-sm overflow-hidden">
                {/* Table scrolls inside its own container so the page itself
                    never scrolls sideways at 390px. */}
                <div className="overflow-x-auto">
                  <table className="w-full text-left border-collapse" data-testid="league-table">
                    <thead>
                      <tr className="bg-surface-container-low border-b border-outline-variant">
                        <th className="py-3 px-4 font-label-md text-label-md text-on-surface-variant w-12">
                          #
                        </th>
                        <th className="py-3 px-4 font-label-md text-label-md text-on-surface-variant">
                          Manager / Team
                        </th>
                        <th className="py-3 px-4 font-label-md text-label-md text-on-surface-variant text-right">
                          GW
                        </th>
                        <th className="py-3 px-4 font-label-md text-label-md text-on-surface-variant text-right">
                          TOT
                        </th>
                      </tr>
                    </thead>
                    <tbody className="font-body-md text-body-md text-on-surface">
                      {table.rows.map((row, index) => {
                        const isMe = row.user_id === user_id
                        return (
                          <tr
                            className={`border-b border-outline-variant/50 transition-colors ${
                              isMe ? 'bg-primary/5' : 'hover:bg-surface'
                            }`}
                            key={row.user_id}
                          >
                            <td className="py-3 px-4 font-bold">{row.rank || index + 1}</td>
                            <td className="py-3 px-4">
                              <div className="font-bold text-primary flex items-center gap-2">
                                {row.team_name ?? `User ${row.user_id}`}
                                {isMe && (
                                  <span className="bg-primary text-on-primary text-[9px] px-1.5 py-0.5 rounded">
                                    ME
                                  </span>
                                )}
                              </div>
                              <div className="text-on-surface-variant text-xs">
                                {row.username ?? `User ${row.user_id}`}
                              </div>
                            </td>
                            <td className="py-3 px-4 text-right">{row.last_gw_points}</td>
                            <td className="py-3 px-4 text-right font-stats-number text-stats-number text-primary">
                              {row.season_points}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <p className="font-body-md text-body-md text-on-surface-variant">
                No standings are available for this league yet.
              </p>
            )}
          </section>
          )}
        </>
      )}
      </main>
    </>
  )
}

export default LeaguesPage
