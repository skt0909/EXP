function AuthBrand({ subtitle }) {
  return (
    <div className="mb-lg flex flex-col items-center text-center">
      <div className="w-full max-w-[230px] h-[145px] mb-md">
        <img
          alt="Football team preparing on the training ground"
          className="w-full h-full object-contain mix-blend-multiply"
          src="/assets/stitch/login-footballers.png"
        />
      </div>
      <div className="inline-flex items-center gap-1.5 rounded-full border border-primary-container/40 bg-primary-container/10 px-3 py-1 mb-sm text-[10px] font-semibold uppercase tracking-[0.12em] text-primary">
        <span className="w-1.5 h-1.5 rounded-full bg-primary-container" />
        Manager access
      </div>
      <h1 className="text-[30px] leading-none font-semibold text-on-surface">PitchSide</h1>
      {subtitle && <p className="font-body-md text-body-md text-on-surface-variant mt-sm">{subtitle}</p>}
    </div>
  )
}

export default AuthBrand
