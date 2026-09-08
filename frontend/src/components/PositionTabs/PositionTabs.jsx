const TABS = ['ALL', 'GK', 'DEF', 'MID', 'FWD']

function PositionTabs({ active, onChange }) {
  return (
    <div className="flex px-md gap-sm overflow-x-auto mb-sm [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      {TABS.map((tab) => (
        <button
          className={`px-5 py-2 rounded-full font-label-md text-label-md whitespace-nowrap transition-colors ${
            active === tab
              ? 'bg-primary-container text-on-primary shadow-sm'
              : 'border border-outline-variant text-on-surface hover:bg-surface-container-lowest'
          }`}
          key={tab}
          onClick={() => onChange(tab)}
          type="button"
        >
          {tab}
        </button>
      ))}
    </div>
  )
}

export default PositionTabs
