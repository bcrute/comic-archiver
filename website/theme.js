// Theme: System (the default), Light or Dark, chosen from the header's theme menu.
// A choice other than System is kept in this browser only (localStorage) and is never sent anywhere.
(function () {
  var root = document.documentElement;
  var NAMES = { system: 'System', light: 'Light', dark: 'Dark' };
  function saved() {
    try { var value = localStorage.getItem('theme'); return value === 'light' || value === 'dark' ? value : 'system'; }
    catch (e) { return 'system'; }
  }
  function apply(choice) {
    if (choice === 'system') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', choice);
    root.setAttribute('data-theme-choice', choice);
  }
  function keep(choice) {
    try { if (choice === 'system') localStorage.removeItem('theme'); else localStorage.setItem('theme', choice); } catch (e) {}
  }

  apply(saved());
  root.classList.add('js');

  document.addEventListener('DOMContentLoaded', function () {
    var pick = document.querySelector('.theme-pick');
    if (!pick) return;
    var button = pick.querySelector('button.theme');
    var menu = pick.querySelector('.theme-menu');
    var items = menu.querySelectorAll('button[data-choice]');

    function show(choice) {
      button.setAttribute('aria-label', 'Theme: ' + NAMES[choice]);
      button.title = 'Theme: ' + NAMES[choice];
      items.forEach(function (item) { item.setAttribute('aria-pressed', String(item.dataset.choice === choice)); });
    }
    function open(state) {
      menu.hidden = !state;
      button.setAttribute('aria-expanded', String(state));
    }
    show(saved());
    button.addEventListener('click', function () { open(menu.hidden); });
    items.forEach(function (item) {
      item.addEventListener('click', function () {
        var choice = item.dataset.choice;
        apply(choice); keep(choice); show(choice); open(false); button.focus();
      });
    });
    document.addEventListener('click', function (event) { if (!pick.contains(event.target)) open(false); });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !menu.hidden) { open(false); button.focus(); }
    });
  });
})();
