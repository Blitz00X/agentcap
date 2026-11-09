(function () {
  var allowed = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'];
  var buy = document.querySelector('a[href^="https://buy.polar.sh/"]');
  if (!buy) return;
  var incoming = new URLSearchParams(window.location.search);
  var outgoing = new URL(buy.href);
  allowed.forEach(function (key) {
    var value = incoming.get(key);
    if (!value) return;
    outgoing.searchParams.set(key, value.slice(0, 200));
  });
  buy.href = outgoing.toString();
})();

document.querySelectorAll('[data-copy]').forEach(function (button) {
  button.addEventListener('click', function () {
    var text = button.getAttribute('data-copy');
    var done = function () {
      var old = button.textContent;
      button.textContent = 'Copied';
      button.classList.add('is-copied');
      window.setTimeout(function () {
        button.textContent = old;
        button.classList.remove('is-copied');
      }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done).catch(function () {});
    } else {
      var field = document.createElement('textarea');
      field.value = text;
      field.setAttribute('readonly', '');
      field.className = 'copy-fallback';
      document.body.appendChild(field);
      field.select();
      try { document.execCommand('copy'); done(); } catch (error) {}
      document.body.removeChild(field);
    }
  });
});
