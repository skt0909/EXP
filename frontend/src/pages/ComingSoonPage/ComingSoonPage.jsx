import { useLocation } from 'react-router-dom'
import BottomNav from '../../components/BottomNav/BottomNav'
import './ComingSoonPage.css'

function titleFromPath(pathname) {
  const segment = pathname.replace('/', '')
  if (!segment) return 'Page'
  return segment.charAt(0).toUpperCase() + segment.slice(1)
}

function ComingSoonPage() {
  const { pathname } = useLocation()

  return (
    <div className="coming-soon">
      <div className="coming-soon__content">
        <span className="material-symbols-outlined coming-soon__icon">construction</span>
        <h1>{titleFromPath(pathname)}</h1>
        <p>This page is coming soon.</p>
      </div>
      <BottomNav />
    </div>
  )
}

export default ComingSoonPage
