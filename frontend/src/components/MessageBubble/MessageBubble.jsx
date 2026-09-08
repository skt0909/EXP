/**
 * Asymmetric bubbles, per the design: the user's are filled primary-container
 * and right-aligned with a squared top-right corner; the assistant's are
 * surface with a border, left-aligned, squared top-left, and preceded by the
 * ball avatar. The squared corner is what makes the tail read as pointing at
 * its sender.
 */
function MessageBubble({ role, text }) {
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
      </div>
    </div>
  )
}

export default MessageBubble
