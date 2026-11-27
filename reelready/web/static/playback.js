(function () {
  'use strict';

  var statusCache = {};
  var NOT_LAUNCHED = '浏览器没有调用本地播放：请确认已在「设置 → 下载器 → 本地播放」导入注册表文件并完全重启浏览器，浏览器询问时选择「打开」。Edge 请在「设置 → 系统和性能」关闭「启动增强」，否则开机由启动增强打开的 Edge 不会打开本地链接。';

  function toast(message, level) {
    if (window.showToast) window.showToast(message, level);
  }

  function openLink(action, movieId) {
    // Windows hands reelready:// links to the handler registered by the .reg file. The handler
    // then asks ReelReady for the file, which is how the page knows the link really launched.
    var started = Date.now();
    toast(action === 'play' ? '正在打开播放器…' : '正在打开文件夹…');
    window.location.href = 'reelready://' + action + '/' + encodeURIComponent(movieId);
    (function poll() {
      fetch('/playback/movies/' + encodeURIComponent(movieId) + '/handler', {cache: 'no-store'})
        .then(function (response) { return response.json(); })
        .then(function (data) {
          var elapsed = (Date.now() - started) / 1000;
          if (data.age !== null && data.age <= elapsed + 0.5) return;
          // A browser prompt may still be waiting for the user, so give it time.
          if (elapsed < 20) setTimeout(poll, 700);
          else toast(NOT_LAUNCHED, 'warn');
        })
        .catch(function () {});
    })();
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
