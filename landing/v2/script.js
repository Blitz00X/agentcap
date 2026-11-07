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
