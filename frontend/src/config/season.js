/**
 * One place for the season/gameweek every page reads.
 *
 * These were previously hardcoded per page and had already drifted -- ChatPage
 * defaulted to gameweek 4 while DashboardPage defaulted to 1, so the same user
 * saw two different gameweeks depending on which tab they were on.
 *
 * The backend has no "current gameweek" endpoint yet; when one exists this
 * module is the only thing that needs to change.
 */
export const CURRENT_SEASON = '2026-27'
export const CURRENT_GAMEWEEK = 1
