# PitchSide / Tactical Fantasy Football — Master Asset & Design Specification

**Design System**: Tactical Minimalist Editorial  
**Typography**: Helvetica (`'Helvetica Neue', Helvetica, Arial, sans-serif`), Inter fallback  
**Platform**: Responsive Mobile App (iOS / Web)  

---

## 1. Brand & Key Art Inventory

| Asset Name | Type | Key Role & Description | Asset Reference |
| :--- | :--- | :--- | :--- |
| **Tactic Mode Vector Logo** | Vector Brand Emblem | Primary brand mark featuring tactical sliders, pitch coordinate lines, and viewfinder ticks. Built for app headers, splash screens, and navigation drawers. | `{{DATA:IMAGE:IMAGE_6}}` |
| **Tactic Mode 3D Isometric Art** | 3D Analytics Artwork (1024×1024) | Elevated isometric perspective of a miniature tactical football pitch with holographic lines, pass vectors, and metric rings. Perfect for hero banners, onboarding, and mode intros. | `{{DATA:IMAGE:IMAGE_5}}` |
| **PitchSide Login Illustration** | Editorial Vector Art (2000×2000) | Two-tone minimalist illustration of youth footballers on the training ground. Clean hero anchor for login and authentication flows. | `{{DATA:IMAGE:IMAGE_17}}` |

---

## 2. Design Tokens & Color Palette

### Primary & Accent Colors
* **Tactical Olive (Brand Primary)**: `#8DAA3C` — Core brand identity, active state highlights, and primary CTA capsules.
* **Tactical Olive Muted**: `#A4C447` — Secondary badges, hover states, and subtle focus indicators.
* **Pitch Grass Green**: `#086834` (gradient to `#0F7B42`) — Tactical pitch field surface with crisp white pitch line grid markings.
* **Electric Status Green**: `#00F47B` — Live match active indicators, free transfer badges, and Captain/Vice-Captain bonus banners.

### Neutral & Surface System
* **Background Surface**: `#FBF9F5` — Warm editorial neutral canvas providing an elevated, non-sterile background.
* **Card Container (Surface-Low)**: `#FFFFFF` with `#E5E6E1` hairline border (1px) and soft elevation shadows (`shadow-sm` / `shadow-[0_2px_8px_rgba(0,0,0,0.04)]`).
* **Muted Fill / Tonal**: `#F5F3F0` — Pill button backgrounds, inactive segmented controls, and search inputs.
* **Midnight Black / Deep Slate**: `#1B1C1A` — High-contrast text labels, primary stat headers, and dark action triggers.
* **Editorial Plum / Dark Wine**: `#22092C` — Accent button states, legacy contest CTA cards, and active navigation highlights.
* **Text Secondary**: `#6D716A` / `#7A7C77` — Metadata subtitles, gameweek labels, and helper copy.

---

## 3. Typography & Spacing Standard

* **Font Family**: Helvetica, `'Helvetica Neue'`, Arial, sans-serif
* **Header Hierarchy**:
  * Page Title: `17px` / `18px`, Weight: `700` (Bold), tracking `-0.02em`
  * Stat Callouts: `28px` / `32px`, Weight: `800` (ExtraBold), tabular numbers
  * Card Section Titles: `13px` / `14px`, Weight: `700`, uppercase with `letter-spacing: 0.06em`
  * Body & Metadata: `12px` / `13px`, Weight: `500` / `600`, color `#6d716a`
* **Card Border Radius**:
  * Outer Surface Cards: `rounded-2xl` (`16px`) to `rounded-3xl` (`24px`)
  * Buttons & Input Pills: `rounded-full` (`9999px`)
  * Bottom Slider Sheet: `rounded-t-[28px]`

---

## 4. Iconography & Telemetry Token Library

Captured on the dedicated **Dashboard Icon System** (`{{DATA:SCREEN:SCREEN_38}}`):

1. **Global Navigation Dock**:
   * `Home / Pitch`: Stylized tactical stadium icon
   * `Starting XI`: Pitch formation & player jersey badge
   * `Transfers`: Horizontal opposing directional arrows (`⇄`)
   * `Leagues`: Championship trophy icon
   * `Chats / Assist`: Editorial chat bubble with embedded AI spark
