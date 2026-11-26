(function () {
  'use strict';

  var statusCache = {};
  var NOT_REGISTERED = '如果没有打开：请先在「设置 → 下载器 → 本地播放」导入注册表文件，导入后重启浏览器；只在导入过的电脑上可用。';

  function toast(message, level) {
    if (window.showToast) window.showToast(message, level);
  }

  function openLink(action, movieId) {
    // Windows hands reelready:// links to the handler registered by the .reg file.
    var blurred = false;
    function onBlur() { blurred = true; }
    window.addEventListener('blur', onBlur, {once: true});
    setTimeout(function () {
      window.removeEventListener('blur', onBlur);
      if (!blurred && !document.hidden) toast(NOT_REGISTERED, 'warn');
    }, 2500);
    window.location.href = 'reelready://' + action + '/' + encodeURIComponent(movieId);
  }

  function render(container, status) {
    var play = container.querySelector('[data-playback-action="play"]');
    var label = play && play.querySelector('[data-playback-label]');
    var state = container.querySelector('[data-playback-state]');
    container.dataset.playbackStatus = status.state || 'error';
    if (label) {
      label.textContent = status.state === 'downloading' ? Math.floor((status.progress || 0) * 100) + '%' : '播放';
    }
    if (play) {
      play.title = status.state === 'ready' ? '用默认播放器播放' : (status.message || '');
      play.classList.toggle('is-waiting', status.state !== 'ready');
    }
    if (state) {
      state.textContent = '';
      var line = document.createElement('span');
      line.className = 'playback-state-label state-' + (status.state || 'error');
      line.textContent = status.message || '';
      state.appendChild(line);
      if (status.path) {
        var path = document.createElement('code');
        path.className = 'playback-path';
        path.textContent = status.path;
        path.title = status.path;
        state.appendChild(path);
      }
    }
  }

  function refresh(root) {
    var containers = Array.from((root || document).querySelectorAll('[data-playback]'));
    var pending = containers.filter(function (el) { return !el.dataset.playbackLoaded; });
    if (!pending.length) return;
    pending.forEach(function (el) {
      el.dataset.playbackLoaded = '1';
      // Show the last known state at once when a live refresh rebuilds the cards.
      if (statusCache[el.dataset.playback]) render(el, statusCache[el.dataset.playback]);
    });
    var ids = Array.from(new Set(pending.map(function (el) { return el.dataset.playback; })));
    fetch('/playback/status?ids=' + ids.join(','), {headers: {Accept: 'application/json'}})
      .then(function (response) { return response.json(); })
      .then(function (data) {
        pending.forEach(function (el) {
          var status = (data.movies || {})[el.dataset.playback] ||
            (data.error ? {state: 'error', message: data.error} : {state: 'missing', message: '没有找到下载记录'});
          statusCache[el.dataset.playback] = status;
          render(el, status);
        });
      })
      .catch(function () {
        pending.forEach(function (el) { render(el, {state: 'error', message: '读取下载状态失败'}); });
      });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return legacyCopy(text); });
    }
    return legacyCopy(text);
  }

  function legacyCopy(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    var ok = document.execCommand('copy');
    area.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error('copy failed'));
  }

  document.addEventListener('click', function (evt) {
    var button = evt.target.closest('[data-playback-action]');
    if (!button) return;
    evt.preventDefault();
    evt.stopPropagation();
    var movieId = button.dataset.movieId;
    var action = button.dataset.playbackAction;
    var status = statusCache[movieId];
    if (action === 'copy') {
      if (!status || !status.path) { toast((status && status.message) || '还没有读取到文件路径', 'warn'); return; }
      copyText(status.path).then(function () { toast('已复制路径'); }, function () { toast('复制失败，请手动选择路径', 'error'); });
      return;
    }
    if (action === 'folder' && status && !status.path) {
      toast(status.message || '还没有找到文件位置', 'warn');
      return;
    }
    if (action === 'play' && status && status.state !== 'ready') {
      toast(status.message || '还不能播放', 'warn');
      return;
    }
    if (!/^\d+$/.test(movieId || '')) return;
    openLink(action, movieId);
  });

  document.addEventListener('htmx:afterSwap', function (evt) { refresh(evt.target); });
  document.addEventListener('reelready:movies-replaced', function (evt) { refresh(evt.detail.root); });
  refresh(document);
})();
