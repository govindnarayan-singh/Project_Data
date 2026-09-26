(() => {
  const body = document.body;
  const navToggle = document.querySelector('.nav-toggle');
  const navDismiss = document.querySelector('[data-nav-dismiss]');
  const sidebar = document.querySelector('#primary-navigation');
  const sidebarToggles = Array.from(document.querySelectorAll('[data-sidebar-toggle]'));
  const contextToggle = document.querySelector('.context-toggle');
  const contextClose = document.querySelector('.context-close');
  const accountMenu = document.querySelector('[data-account-menu]');
  const accountToggle = document.querySelector('[data-account-menu-toggle]');
  const accountPanel = document.querySelector('#account-menu-panel');
  const mobileQuery = window.matchMedia('(max-width: 900px)');
  const sidebarStorageKey = 'app-sidebar-collapsed';

  const syncSidebarControls = () => {
    const drawerOpen = body.classList.contains('nav-open');
    const collapsed = body.classList.contains('sidebar-collapsed');
    const mobile = mobileQuery.matches;

    navToggle?.setAttribute('aria-expanded', String(drawerOpen));
    const navLabel = drawerOpen ? 'Close application navigation' : 'Open application navigation';
    navToggle?.setAttribute('aria-label', navLabel);
    if (navToggle) navToggle.title = navLabel;

    sidebarToggles.forEach(button => {
      const expanded = mobile ? drawerOpen : !collapsed;
      const label = mobile
        ? 'Close application navigation'
        : (collapsed ? 'Expand application navigation' : 'Collapse application navigation');
      button.setAttribute('aria-expanded', String(expanded));
      button.setAttribute('aria-label', label);
      button.title = label;
      const icon = button.querySelector('[data-sidebar-toggle-icon]');
      if (icon) icon.textContent = String.fromCharCode(mobile ? 215 : (collapsed ? 8250 : 8249));
    });
  };

  const setNav = (open, {restoreFocus = false, moveFocus = false} = {}) => {
    body.classList.toggle('nav-open', open);
    syncSidebarControls();
    if (open && moveFocus) {
      window.requestAnimationFrame(() => sidebar?.querySelector('.nav-link')?.focus());
    } else if (!open && restoreFocus) {
      navToggle?.focus();
    }
  };

  const setContext = open => {
    body.classList.toggle('context-collapsed', !open);
    contextToggle?.setAttribute('aria-expanded', String(open));
  };

  const setSidebar = (collapsed, persist = true) => {
    body.classList.toggle('sidebar-collapsed', collapsed);
    syncSidebarControls();
    if (persist) {
      try {
        localStorage.setItem(sidebarStorageKey, collapsed ? '1' : '0');
      } catch (_) {
        /* A blocked preference store must not affect navigation. */
      }
    }
    window.dispatchEvent(new CustomEvent('app:sidebarchange', {detail: {collapsed}}));
  };

  const setAccountMenu = (open, {restoreFocus = false, moveFocus = false} = {}) => {
    if (!accountToggle || !accountPanel) return;
    accountToggle.setAttribute('aria-expanded', String(open));
    accountPanel.hidden = !open;
    if (open && moveFocus) {
      window.requestAnimationFrame(() => accountPanel.querySelector('[role="menuitem"]')?.focus());
    } else if (!open && restoreFocus) {
      accountToggle.focus();
    }
  };

  let sidebarCollapsed = false;
  try {
    sidebarCollapsed = localStorage.getItem(sidebarStorageKey) === '1';
  } catch (_) {
    /* A blocked preference store must not affect navigation. */
  }
  setSidebar(sidebarCollapsed, false);
  setNav(false);
  if (mobileQuery.matches) setContext(false);

  navToggle?.addEventListener('click', () => {
    setAccountMenu(false);
    setNav(!body.classList.contains('nav-open'), {moveFocus: true});
  });

  navDismiss?.addEventListener('click', () => setNav(false, {restoreFocus: true}));

  sidebarToggles.forEach(button => button.addEventListener('click', () => {
    if (mobileQuery.matches) {
      setNav(false, {restoreFocus: true});
      return;
    }
    setSidebar(!body.classList.contains('sidebar-collapsed'));
  }));

  sidebar?.querySelectorAll('.nav-link').forEach(link => link.addEventListener('click', () => {
    if (mobileQuery.matches) setNav(false);
  }));

  contextToggle?.addEventListener('click', () => {
    setAccountMenu(false);
    setContext(body.classList.contains('context-collapsed'));
  });
  contextClose?.addEventListener('click', () => setContext(false));

  accountToggle?.addEventListener('click', () => {
    const open = accountToggle.getAttribute('aria-expanded') !== 'true';
    if (open && mobileQuery.matches) setNav(false);
    setAccountMenu(open);
  });

  accountToggle?.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setAccountMenu(true, {moveFocus: true});
    }
  });

  document.addEventListener('click', event => {
    if (accountMenu && !accountMenu.contains(event.target)) setAccountMenu(false);
  });

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (accountToggle?.getAttribute('aria-expanded') === 'true') {
      event.preventDefault();
      setAccountMenu(false, {restoreFocus: true});
    } else if (body.classList.contains('nav-open')) {
      event.preventDefault();
      setNav(false, {restoreFocus: true});
    }
  });

  const handleBreakpointChange = event => {
    setNav(false);
    if (event.matches) setContext(false);
    syncSidebarControls();
  };
  if (mobileQuery.addEventListener) {
    mobileQuery.addEventListener('change', handleBreakpointChange);
  } else {
    mobileQuery.addListener(handleBreakpointChange);
  }

  document.querySelectorAll('[data-source-select]').forEach(button => button.addEventListener('click', () => {
    const id = button.dataset.sourceSelect;
    document.querySelectorAll('[data-source-card]').forEach(card => card.classList.toggle('selected', card.dataset.sourceCard === id));
    document.querySelectorAll('[data-source-context]').forEach(panel => { panel.hidden = panel.dataset.sourceContext !== id; });
    setContext(true);
  }));

  const form = document.querySelector('[data-search-form]');
  form?.querySelector('textarea')?.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form?.addEventListener('submit', () => {
    form.classList.add('loading');
    form.querySelector('button[type=submit]')?.setAttribute('aria-busy', 'true');
  });
})();
