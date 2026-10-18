'use strict';
(() => {
  const key = 'comic-archive-theme';
  const device = window.matchMedia('(prefers-color-scheme: dark)');
  let preference;
  try {
    preference = localStorage.getItem(key);
  } catch { /* Storage may be blocked; the device theme still works. */ }
  if (!['light', 'dark'].includes(preference)) preference = null;
  function apply() {
    const dark = preference ? preference === 'dark' : device.matches;
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    const button = document.getElementById('theme-toggle');
    if (button) {
      button.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
      button.setAttribute('aria-pressed', String(dark));
      button.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
    }
  }
  apply();
  device.addEventListener('change', () => {if (!preference) apply();});
  window.addEventListener('storage', event => {
    if (event.key === key) {preference = ['light','dark'].includes(event.newValue) ? event.newValue : null; apply();}
  });
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.getElementById('theme-toggle')?.addEventListener('click', () => {
      preference = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      try {localStorage.setItem(key, preference);} catch { /* Keep this choice for the open page. */ }
      apply();
      // The desktop app keeps the choice itself: its page storage does not outlive the window.
      document.dispatchEvent(new CustomEvent('comic-archive-theme', {detail: preference}));
    });
  });
})();
