# PitchSide / Tactical Fantasy Football — Downloadable Asset Package & Code Export

**Design System**: Tactical Minimalist Editorial  
**Typography**: Helvetica (`'Helvetica Neue', Helvetica, Arial, sans-serif`), Inter fallback  
**Platform**: iOS & Web Mobile (390px viewport base)  
**Primary Brand Accent**: `#8DAA3C` (Tactical Olive)  

---

## 1. Brand & Artwork Asset References

All key brand illustrations and renders in the workspace:

| Asset Name | Type / Format | Resolution | Description & Use | Workspace Asset ID |
| :--- | :--- | :--- | :--- | :--- |
| **Tactic Mode Vector Logo** | Vector Emblem / PNG | Scalable | Primary brand mark with tactical sliders, viewfinder ticks, and coordinate grid. Used in splash screens, app headers, and drawer. | `{{DATA:IMAGE:IMAGE_7}}` |
| **Tactic Mode 3D Isometric Key Art** | 3D Render (PNG) | 1024×1024 | Isometric tactical pitch with holographic lines, pass vectors, and metric rings. Perfect for mode selector and hero card. | `{{DATA:IMAGE:IMAGE_6}}` |
| **PitchSide Login Illustration** | Vector Illustration (PNG) | 2000×2000 | Clean two-tone editorial youth soccer players on training ground. Hero art for authentication. | `{{DATA:IMAGE:IMAGE_18}}` |
| **Tactic Mode View Card Reference** | Mobile Spec (PNG) | 387×851 | Reference visual for player telemetry slider card. | `{{DATA:IMAGE:IMAGE_5}}` |

---

## 2. Copyable Production Vector SVGs (24×24px • 1.75px Stroke)

Use these clean, inline-ready SVGs directly in React, React Native, iOS SF Symbols mapping, or Flutter:

### 2.1 Navigation & Frame Controls

#### Hamburger / Tactical Drawer Trigger
```svg
<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#1B1C1A" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <line x1="4" y1="7" x2="20" y2="7"/>
  <line x1="4" y1="12" x2="20" y2="12"/>
  <line x1="4" y1="17" x2="14" y2="17"/>
</svg>
```

#### Manager Profile Disc Glyph
```svg
<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#1B1C1A" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/>
  <circle cx="12" cy="7" r="4"/>
</svg>
```

#### Gameweek Stepper / Chevrons
```svg
<!-- Chevron Left -->
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#1B1C1A" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="15 18 9 12 15 6"/>
</svg>

<!-- Chevron Right -->
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#1B1C1A" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="9 18 15 12 9 6"/>
</svg>
```

#### Transfer Market Lock / Deadline Clock
```svg
<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#8DAA3C" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="10"/>
  <polyline points="12 6 12 12 16 14"/>
</svg>
```

---

### 2.2 Bottom Navigation Dock Tokens

#### 1. Home / Pitch
```svg
<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
  <polyline points="9 22 9 12 15 12 15 22"/>
</svg>
```

#### 2. Starting XI / Squad
```svg
<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>
  <circle cx="9" cy="7" r="4"/>
  <path d="M22 21v-2a4 4 0 0 0-3-3.87"/>
  <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
</svg>
```

#### 3. Transfers / Trade Desk
```svg
<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="m8 3 4 8 5-5 5 15H2L8 3z"/>
  <path d="M4 17h16"/>
  <path d="m16 21 4-4-4-4"/>
  <path d="m8 7-4 4 4 4"/>
</svg>
```

#### 4. Leagues / Trophy
```svg
<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/>
  <path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/>
  <path d="M4 22h16"/>
  <path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/>
  <path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/>
  <path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>
</svg>
```

#### 5. Chats / AI Assist
```svg
<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
  <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
</svg>
```

---

### 2.3 Pitch Roles, Badges & Tactical Elements

#### Star / Bonus Points System (BPS 8-Point Token)
```svg
<svg width="18" height="18" viewBox="0 0 24 24" fill="#8DAA3C" stroke="#8DAA3C" stroke-width="1">
  <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
</svg>
```

#### Tactical Substitution Swap Arrows
```svg
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#00F47B" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M7 10v12"/>
  <path d="m11 18-4 4-4-4"/>
  <path d="M17 14V2"/>
  <path d="m13 6 4-4 4 4"/>
</svg>
```

#### Tactical Captain Badge (C)
```svg
<svg width="22" height="22" viewBox="0 0 24 24" fill="#1B1C1A">
  <circle cx="12" cy="12" r="11" fill="#1B1C1A"/>
  <text x="12" y="16" fill="#FFFFFF" font-family="'Helvetica Neue', Helvetica, Arial, sans-serif" font-size="12" font-weight="900" text-anchor="middle">C</text>
</svg>
```

#### Tactical Vice-Captain Badge (VC)
```svg
<svg width="26" height="20" viewBox="0 0 32 24" fill="none">
  <rect width="32" height="24" rx="6" fill="#1B1C1A"/>
  <text x="16" y="16.5" fill="#00F47B" font-family="'Helvetica Neue', Helvetica, Arial, sans-serif" font-size="11" font-weight="800" text-anchor="middle" letter-spacing="0.5">VC</text>
</svg>
```

