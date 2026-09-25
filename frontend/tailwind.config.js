/**
 * Canonical PitchSide AI design tokens.
 *
 * Extracted from the Stitch export. Every screen embeds its own copy of this
 * config in a <script id="tailwind-config"> block; all nine exported screens
 * were compared and their colour palette, spacing scale, radius scale and type
 * scale are identical (only key ordering differs), so this file is the single
 * source of truth rather than any one screen's copy.
 *
 * preflight stays ON. Tailwind's `border` utility only emits border-width --
 * `border-style: solid` comes from preflight -- and the Stitch Register screen
 * shipped a bug where borders vanished for exactly that class of reason. The
 * pre-existing hand-written CSS pages (SquadSelectionPage, StartingXIPage,
 * TransfersPage...) are verified against preflight after this landed.
 */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        primary: '#4f6600',
        'on-primary': '#ffffff',
        'primary-container': '#8daa3c',
        'on-primary-container': '#2d3b00',
        'primary-fixed': '#cfef78',
        'primary-fixed-dim': '#b3d25f',
        'on-primary-fixed': '#161f00',
        'on-primary-fixed-variant': '#3b4d00',
        secondary: '#196c3b',
        'on-secondary': '#ffffff',
        'secondary-container': '#a3f5b6',
        'on-secondary-container': '#217240',
        'secondary-fixed': '#a3f5b6',
        'secondary-fixed-dim': '#88d89c',
        'on-secondary-fixed': '#00210d',
        'on-secondary-fixed-variant': '#005228',
        tertiary: '#506612',
        'on-tertiary': '#ffffff',
        'tertiary-container': '#90a94f',
        'on-tertiary-container': '#2c3c00',
        'tertiary-fixed': '#d2ed8a',
        'tertiary-fixed-dim': '#b6d171',
        'on-tertiary-fixed': '#151f00',
        'on-tertiary-fixed-variant': '#3a4d00',
        error: '#ba1a1a',
        'on-error': '#ffffff',
        'error-container': '#ffdad6',
        'on-error-container': '#93000a',
        background: '#fbf9f5',
        'on-background': '#1b1c1a',
        surface: '#fbf9f5',
        'on-surface': '#1b1c1a',
        'surface-variant': '#e4e2df',
        'on-surface-variant': '#454839',
        'surface-bright': '#fbf9f5',
        'surface-dim': '#dbdad6',
        'surface-container-lowest': '#ffffff',
        'surface-container-low': '#f5f3f0',
        'surface-container': '#efeeea',
        'surface-container-high': '#e9e8e4',
        'surface-container-highest': '#e4e2df',
        'surface-tint': '#4f6600',
        'inverse-surface': '#30312e',
        'inverse-on-surface': '#f2f1ed',
        'inverse-primary': '#b3d25f',
        outline: '#757967',
        'outline-variant': '#c5c8b4',
      },
      borderRadius: {
        DEFAULT: '0.25rem',
        lg: '0.5rem',
        xl: '0.75rem',
        full: '9999px',
      },
      spacing: {
        base: '4px',
        xs: '4px',
        sm: '8px',
        gutter: '12px',
        md: '16px',
        lg: '24px',
        xl: '32px',
        'safe-margin': '16px',
      },
      fontFamily: {
        'display-lg': ['Helvetica Neue', 'Helvetica', 'Arial', 'sans-serif'],
        'headline-md': ['Helvetica Neue', 'Helvetica', 'Arial', 'sans-serif'],
        'headline-sm': ['Helvetica Neue', 'Helvetica', 'Arial', 'sans-serif'],
        'body-lg': ['Helvetica Neue', 'Helvetica', 'Arial', 'sans-serif'],
        'body-md': ['Helvetica Neue', 'Helvetica', 'Arial', 'sans-serif'],
        'label-md': ['Helvetica Neue', 'Helvetica', 'Arial', 'sans-serif'],
        'stats-number': ['Helvetica Neue', 'Helvetica', 'Arial', 'sans-serif'],
      },
      fontSize: {
        'display-lg': ['32px', { lineHeight: '1.2', letterSpacing: '-0.02em', fontWeight: '800' }],
        'headline-md': ['24px', { lineHeight: '1.3', letterSpacing: '-0.01em', fontWeight: '700' }],
        'headline-sm': ['20px', { lineHeight: '1.3', fontWeight: '700' }],
        'body-lg': ['16px', { lineHeight: '1.6', fontWeight: '400' }],
        'body-md': ['14px', { lineHeight: '1.5', fontWeight: '400' }],
        'label-md': ['12px', { lineHeight: '1', letterSpacing: '0.05em', fontWeight: '600' }],
        'stats-number': ['18px', { lineHeight: '1', fontWeight: '800' }],
      },
    },
  },
  plugins: [require('@tailwindcss/forms')],
}