2. **Tactical & Role Badges**:
   * `Captain (C)`: Circular badge with high-contrast bold glyph
   * `Vice-Captain (VC)`: Slate capsule badge with 1.5× bonus multiplier
   * `Bonus Points (BPS)`: 8-point geometric star icon (`★`)
   * `Tactical Sub Swaps`: Directional badge indicating scheduled in/out substitutions
3. **Controls & Actions**:
   * `Hamburger Menu`: Minimalist dual-line / triple-line pill button (36×36px)
   * `Manager Profile`: Circular disc button with user outline glyph (36×36px)
   * `Copy / Share`: Invite code icon with clipboard feedback

---

## 5. Master Screen Catalog & Flow Architecture

1. **FPL Dashboard** (`{{DATA:SCREEN:SCREEN_40}}`)
   * Top navigation bar with Hamburger trigger & Manager Profile
   * Gameweek deadline counter with urgent timer badge
   * Performance telemetry: Gameweek Points, Season Total, Overall Rank
   * Team value & Bank balance metrics
   * Interactive tactical formation pitch (4-4-2 / 3-5-2)
   * Bench player strip and Tactical Swap tracker
   * Slide-out side drawer with Game Mode selection (`Tactic Mode` vs `Quick 11 Mode`)
   * Floating frosted glass dock navigation bar

2. **Starting XI** (`{{DATA:SCREEN:SCREEN_35}}`)
   * Standardized left-aligned header with hamburger trigger
   * Tactic Mode formation switcher (Attack / Defense / Balanced)
   * Live interactive pitch with player nodes, captaincy toggles, and price tiers
   * Tactical Sub control panel with auto-sub rules and swap validation
   * Bench squad list with individual role reordering

3. **Leaderboard with Interactive Player View Card** (`{{DATA:SCREEN:SCREEN_2}}`)
   * Contest status header (`Warrior`, 1/2 members, invite code)
   * Rival squad leaderboard ranking
   * Interactive pitch: tapping any player opens the animated bottom slider card
   * Player View Card Sheet:
     * Player header with position, jersey, and VC badge
     * Telemetry rows (Goals, Assists, Clean Sheets, Goals Conceded, Minutes Played)
     * Tactical accent green bonus bar (`Vice-captain bonus [+50% extra]`)
     * Smooth drag-to-dismiss handle and backdrop overlay

4. **Transfers** (`{{DATA:SCREEN:SCREEN_30}}`)
   * Gameweek transfer allowance & bank budget telemetry
   * Pitch overview highlighting flagged/injured squad members
   * Filterable transfer replacement marketplace (sorted by price, fixture difficulty, team)
   * Confirmation bar with hit penalty calculation

5. **Matches** (`{{DATA:SCREEN:SCREEN_20}}`)
   * Gameweek fixture schedule with club crests and kickoff countdowns
   * Head-to-head matchup cards with contest indicators
   * Completed match results with historical fantasy point tallies

6. **Leagues Hub & League Detail** (`{{DATA:SCREEN:SCREEN_26}}`, `{{DATA:SCREEN:SCREEN_10}}`)
   * Join League code input module with instant preview
   * Create Contest form (match selector, member limits, saved team attachment)
   * Active league tables and standing leaderboards

7. **PitchSide AI Assist / Chats** (`{{DATA:SCREEN:SCREEN_23}}`)
   * AI tactical manager conversational feed with Gameweek recommendations
   * Squad audit breakdown highlighting weak links and rotational opportunities
   * Quick action pills to switch game modes directly from chat

8. **Edit Team** (`{{DATA:SCREEN:SCREEN_13}}`)
   * Team name customization with AI generator trigger
   * Live squad constraint trackers (11/11 players, 100.0M budget cap)
   * Pitch formation view with captaincy popover menu
   * Filterable player picker by position (GK, DEF, MID, FWD)

9. **PitchSide Login** (`{{DATA:SCREEN:SCREEN_15}}`)
   * Clean brand authentication screen featuring `Junior soccer-bro.png` vector art
   * Floating input fields with hairline borders and toggle visibility
   * Consistent Tactical Minimalist styling and typography
