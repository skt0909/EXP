import { useEffect, useRef } from 'react'
import MessageBubble from '../MessageBubble/MessageBubble'
import TypingIndicator from '../TypingIndicator/TypingIndicator'

function ChatLog({ messages, isTyping }) {
  const logRef = useRef(null)

  // Pin to the newest message. isTyping is in the deps so the log also scrolls
  // when the dots appear, not just when text lands.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [messages, isTyping])

  return (
    <main
      // pb covers the fixed composer below (mode toggle + optional contest
      // picker + text bar), which is taller and more variable now that the
      // toggle lives there too -- 190px clears the toggle+input case with
      // room to spare; the contest-picker case just leaves a bit more
      // scrollable headroom, which is harmless.
      className="flex-1 overflow-y-auto px-md pt-lg pb-[190px] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      ref={logRef}
    >
      <div className="flex flex-col gap-6">
        {messages.map((m, i) => (
          <MessageBubble key={i} role={m.role} text={m.text} />
        ))}
        {isTyping && <TypingIndicator />}
      </div>
    </main>
  )
}

export default ChatLog
