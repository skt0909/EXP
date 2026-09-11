import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'
import Layout from './layout/Layout'
import { AppModeProvider } from './config/appMode'
import { GameweekProvider } from './config/gameweek'
import { AuthProvider } from './auth/AuthContext'
import ProtectedRoute from './auth/ProtectedRoute'
import LoginPage from './pages/Login/LoginPage'
import RegisterPage from './pages/Register/RegisterPage'
import ChatPage from './pages/Chat/ChatPage'
import TransfersPage from './pages/Transfers/TransfersPage'
import StartingXIPage from './pages/StartingXI/StartingXIPage'
import DashboardPage from './pages/DashboardPage/DashboardPage'
import SquadSelectionPage from './pages/SquadSelectionPage/SquadSelectionPage'
import LeaguesPage from './pages/Leagues/LeaguesPage'
import MatchesPage from './pages/Matches/MatchesPage'
import MatchDetailPage from './pages/Matches/MatchDetailPage'
import Dream11ContestsPage from './pages/Dream11/Dream11ContestsPage'
import Dream11ContestPage from './pages/Dream11/Dream11ContestPage'
import Dream11ScoringPage from './pages/Dream11/Dream11ScoringPage'
import PickTeamPage from './pages/Dream11/PickTeamPage'
import ScoringPage from './pages/Scoring/ScoringPage'

/**
 * Carries game mode and the real current season/gameweek to every
 * authenticated page, Layout-wrapped or not (Dashboard and Squad Selection
 * draw their own chrome outside Layout, so this can't just live there).
 */
function ModeShell() {
  return (
    <AppModeProvider>
      <GameweekProvider>
        <Outlet />
      </GameweekProvider>
    </AppModeProvider>
  )
}

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* Public: the only two routes reachable without a token. */}
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />

          {/* Everything else sits behind ProtectedRoute, which redirects to
              /login when GET /auth/me doesn't confirm a session.
              AppModeProvider wraps the whole authenticated area rather than
              Layout, so the self-contained pages below get the mode too. */}
          <Route element={<ProtectedRoute />}>
            <Route element={<ModeShell />}>
              <Route element={<Layout />}>
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/transfers" element={<TransfersPage />} />
                <Route path="/squad" element={<StartingXIPage />} />
                <Route path="/leagues" element={<LeaguesPage />} />
                {/* The two scoring-rules screens are Layout children on purpose:
                    that is what gives each the right BottomNav tab set for free.
                    /scoring reads as FPL and /dream11/scoring as Contests under
                    modeForPath, so neither needed a nav or mode change. */}
                <Route path="/scoring" element={<ScoringPage />} />
              {/* Both sit under Layout so they inherit the settings outlet
                  context (season/gameweek, plus user_id for the Dream11
                  endpoints that still take one) the rest of the app reads. */}
                <Route path="/matches" element={<MatchesPage />} />
                <Route path="/matches/:fixtureId" element={<MatchDetailPage />} />
                <Route path="/dream11" element={<Dream11ContestsPage />} />
                <Route path="/dream11/scoring" element={<Dream11ScoringPage />} />
                <Route path="/dream11/contests/:contestId" element={<Dream11ContestPage />} />
                <Route path="/dream11/contests/:contestId/pick" element={<PickTeamPage />} />
              </Route>
              {/* Self-contained pages: these render their own header/nav rather
                  than Layout's, so nesting them under Layout would double it up. */}
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="/squad-selection" element={<SquadSelectionPage />} />
            </Route>
          </Route>

          {/* Dashboard is the hub, so it -- not chat -- is the landing route. */}
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}

export default App
