function UnsavedChangesModal({ onStay, onLeave }) {
  return (
    <div
      aria-labelledby="unsaved-title"
      aria-modal="true"
      className="fixed inset-0 z-[70] flex items-center justify-center px-md"
      role="alertdialog"
    >
      <div className="absolute inset-0 bg-black/40" onClick={onStay} />
      <div className="relative w-full max-w-[400px] bg-surface-container-lowest rounded-[20px] p-md shadow-lg border border-outline-variant">
        <h2 className="font-headline-sm text-headline-sm text-on-surface mb-xs" id="unsaved-title">
          Unsaved team
        </h2>
        <p className="font-body-md text-body-md text-on-surface-variant mb-md">
          Your team hasn&apos;t been saved. If you leave now, your selection will be lost.
        </p>
        <div className="flex gap-sm">
          <button
            autoFocus
            className="flex-1 py-2 rounded-full bg-primary-container text-on-primary font-label-md text-label-md"
            onClick={onStay}
            type="button"
          >
            Stay
          </button>
          <button
            className="flex-1 py-2 rounded-full border border-outline-variant text-error font-label-md text-label-md hover:bg-surface-container-low"
            onClick={onLeave}
            type="button"
          >
            Leave anyway
          </button>
        </div>
      </div>
    </div>
  )
}

export default UnsavedChangesModal