#### Mini Football Squad Jersey (Modular SVG)
```svg
<!-- Arsenal Home Red -->
<svg width="32" height="32" viewBox="0 0 32 32" fill="none">
  <path d="M9 7L3 13L7 16L9 14V27H23V14L25 16L29 13L23 7C21 9 11 9 9 7Z" fill="#DC2626" stroke="#991B1B" stroke-width="1.5" stroke-linejoin="round"/>
  <path d="M9 7C11 9 21 9 23 7V10C21 11.5 11 11.5 9 10V7Z" fill="#FFFFFF"/>
</svg>

<!-- Away White / Leeds -->
<svg width="32" height="32" viewBox="0 0 32 32" fill="none">
  <path d="M9 7L3 13L7 16L9 14V27H23V14L25 16L29 13L23 7C21 9 11 9 9 7Z" fill="#F8FAFC" stroke="#CBD5E1" stroke-width="1.5" stroke-linejoin="round"/>
  <path d="M9 7C11 9 21 9 23 7V10C21 11.5 11 11.5 9 10V7Z" fill="#1E3A8A"/>
</svg>

<!-- Olive Brand Kit -->
<svg width="32" height="32" viewBox="0 0 32 32" fill="none">
  <path d="M9 7L3 13L7 16L9 14V27H23V14L25 16L29 13L23 7C21 9 11 9 9 7Z" fill="#8DAA3C" stroke="#688224" stroke-width="1.5" stroke-linejoin="round"/>
  <path d="M9 7C11 9 21 9 23 7V10C21 11.5 11 11.5 9 10V7Z" fill="#1B1C1A"/>
</svg>
```

---

## 3. Design System Tokens (JSON Export)

```json
{
  "themeName": "Tactical Minimalist Editorial",
  "typography": {
    "fontFamily": "Helvetica, 'Helvetica Neue', Arial, sans-serif",
    "fontFamilyMono": "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
    "scale": {
      "display": { "fontSize": "32px", "lineHeight": "36px", "fontWeight": "800", "letterSpacing": "-0.03em" },
      "title": { "fontSize": "17px", "lineHeight": "22px", "fontWeight": "700", "letterSpacing": "-0.02em" },
      "sectionHeader": { "fontSize": "13px", "lineHeight": "18px", "fontWeight": "700", "letterSpacing": "0.06em", "textTransform": "uppercase" },
      "body": { "fontSize": "14px", "lineHeight": "20px", "fontWeight": "500" },
      "caption": { "fontSize": "12px", "lineHeight": "16px", "fontWeight": "600", "color": "#6D716A" }
    }
  },
  "colors": {
    "brandPrimary": "#8DAA3C",
    "brandPrimaryMuted": "#A4C447",
    "brandPrimaryDark": "#688224",
    "pitchFieldDark": "#086834",
    "pitchFieldLight": "#0F7B42",
    "electricAccent": "#00F47B",
    "surface": "#FBF9F5",
    "surfaceCard": "#FFFFFF",
    "surfaceMuted": "#F5F3F0",
    "borderHairline": "#E5E6E1",
    "textPrimary": "#1B1C1A",
    "textSecondary": "#6D716A",
    "midnightAccent": "#22092C"
  },
  "radii": {
    "card": "20px",
    "button": "9999px",
    "sheet": "28px 28px 0 0",
    "badge": "6px"
  },
  "shadows": {
    "card": "0 2px 8px rgba(0, 0, 0, 0.04)",
    "floatingDock": "0 8px 30px rgba(0, 0, 0, 0.08)",
    "activePill": "0 2px 6px rgba(141, 170, 60, 0.25)"
  }
}
```

---

## 4. Tailwind CSS Configuration Snippet

Add this to `tailwind.config.js` to replicate the PitchSide theme in any web or hybrid project:

```javascript
module.exports = {
  theme: {
    extend: {
      colors: {
        tactical: {
          olive: '#8DAA3C',
          'olive-muted': '#A4C447',
          'olive-dark': '#688224',
          pitch: '#086834',
          'pitch-light': '#0F7B42',
          electric: '#00F47B',
          midnight: '#1B1C1A',
          plum: '#22092C',
        },
        surface: {
          canvas: '#FBF9F5',
          card: '#FFFFFF',
          tonal: '#F5F3F0',
          border: '#E5E6E1',
        },
        ink: {
          primary: '#1B1C1A',
          secondary: '#6D716A',
          muted: '#9E9E9B',
        }
      },
      fontFamily: {
        sans: ['Helvetica', '"Helvetica Neue"', 'Arial', 'sans-serif'],
      },
      borderRadius: {
        '2.5xl': '20px',
        'sheet': '28px',
      },
      boxShadow: {
        'editorial': '0 2px 8px rgba(0, 0, 0, 0.04)',
        'dock': '0 8px 32px rgba(0, 0, 0, 0.08)',
      }
    },
  },
};
```

---

## 5. Summary of Screens Available for Export

All 9 mobile screens on canvas are structured in pure semantic HTML5 & Tailwind CSS:
1. **FPL Dashboard** (`{{DATA:SCREEN:SCREEN_41}}`) — Master overview, metric cards, menu drawer, tactical pitch.
2. **Starting XI** (`{{DATA:SCREEN:SCREEN_36}}`) — Tactic mode formation toggles, auto/tactical subs, bench strip.
3. **Leaderboard & Player View Card** (`{{DATA:SCREEN:SCREEN_3}}`) — Live sliding sheet with telemetry breakdown.
4. **Transfers** (`{{DATA:SCREEN:SCREEN_31}}`) — Trade allowance, marketplace filters, replacement cards.
5. **Matches** (`{{DATA:SCREEN:SCREEN_21}}`) — Fixtures schedule, kickoff timers, head-to-head status.
6. **Leagues Hub** (`{{DATA:SCREEN:SCREEN_27}}`) — League join codes and contest creator.
7. **PitchSide AI Assist** (`{{DATA:SCREEN:SCREEN_24}}`) — Manager audit feed and recommendations.
8. **Edit Team** (`{{DATA:SCREEN:SCREEN_14}}`) — Squad picker, credit tracker, and captaincy overlay.
9. **Login** (`{{DATA:SCREEN:SCREEN_16}}`) — Minimalist authentication with soccer-bro hero graphic.
