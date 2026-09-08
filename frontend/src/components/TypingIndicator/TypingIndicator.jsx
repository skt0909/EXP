import './TypingIndicator.css'

function TypingIndicator() {
  return (
    <div className="flex w-full gap-3 items-end justify-start">
      <div className="w-7 h-7 rounded-full bg-surface-container-high border border-surface-variant shrink-0 flex items-center justify-center mb-1">
        <span className="material-symbols-outlined text-[16px] text-primary">sports_soccer</span>
      </div>
      {/* Fixed height matches a one-line bubble so the log doesn't jump when
          the real answer replaces the dots. */}
      <div className="bg-surface-container-lowest border border-surface-variant rounded-[20px] rounded-tl-[4px] px-5 py-3 shadow-sm flex items-center h-[46px]">
        <div className="typing-indicator flex items-center" aria-label="PitchSide AI is typing">
          <span />
          <span />
          <span />
        </div>
      </div>
    </div>
  )
}

export default TypingIndicator
