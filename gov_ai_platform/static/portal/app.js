(() => {
  const body = document.body;
  const navToggle = document.querySelector('.nav-toggle');
  const sidebarToggles = Array.from(document.querySelectorAll('[data-sidebar-toggle]'));
  const contextToggle = document.querySelector('.context-toggle');
  const contextClose = document.querySelector('.context-close');
  const sidebarStorageKey = 'app-sidebar-collapsed';
  const setNav = open => { body.classList.toggle('nav-open', open); navToggle?.setAttribute('aria-expanded', String(open)); };
  const setContext = open => { body.classList.toggle('context-collapsed', !open); contextToggle?.setAttribute('aria-expanded', String(open)); };
  const setSidebar = (collapsed, persist = true) => {
    body.classList.toggle('sidebar-collapsed', collapsed);
    sidebarToggles.forEach(button => {
      const expanded = !collapsed;
      const label = expanded ? 'Collapse application navigation' : 'Expand application navigation';
      button.setAttribute('aria-expanded', String(expanded));
      button.setAttribute('aria-label', label);
      button.title = label;
      const icon = button.querySelector('[data-sidebar-toggle-icon]');
      if (icon) icon.textContent = expanded ? '‹' : '›';
    });
    if (persist) {
      try { localStorage.setItem(sidebarStorageKey, collapsed ? '1' : '0'); } catch (_) { /* storage may be unavailable */ }
    }
    window.dispatchEvent(new CustomEvent('app:sidebarchange', {detail: {collapsed}}));
  };
  let sidebarCollapsed = false;
  try { sidebarCollapsed = localStorage.getItem(sidebarStorageKey) === '1'; } catch (_) { /* storage may be unavailable */ }
  setSidebar(sidebarCollapsed, false);
  if (window.matchMedia('(max-width: 820px)').matches) setContext(false);
  navToggle?.addEventListener('click', () => setNav(!body.classList.contains('nav-open')));
  sidebarToggles.forEach(button => button.addEventListener('click', () => {
    sidebarCollapsed = !body.classList.contains('sidebar-collapsed');
    setSidebar(sidebarCollapsed);
    if (window.matchMedia('(max-width: 820px)').matches) setNav(false);
  }));
  contextToggle?.addEventListener('click', () => setContext(body.classList.contains('context-collapsed')));
  contextClose?.addEventListener('click', () => setContext(false));
  document.querySelectorAll('[data-source-select]').forEach(button => button.addEventListener('click', () => {
    const id = button.dataset.sourceSelect;
    document.querySelectorAll('[data-source-card]').forEach(card => card.classList.toggle('selected', card.dataset.sourceCard === id));
    document.querySelectorAll('[data-source-context]').forEach(panel => panel.hidden = panel.dataset.sourceContext !== id);
    setContext(true);
  }));
  const form = document.querySelector('[data-search-form]');
  form?.querySelector('textarea')?.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); form.requestSubmit(); }
  });
  form?.addEventListener('submit', () => { form.classList.add('loading'); form.querySelector('button[type=submit]')?.setAttribute('aria-busy', 'true'); });
})();
