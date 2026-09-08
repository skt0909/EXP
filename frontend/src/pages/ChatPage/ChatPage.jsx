import { useState } from 'react'
import Header from '../../components/Header/Header'
import ChatLog from '../../components/ChatLog/ChatLog'
import ChatInput from '../../components/ChatInput/ChatInput'
import { sendChatMessage } from '../../api/chat'
import { CURRENT_GAMEWEEK, CURRENT_SEASON } from '../../config/season'
import './ChatPage.css'

const INITIAL_MESSAGES = [
  {
    role: 'assistant',
    text: "Ask me anything about your starting XI -- captaincy calls, transfer hits, who's in form.",
  },
]

function ChatPage() {
  const [settings, setSettings] = useState({
    user_id: 1,
    season: CURRENT_SEASON,
    gameweek: CURRENT_GAMEWEEK,
  })
  const [showSettings, setShowSettings] = useState(false)
  const [messages, setMessages] = useState(INITIAL_MESSAGES)
  const [isTyping, setIsTyping] = useState(false)

  async function handleSend(text) {
    setMessages((prev) => [...prev, { role: 'user', text }])
    setIsTyping(true)

    try {
      const data = await sendChatMessage({ ...settings, message: text })
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: data.response || data.detail || '(no response/detail field)' },
      ])
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: `Error reaching PitchSide AI -- likely CORS or server not running. (${err})` },
      ])
    } finally {
      setIsTyping(false)
    }
  }

  return (
    <div className="chat-page">
      <Header
        showSettings={showSettings}
        onToggleSettings={() => setShowSettings((v) => !v)}
        settings={settings}
        onChangeSettings={setSettings}
      />
      <ChatLog messages={messages} isTyping={isTyping} />
      <ChatInput onSend={handleSend} disabled={isTyping} />
    </div>
  )
}

export default ChatPage
