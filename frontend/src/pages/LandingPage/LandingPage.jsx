import { useNavigate } from 'react-router-dom'
import './LandingPage.css'

const FEATURES = [
  {
    icon: 'lightbulb',
    title: 'Smart Captaincy',
    description:
      'AI-powered analysis of form, underlying metrics, and upcoming fixtures to ensure the armband lands on the right player.',
    featured: true,
  },
  {
    icon: 'trending_up',
    title: 'Transfer Intelligence',
    description: 'Spot the next big riser before they trend. Stay ahead of price changes.',
  },
  {
    icon: 'account_balance_wallet',
    title: 'Squad Optimization',
    description: 'Balance your budget efficiently and maximize long-term points potential.',
  },
]

function LandingPage() {
  const navigate = useNavigate()

  return (
    <div className="landing-page">
      <header className="landing-header">
        <h1 className="landing-header__title">PitchSide AI</h1>
      </header>

      <main className="landing-main">
        <section className="hero">
          <h2 className="hero__title">Dominate Your FPL League with AI</h2>
          <p className="hero__subtitle">
            Get data-driven captaincy picks, transfer advice, and squad optimizations in seconds.
          </p>
          <button className="hero__cta" onClick={() => navigate('/chat')}>
            Get Started
          </button>
        </section>

        <section className="features">
          {FEATURES.map((f) => (
            <div key={f.title} className={`feature-card ${f.featured ? 'feature-card--featured' : ''}`}>
              <div className="feature-card__icon">
                <span className="material-symbols-outlined">{f.icon}</span>
              </div>
              <h3 className="feature-card__title">{f.title}</h3>
              <p className="feature-card__description">{f.description}</p>
            </div>
          ))}
        </section>
      </main>

      <footer className="landing-footer">
        <h2 className="landing-footer__title">PitchSide AI</h2>
        <div className="landing-footer__links">
          <span>Privacy Policy</span>
          <span>Terms of Service</span>
          <span>Contact</span>
        </div>
        <p className="landing-footer__copy">© 2026 PitchSide AI. All rights reserved.</p>
      </footer>
    </div>
  )
}

export default LandingPage
