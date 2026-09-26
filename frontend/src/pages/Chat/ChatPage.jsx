import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import ChatLog from '../../components/ChatLog/ChatLog'
import ChatInput from '../../components/ChatInput/ChatInput'
import FplHeader from '../../components/FplHeader/FplHeader'
import { sendChatMessage } from '../../api/chat'
import { fetchUserContests } from '../../api/dream11'
import { MODE_CONTESTS, MODE_FPL, useAppMode } from '../../config/appMode'

const MODE_TACTICAL = 'tactical'
const MODE_DREAM11 = 'dream11'

// Keyed the same as the /chat request's mode field. Chat has no per-mode
// route (both Tactic Mode and Quick 11 Mode land on /chat), so unlike the
// rest of the app it can't rely on config/appMode's path-derived mode --
// this page tracks its own toggle instead, local to the conversation.
const WELCOME_MESSAGES = {
  [MODE_TACTICAL]: "Hi, I'm PitchSide AI. Ask me about your starting XI, Bonus Players, transfers, or anything else about your squad.",
  [MODE_DREAM11]: "Hi, I'm PitchSide AI. Ask me about your Quick 11 mode team for this match.",
}

function welcomeMessage(mode) {
  return { role: 'assistant', text: WELCOME_MESSAGES[mode] }
}

function ChatPage() {
  const { settings } = useOutletContext()
  const { mode: appMode, setMode: setAppMode } = useAppMode()
  const initialMode = appMode === MODE_CONTESTS ? MODE_DREAM11 : MODE_TACTICAL
  const [mode, setChatMode] = useState(initialMode)
  const [messages, setMessages] = useState(() => [welcomeMessage(initialMode)])
  const [isTyping, setIsTyping] = useState(false)

  const [contests, setContests] = useState(null) // null = not fetched yet
  const [contestId, setContestId] = useState(null)
  const [contestsError, setContestsError] = useState(false)

  // Re-seed the log with the mode-appropriate greeting every time the
  // toggle changes -- the two modes' squads are unrelated (see the /chat
  // system prompt), so carrying tactical-mode conversation into a Dream11
  // context (or vice versa) would be confusing rather than helpful.
  function selectMode(next) {
    if (next === mode) return
    setChatMode(next)
    setAppMode(next === MODE_DREAM11 ? MODE_CONTESTS : MODE_FPL)
    setMessages([welcomeMessage(next)])
    setContestId(null)
  }

  useEffect(() => {
    if (mode !== MODE_DREAM11 || contests !== null) return
    let cancelled = false
    fetchUserContests({ user_id: settings.user_id })
      .then((data) => {
        if (cancelled) return
        const list = data.contests ?? data
        setContests(list)
        if (list.length === 1) setContestId(list[0].contest_id)
      })
      .catch(() => {
        if (!cancelled) setContestsError(true)
      })
    return () => {
      cancelled = true
    }
  }, [mode, contests, settings.user_id])

  async function handleSend(text) {
    setMessages((prev) => [...prev, { role: 'user', text }])
    setIsTyping(true)
    try {
      const { response } = await sendChatMessage({
        ...settings,
        message: text,
        mode,
        contest_id: mode === MODE_DREAM11 ? contestId : undefined,
      })
      setMessages((prev) => [...prev, { role: 'assistant', text: response }])
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: 'Something went wrong reaching PitchSide AI -- please try again.' },
      ])
    } finally {
      setIsTyping(false)
    }
  }

  // Dream11 mode needs a contest picked before there's a squad to talk
  // about at all: this user could have zero, one, or several Quick 11
  // teams, and /chat requires exactly one contest_id.
  const needsContestPick = mode === MODE_DREAM11 && contestId === null
  const inputDisabled = isTyping || (mode === MODE_DREAM11 && (contestsError || contests?.length === 0 || needsContestPick))

  return (
    <div className="flex flex-col h-full">
      <FplHeader
        helpTo={appMode === MODE_CONTESTS ? '/dream11/scoring' : '/scoring'}
        showHelp
        title="Assist"
      />
      <ChatLog isTyping={isTyping} messages={messages} />

      {/* Composer block: text bar first, then the mode toggle, then (in
          Quick 11 mode) the contest/match picker below that -- all one
          fixed group anchored above BottomNav, with generous spacing
          between each row. */}
      <div className="fixed bottom-[88px] left-1/2 -translate-x-1/2 w-full max-w-[600px] z-40 bg-surface/90 backdrop-blur-md border-t border-surface-variant shadow-[0_-8px_16px_-4px_rgba(0,0,0,0.05)] px-md pt-4 pb-4 flex flex-col gap-md">
        <ChatInput disabled={inputDisabled} onSend={handleSend} />

        <div className="flex gap-sm" role="tablist" aria-label="Chat mode">
          {[
            { mode: MODE_TACTICAL, label: 'Tactic Mode' },
            { mode: MODE_DREAM11, label: 'Quick 11 Mode' },
          ].map((tab) => (
            <button
              aria-pressed={mode === tab.mode}
              className={`flex-1 py-3 rounded-full font-label-md text-label-md font-bold transition-colors ${
                mode === tab.mode
                  ? 'bg-primary-container text-on-primary'
                  : 'bg-surface-container-lowest text-on-surface-variant border border-outline-variant'
              }`}
              data-testid={`chat-mode-${tab.mode}`}
              key={tab.mode}
              onClick={() => selectMode(tab.mode)}
              type="button"
            >
              {tab.label}
            </button>
          ))}
        </div>


        {mode === MODE_DREAM11 && contestsError && (
          <p className="font-body-md text-body-md text-error">
            Couldn't load your Quick 11 contests -- please try again.
          </p>
        )}

        {mode === MODE_DREAM11 && contests?.length === 0 && (
          <p className="font-body-md text-body-md text-on-surface-variant">
            You haven't joined a Quick 11 contest yet -- join one to get squad advice here.
          </p>
        )}

        {needsContestPick && contests?.length > 1 && (
          <div className="flex flex-col gap-sm">
            <span className="font-body-md text-body-md text-on-surface-variant">Which match?</span>
            {contests.map((c) => (
              <button
                className="text-left px-3 py-3 rounded-lg bg-surface-container-lowest border border-outline-variant hover:bg-surface-container-low transition-colors"
                key={c.contest_id}
                onClick={() => setContestId(c.contest_id)}
                type="button"
              >
                <span className="block font-body-md text-body-md font-bold text-on-surface">{c.name}</span>
                <span className="block font-label-md text-label-md text-on-surface-variant">
                  {c.home_team} vs {c.away_team}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default ChatPage
