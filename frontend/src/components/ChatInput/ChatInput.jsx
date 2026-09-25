import { useState } from 'react'

function ChatInput({ onSend, disabled }) {
  const [value, setValue] = useState('')

  function handleSubmit(e) {
    e.preventDefault()
    const trimmed = value.trim()
    if (!trimmed) return
    onSend(trimmed)
    setValue('')
  }

  return (
    <form
      className="flex items-center gap-sm bg-surface-container-lowest border border-surface-variant rounded-full p-[6px] pl-4 shadow-sm focus-within:border-primary-container focus-within:ring-1 focus-within:ring-primary-container transition-all"
      onSubmit={handleSubmit}
    >
      <input
        autoComplete="off"
        className="flex-1 min-w-0 bg-transparent border-none focus:ring-0 focus:outline-none font-body-md text-body-md text-on-surface placeholder:text-on-surface-variant py-2 disabled:opacity-60"
        disabled={disabled}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Message PitchSide AI..."
        type="text"
        value={value}
      />
      <button
        aria-label="Send"
        className="w-[36px] h-[36px] rounded-full bg-primary-container text-on-primary flex items-center justify-center shrink-0 shadow-sm hover:opacity-90 transition-opacity focus:outline-none disabled:opacity-50"
        disabled={disabled}
        type="submit"
      >
        <span className="material-symbols-outlined text-[20px]">send</span>
      </button>
    </form>
  )
}

export default ChatInput
