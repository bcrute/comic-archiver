// Shows the download for the visitor's own system first. It reads only what the browser already says about
// the system it runs on; nothing is kept or sent anywhere. Without the script every download shows as before.
(function () {
  var NAMES = { windows: 'Windows', mac: 'Mac', linux: 'Linux', android: 'Android' };

  function system() {
    var agent = navigator.userAgent || '';
    var platform = (navigator.userAgentData && navigator.userAgentData.platform) || '';
    if (/android/i.test(platform) || /Android/.test(agent)) return 'android';
    // iPads ask for the desktop site and say "Macintosh"; only they have a touch screen.
    if (/iPhone|iPad|iPod/.test(agent) || (/Macintosh/.test(agent) && navigator.maxTouchPoints > 1)) return 'ios';
    if (/chrome os|cros/i.test(platform) || /CrOS/.test(agent)) return null;
    if (/windows/i.test(platform) || /Windows/.test(agent)) return 'windows';
    if (/mac/i.test(platform) || /Macintosh|Mac OS X/.test(agent)) return 'mac';
    if (/linux/i.test(platform) || /Linux|X11/.test(agent)) return 'linux';
    return null;
  }

  document.addEventListener('DOMContentLoaded', function () {
    var os = system();
    if (!os) return;
    // Phones: a note, or the phone app, above the computer downloads.
    document.querySelectorAll('.for-device[data-os="' + os + '"]').forEach(function (block) { block.hidden = false; });
    // Computers: their download first and marked, and its install steps open and first.
    var panel = document.querySelector('.panels .panel[data-os="' + os + '"]');
    if (panel) {
      var tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = 'Your computer';
      panel.querySelector('h3').appendChild(tag);
      panel.classList.add('yours');
      panel.parentNode.classList.add('picked');
      panel.parentNode.insertBefore(panel, panel.parentNode.firstChild);
    }
    var steps = document.querySelector('details[data-os="' + os + '"]');
    if (steps) {
      steps.open = true;
      var heading = steps.parentNode.querySelector('h2');
      if (heading) heading.after(steps);
    }
    // The home page's button names the system it downloads for.
    var button = document.querySelector('a[data-download]');
    var android = document.querySelector('meta[name="android-app"]');
    if (button && os === 'android' && android) button.textContent = 'Download for Android';
    else if (button && os !== 'android' && os !== 'ios') button.textContent += ' for ' + NAMES[os];
  });
})();
