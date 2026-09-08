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
        primary: '#040004',
        'on-primary': '#ffffff',
        'primary-container': '#38003c',
        'on-primary-container': '#af6eac',
        'primary-fixed': '#ffd6f8',
        'primary-fixed-dim': '#f7aff2',
        'on-primary-fixed': '#37003b',
        'on-primary-fixed-variant': '#6a306a',
        secondary: '#006d35',
        'on-secondary': '#ffffff',
        'secondary-container': '#00fa82',
        'on-secondary-container': '#006e36',
        'secondary-fixed': '#61ff97',
        'secondary-fixed-dim': '#00e476',
        'on-secondary-fixed': '#00210c',
        'on-secondary-fixed-variant': '#005227',
        tertiary: '#050000',
        'on-tertiary': '#ffffff',
        'tertiary-container': '#410011',
        'on-tertiary-container': '#fe225f',
        'tertiary-fixed': '#ffd9dc',
        'tertiary-fixed-dim': '#ffb2ba',
        'on-tertiary-fixed': '#400010',
        'on-tertiary-fixed-variant': '#910030',
        error: '#ba1a1a',
        'on-error': '#ffffff',
        'error-container': '#ffdad6',
        'on-error-container': '#93000a',
        background: '#faf8ff',
        'on-background': '#131b2e',
        surface: '#faf8ff',
        'on-surface': '#131b2e',
        'surface-variant': '#dae2fd',
        'on-surface-variant': '#4f434c',
        'surface-bright': '#faf8ff',
        'surface-dim': '#d2d9f4',
        'surface-container-lowest': '#ffffff',
        'surface-container-low': '#f2f3ff',
        'surface-container': '#eaedff',
        'surface-container-high': '#e2e7ff',
        'surface-container-highest': '#dae2fd',
        'surface-tint': '#854884',
        'inverse-surface': '#283044',
        'inverse-on-surface': '#eef0ff',
        'inverse-primary': '#f7aff2',
        outline: '#80737d',
        'outline-variant': '#d2c2cd',
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
        'display-lg': ['Inter', 'system-ui', 'sans-serif'],
        'headline-md': ['Inter', 'system-ui', 'sans-serif'],
        'headline-sm': ['Inter', 'system-ui', 'sans-serif'],
        'body-lg': ['Inter', 'system-ui', 'sans-serif'],
        'body-md': ['Inter', 'system-ui', 'sans-serif'],
        'label-md': ['Inter', 'system-ui', 'sans-serif'],
        'stats-number': ['Inter', 'system-ui', 'sans-serif'],
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
