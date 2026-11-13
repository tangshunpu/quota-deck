// Run before the stylesheet/app so restoring a theme never flashes a white page.
(() => {
  const STORAGE_KEY = 'quota-deck.theme.v2';
  const scheduledTheme = (now = new Date()) => now.getHours() >= 18 || now.getHours() < 7 ? 'dark' : 'light';
  const nextBoundary = (now = new Date()) => {
    const next = new Date(now);
    if (now.getHours() < 7) next.setHours(7, 0, 0, 0);
    else if (now.getHours() < 18) next.setHours(18, 0, 0, 0);
    else { next.setDate(next.getDate() + 1); next.setHours(7, 0, 0, 0); }
    return next.getTime();
  };
  const validOverride = value => value && ['light', 'dark'].includes(value.theme) &&
    Number.isFinite(value.until) && value.until > Date.now();
  let override, hasStored = false;
  try { const saved = localStorage.getItem(STORAGE_KEY); hasStored = saved !== null; override = JSON.parse(saved); } catch {}
  if (!hasStored && !validOverride(override)) override = window.quotaNativeTheme;
  if (!validOverride(override)) override = null;

  const apply = theme => {
    const changed = document.documentElement.dataset.theme !== theme;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#101113' : '#F5F6F8');
    if (changed) window.dispatchEvent(new Event('quota-theme-change'));
  };
  let timer;
  const sync = () => {
    clearTimeout(timer);
    if (!validOverride(override)) override = null;
    apply(override ? override.theme : scheduledTheme());
    // Exact boundary timer, with a one-minute ceiling to recover from clock changes.
    const due = Math.min(nextBoundary(), override?.until ?? Infinity);
    timer = setTimeout(sync, Math.max(1, Math.min(60000, due - Date.now())));
  };
  const toggle = () => {
    override = {theme: document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', until: nextBoundary()};
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(override)); } catch {}
    window.webkit?.messageHandlers?.quota?.postMessage(`theme:${JSON.stringify(override)}`);
    sync();
  };
  // The physical screen follows the shared backend; other browser themes stay local.
  const setOverride = value => {
    override = validOverride(value) ? value : null;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(override)); } catch {}
    sync();
  };
  window.quotaTheme = {toggle, setOverride};
  document.addEventListener('visibilitychange', sync);
  window.addEventListener('pageshow', sync);
  window.addEventListener('focus', sync);
  sync();
})();
