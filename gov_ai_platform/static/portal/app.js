(() => {
  const body = document.body;
  const navToggle = document.querySelector('.nav-toggle');
  const contextToggle = document.querySelector('.context-toggle');
  const contextClose = document.querySelector('.context-close');
  const setNav = open => { body.classList.toggle('nav-open', open); navToggle?.setAttribute('aria-expanded', String(open)); };
  const setContext = open => { body.classList.toggle('context-collapsed', !open); contextToggle?.setAttribute('aria-expanded', String(open)); };
  if (window.matchMedia('(max-width: 820px)').matches) setContext(false);
  navToggle?.addEventListener('click', () => setNav(!body.classList.contains('nav-open')));
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
