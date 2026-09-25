const paths = {
  arrowBack: <path d="m15 18-6-6 6-6" />,
  account: (
    <><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></>
  ),
  calendar: (
    <><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M16 3v4M8 3v4M3 10h18" /></>
  ),
  chart: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>,
  add: <path d="M12 5v14M5 12h14" />,
  balance: <><path d="M12 3v18M5 7h14" /><path d="m5 7-3 6h6L5 7Zm14 0-3 6h6l-3-6ZM8 21h8" /></>,
  bonusStar: <polygon fill="currentColor" points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" stroke="none" />,
  bench: <><path d="M4 12h16v6H4zM6 18v3M18 18v3M7 12V7M17 12V7" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  chevronLeft: <path d="m15 18-6-6 6-6" />,
  chevronRight: <path d="m9 18 6-6-6-6" />,
  chat: (
    <><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" /><path d="M8 9h8M8 13h5" /></>
  ),
  close: <path d="M18 6 6 18M6 6l12 12" />,
  expandDown: <path d="m6 9 6 6 6-6" />,
  expandUp: <path d="m18 15-6-6-6 6" />,
  home: (
    <><path d="m3 11 9-8 9 8" /><path d="M5 10v10h14V10M9 20v-6h6v6" /></>
  ),
  help: <><circle cx="12" cy="12" r="9" /><path d="M9.6 9a2.7 2.7 0 1 1 4.3 2.2c-1 .7-1.9 1.2-1.9 2.8M12 17h.01" /></>,
  leagues: (
    <><path d="M8 21h8M12 17v4M7 4h10v4a5 5 0 0 1-10 0z" /><path d="M7 6H4v2a4 4 0 0 0 4 4M17 6h3v2a4 4 0 0 1-4 4" /></>
  ),
  lock: (
    <><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /></>
  ),
  logout: <><path d="M10 17l5-5-5-5M15 12H3" /><path d="M14 3h5a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-5" /></>,
  medal: <><circle cx="12" cy="15" r="5" /><path d="m9 10-3-7h4l2 4 2-4h4l-3 7M10 15l1.3 1 1.7-2" /></>,
  squad: (
    <><circle cx="9" cy="8" r="3" /><path d="M3 20v-2a5 5 0 0 1 5-5h2a5 5 0 0 1 5 5v2" /><path d="M16 5.5a3 3 0 0 1 0 5.8M17 14a5 5 0 0 1 4 4.9V20" /></>
  ),
  star: <path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z" />,
  transfers: (
    <><path d="M4 7h14M14 3l4 4-4 4M20 17H6M10 13l-4 4 4 4" /></>
  ),
  trash: <><path d="M3 6h18M8 6V4h8v2M19 6l-1 15H6L5 6M10 11v5M14 11v5" /></>,
  sync: <><path d="M20 7h-5V2" /><path d="M20 7a8 8 0 0 0-14-2M4 17h5v5" /><path d="M4 17a8 8 0 0 0 14 2" /></>,
  substitution: <><rect height="16" rx="2" width="18" x="3" y="4" /><path d="m8 10 2-2 2 2" /><path d="M10 8v8" /><path d="m16 14-2 2-2-2" /><path d="M14 16V8" /></>,
  timer: <><circle cx="12" cy="13" r="8" /><path d="M12 9v4l3 2M9 2h6" /></>,
  trophy: <><path d="M8 21h8M12 17v4M7 4h10v4a5 5 0 0 1-10 0z" /><path d="M7 6H4v2a4 4 0 0 0 4 4M17 6h3v2a4 4 0 0 1-4 4" /></>,
  menu: <><path d="M4 6h16M4 12h16M4 18h16" /></>,
}

function DashboardIcon({ name, size = 24, strokeWidth = 1.75, className = '', ...props }) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={strokeWidth}
      viewBox="0 0 24 24"
      width={size}
      {...props}
    >
      {paths[name] ?? paths.home}
    </svg>
  )
}

export default DashboardIcon
