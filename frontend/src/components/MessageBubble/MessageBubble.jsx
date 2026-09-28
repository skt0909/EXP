/**
 * Asymmetric bubbles, per the design: the user's are filled primary-container
 * and right-aligned with a squared top-right corner; the assistant's are
 * surface with a border, left-aligned, squared top-left, and preceded by the
 * ball avatar. The squared corner is what makes the tail read as pointing at
 * its sender.
 */
function MessageBubble({ role, text, attachment }) {
  const isUser = role === 'user'

  return (
    <div className={`flex w-full gap-3 items-end ${isUser ? 'justify-end' : 'justify-start'}`}>
      {!isUser && (
        <div className="w-7 h-7 rounded-full bg-surface-container-high border border-surface-variant shrink-0 flex items-center justify-center mb-1">
          <span className="material-symbols-outlined text-[16px] text-primary">sports_soccer</span>
        </div>
      )}
      <div
        className={`px-5 py-3 max-w-[85%] shadow-sm font-body-md text-body-md ${
          isUser
            ? 'bg-primary-container text-on-primary rounded-[20px] rounded-tr-[4px]'
            : 'bg-surface-container-lowest text-on-surface border border-surface-variant rounded-[20px] rounded-tl-[4px]'
        }`}
      >
        {/* whitespace-pre-wrap keeps the model's paragraph breaks; without it
            a multi-paragraph answer collapses into one run-on block. */}
        <p className="whitespace-pre-wrap break-words">{text}</p>
        {attachment && (
          <a
            className="mt-3 flex items-center gap-2 rounded-xl border border-outline-variant bg-surface-container-lowest px-3 py-2 hover:bg-surface-container-low transition-colors"
            download={attachment.name}
            href={attachment.href}
            rel="noopener noreferrer"
            target="_blank"
          >
            <span className="w-8 h-8 rounded-lg bg-error-container text-on-error-container flex items-center justify-center shrink-0">
              <span className="material-symbols-outlined text-[18px]">picture_as_pdf</span>
            </span>
            <span className="min-w-0 flex-1">
              <span className="block font-label-md text-label-md font-semibold text-on-surface truncate">
                {attachment.name}
              </span>
              <span className="block font-label-md text-[10px] text-on-surface-variant">Tap to download</span>
            </span>
            <span className="material-symbols-outlined text-[18px] text-on-surface-variant shrink-0">download</span>
          </a>
        )}
      </div>
    </div>
  )
}

export default MessageBubble
