import AccountMenu from '../AccountMenu/AccountMenu'
import DashboardIcon from '../DashboardIcon/DashboardIcon'

function DetailHeader({ onBack, title }) {
  return (
    <header className="sticky top-0 z-40 -mx-safe-margin -mt-md mb-sm bg-surface/95 backdrop-blur-xl border-b border-outline-variant/60">
      <div className="flex items-center gap-sm h-16 px-md w-full max-w-[600px] mx-auto">
        <button
          aria-label="Go back"
          className="w-11 h-11 rounded-full flex items-center justify-center text-on-surface hover:bg-surface-container-high transition-colors"
          onClick={onBack}
          type="button"
        >
          <DashboardIcon name="arrowBack" size={21} />
        </button>
        <h1 className="flex-1 min-w-0 text-left truncate font-headline-sm text-headline-sm font-semibold text-on-surface">
          {title}
        </h1>
        <button
          aria-label="Help"
          className="w-10 h-10 rounded-full border border-outline-variant/70 bg-surface-container-lowest shadow-sm flex items-center justify-center hover:bg-surface-container-high active:scale-95 transition-all"
          type="button"
        >
          <DashboardIcon name="help" size={19} />
        </button>
        <AccountMenu />
      </div>
    </header>
  )
}

export default DetailHeader
