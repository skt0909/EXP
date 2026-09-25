# Design Notes

## Stitch Source

- Project: Website Header Design Variations
- Project ID: 5235814110832489212
- Screen: Dashboard Icon System
- Screen ID: 2ab6e28aad83453bbd8d896af92d10be
- Screen: PitchSide - Downloadable Asset Bundle & SVG Export
- Screen ID: cfbb654161ef483aa2fa314872370e34
- Screen: PitchSide - Master Asset & Design Specification
- Screen ID: a929fa2651a84013a3fee7d884c5254c
- Screen: Login - Tactical Minimalist Editorial
- Screen ID: 86553d288a66498da21171a2fe71f574
- Screen: FPL Dashboard - Tennis Reference Visual Style
- Screen ID: 99a96a60ee8946ad850fcc661dec5d53
- Screen: Starting XI - Tactical Minimalist Editorial
- Screen ID: 95ab758564644a74a32d493f4828a75e
- Screen: Transfers - Tactical Minimalist Editorial
- Screen ID: 59af78e314944f6181afcd575f1b6624
- Screen: Leagues Hub - Tactical Minimalist Editorial
- Screen ID: da053823546449bdac29d495de7a47aa
- Screen: Chats / Assist - Tactical Minimalist Editorial
- Screen ID: f8f60f3baaec42e2a39ee9fc2625273c
- Screen: Matches - Tactical Minimalist Editorial
- Screen ID: 40c05fcb0aac46fc92a44652223deb3d
- Screen: Match Detail - Tactical Minimalist Editorial
- Screen ID: d5026e58ddb54db1940de27ce13d5d1d
- Screen: Edit Team - Tactical Minimalist Editorial
- Screen ID: ca868aee0315497c8784230f8a2c0314
- Screen: Leagues - Tactical Minimalist Editorial (Quick 11)
- Screen ID: d2a16eb4406042e9875f12c664f718aa
- Screen: Leaderboard with Player View Card
- Screen ID: 84d2151bd3e5470fbfa27f1018a054d9

## Icon Direction

Use the icons from the shared Stitch screen, "Dashboard Icon System", as the source of truth for all dashboard and header iconography. Reuse the icon choices shown in that screen and match their visual language, optical size, weight, fill treatment, spacing, touch targets, and active, inactive, hover, and disabled states.

Do not substitute a different icon merely because it is the default in an icon library. Prefer existing app icon components only when they faithfully match the Stitch reference; otherwise, recreate or adapt the icon so the implemented UI follows the shared icon system.

The only exception is the hamburger menu icon: each screen may use the hamburger icon shown in its own Stitch reference. Do not reuse any other screen-specific icons. All remaining icons must come from, or faithfully match, the Dashboard Icon System.

Keep the existing Premier League team logos and existing jersey artwork on every pitch. The Stitch screens define composition and interaction, but must not replace those live application assets.

Player jerseys on the Quick 11 leaderboard pitch and Tactic dashboard pitch are interactive. Tapping a jersey opens a bottom player card above the navigation dock with mode-appropriate scoring or performance details; dismissing it returns to the same pitch state.

Use the downloaded HTML as the implementation reference and the screenshot as the visual reference:

- Visual reference: `docs/stitch_mockups/dashboard-icon-system/dashboard-icon-system.jpg`
- Generated code: `docs/stitch_mockups/dashboard-icon-system/dashboard-icon-system.html`

## PitchSide Assets and Specification

Use the PitchSide asset bundle for production icon and vector implementation. Reuse its inline SVG definitions for navigation controls, bottom navigation, pitch roles, captain badges, squad jerseys, and tactical actions instead of approximating them with unrelated library icons. Preserve the specified 24x24 view boxes, 1.75px stroke weight, line caps, joins, and `currentColor` behavior unless a component explicitly requires another state.

Use the master asset and design specification as the implementation reference for brand artwork, color tokens, typography, spacing, iconography, telemetry tokens, and screen-flow conventions. When it conflicts with generic library defaults, follow the PitchSide specification while remaining consistent with established application behavior and accessibility requirements.

- SVG asset bundle and code export: `docs/stitch_mockups/pitchside-asset-bundle/pitchside-asset-bundle.md`
- Master asset and design specification: `docs/stitch_mockups/pitchside-master-specification/pitchside-master-design-specification.md`
- Stitch did not provide screenshot URLs for these two export/specification screens; their downloadable code artifacts are the canonical sources.

## Screen References

Use each screenshot as the visual reference and its paired HTML file as the structural/code reference:

- Login: `docs/stitch_mockups/login-tactical-minimalist/login-tactical-minimalist.jpg` and `docs/stitch_mockups/login-tactical-minimalist/login-tactical-minimalist.html`
- FPL Dashboard: `docs/stitch_mockups/fpl-dashboard-tennis-style/fpl-dashboard-tennis-style.jpg` and `docs/stitch_mockups/fpl-dashboard-tennis-style/fpl-dashboard-tennis-style.html`
- Starting XI: `docs/stitch_mockups/starting-xi-tactical-minimalist/starting-xi-tactical-minimalist.jpg` and `docs/stitch_mockups/starting-xi-tactical-minimalist/starting-xi-tactical-minimalist.html`
- Transfers: `docs/stitch_mockups/transfers-tactical-minimalist/transfers-tactical-minimalist.jpg` and `docs/stitch_mockups/transfers-tactical-minimalist/transfers-tactical-minimalist.html`
- Leagues Hub: `docs/stitch_mockups/leagues-hub-tactical-minimalist/leagues-hub-tactical-minimalist.jpg` and `docs/stitch_mockups/leagues-hub-tactical-minimalist/leagues-hub-tactical-minimalist.html`
- Chats / Assist: `docs/stitch_mockups/chats-assist-tactical-minimalist/chats-assist-tactical-minimalist.jpg` and `docs/stitch_mockups/chats-assist-tactical-minimalist/chats-assist-tactical-minimalist.html`
- Matches: `docs/stitch_mockups/matches-tactical-minimalist/matches-tactical-minimalist.jpg` and `docs/stitch_mockups/matches-tactical-minimalist/matches-tactical-minimalist.html`
- Match Detail: `docs/stitch_mockups/match-detail-tactical-minimalist/match-detail-tactical-minimalist.jpg` and `docs/stitch_mockups/match-detail-tactical-minimalist/match-detail-tactical-minimalist.html`
- Edit Team: `docs/stitch_mockups/edit-team-tactical-minimalist/edit-team-tactical-minimalist.jpg` and `docs/stitch_mockups/edit-team-tactical-minimalist/edit-team-tactical-minimalist.html`
- Quick 11 Leagues: `docs/stitch_mockups/leagues-quick11-tactical-minimalist/leagues-quick11-tactical-minimalist.jpg` and `docs/stitch_mockups/leagues-quick11-tactical-minimalist/leagues-quick11-tactical-minimalist.html`
- Leaderboard player card: `docs/stitch_mockups/leaderboard-player-view-card/leaderboard-player-view-card.jpg` and `docs/stitch_mockups/leaderboard-player-view-card/leaderboard-player-view-card.html`

## Asset Status

The Dashboard Icon System, application and Quick 11 screen references, generated HTML, PitchSide SVG asset bundle, and master design specification were downloaded from the hosted URLs returned by Stitch and are stored under `docs/stitch_mockups/`.
