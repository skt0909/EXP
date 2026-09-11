import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import ChatLog from '../../components/ChatLog/ChatLog'
import ChatInput from '../../components/ChatInput/ChatInput'
import FplHeader from '../../components/FplHeader/FplHeader'
import { sendChatMessage } from '../../api/chat'

const WELCOME_MESSAGE = {
  role: 'assistant',
  text: "Hi, I'm PitchSide AI. Ask me about your starting XI, captain choice, or anything else about your squad.",
}

function ChatPage() {
  const { settings } = useOutletContext()
  const [messages, setMessages] = useState([WELCOME_MESSAGE])
  const [isTyping, setIsTyping] = useState(false)

  async function handleSend(text) {
    setMessages((prev) => [...prev, { role: 'user', text }])
    setIsTyping(true)
    try {
      const { response } = await sendChatMessage({ ...settings, message: text })
      setMessages((prev) => [...prev, { role: 'assistant', text: response }])
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: 'Something went wrong reaching PitchSide AI -- please try again.' },
      ])
    } finally {
      setIsTyping(false)
    }
  }

  // Fills the space Layout gives it (viewport minus header and BottomNav) so
  // the log scrolls internally and the composer stays put at the bottom.
  // FplHeader's bar is a fixed h-16 (64px), same height the old bare header
  // used, so the h-[calc(...)] math above still holds unchanged. ChatLog is
  // already flex-1 (see that component), so it absorbs whatever height the
  // header doesn't use.
  return (
    <div className="flex flex-col h-[calc(100dvh-64px-88px)]">
      <FplHeader title="Assist" />
      <ChatLog isTyping={isTyping} messages={messages} />
      <ChatInput disabled={isTyping} onSend={handleSend} />
    </div>
  )
}

export default ChatPage
