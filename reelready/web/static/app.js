(function () {
  "use strict";

  var saveQueue = [], saving = false;
  function saveStatus(form, message, failed) {
    var status = form.querySelector('[data-save-status]');
    status.textContent = message;
    status.classList.toggle('error', !!failed);
    form.querySelectorAll('[data-settings-test]').forEach(function (button) {
      var missingTarget = button.matches('[data-backup-run]') && !form.querySelector('[name="github_enabled"]:checked, [name="webdav_enabled"]:checked');
      button.disabled = message !== '已保存' || !!missingTarget;
    });
  }
  function queueSave(form, delay) {
    form.dataset.unsaved = 'true';
    saveStatus(form, '等待保存…');
    clearTimeout(form.saveTimer);
    form.saveTimer = setTimeout(function () {
      if (!saveQueue.includes(form)) saveQueue.push(form);
      drainSaves();
    }, delay || 0);
  }
  async function drainSaves() {
    if (saving || !saveQueue.length) return;
    var form = saveQueue.shift();
    if (!form.checkValidity() || Array.from(form.querySelectorAll('input[type="number"]:not([data-optional-number])')).some(function (input) { return input.value === ''; })) {
      saveStatus(form, '请填写有效数值', true);
      drainSaves(); return;
    }
    saving = true;
    var data = new FormData(form), secrets = [], editingSecret = false;
    form.querySelectorAll('[data-secret-mask]').forEach(function (input) {
      // Never persist a half-typed password when another field changes.
      if (document.activeElement === input) { data.set(input.name, '••••••••••••'); editingSecret = true; }
      else if (input.value && input.value !== '••••••••••••') secrets.push([input, input.value]);
    });
    form.dataset.unsaved = editingSecret ? 'true' : 'false';
    saveStatus(form, '保存中…');
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 12000);
    try {
      var response = await fetch(form.dataset.autosave, {method: 'POST', body: data, headers: {'HX-Request': 'true'}, signal: controller.signal});
      var result = JSON.parse(response.headers.get('HX-Trigger') || '{}').toast;
      if (!response.ok || !result || result.level === 'error' || result.message !== '设置已保存') throw new Error(result ? result.message : '无法保存，请重试');
      secrets.forEach(function (entry) {
        var input = entry[0];
        input.dataset.secretMask = 'true';
        if (input.value === entry[1] && document.activeElement !== input) input.value = '••••••••••••';
        input.closest('.field').querySelector('[data-secret-status]').textContent = '已填写 · 输入新值可替换，留空保持不变';
      });
      saveStatus(form, form.dataset.unsaved === 'true' ? '等待保存…' : '已保存');
    } catch (error) {
      form.dataset.unsaved = 'true';
      saveStatus(form, error.message + ' · 点击重试', true);
    } finally { clearTimeout(timeout); saving = false; drainSaves(); }
  }
  document.addEventListener('input', function (evt) {
    var form = evt.target.closest('form[data-autosave]');
    if (form && !evt.target.matches('[data-secret-mask]')) queueSave(form, 900);
    else if (form) { form.dataset.unsaved = 'true'; saveStatus(form, '输入完成后离开此栏自动保存'); }
  });
  document.addEventListener('change', function (evt) {
    var form = evt.target.closest('form[data-autosave]');
    if (form && !evt.target.matches('[data-secret-mask]')) queueSave(form, 100);
  });
  document.addEventListener('focusout', function (evt) {
    var input = evt.target, form = input.closest('form[data-autosave]');
    if (form && input.matches('[data-secret-mask]') && ((input.value && input.value !== '••••••••••••') || form.dataset.unsaved === 'true')) queueSave(form, 100);
  });
  document.addEventListener('submit', function (evt) {
    if (evt.target.matches('form[data-autosave]')) { evt.preventDefault(); queueSave(evt.target, 0); }
  });
  document.addEventListener('click', function (evt) {
    var status = evt.target.closest('[data-save-status]');
    if (status && status.classList.contains('error')) queueSave(status.closest('form'), 0);
  });

  function syncCloudBackup() {
    var form = document.querySelector('[data-cloud-backup]');
    if (!form) return;
    form.querySelectorAll('[data-cloud-toggle]').forEach(function (input) {
      var card = input.closest('[data-cloud-target]');
      card.querySelector('.cloud-target-body').hidden = !input.checked;
      card.querySelector('[data-cloud-target-status]').textContent = input.checked ? '已启用' : '未启用';
      input.setAttribute('aria-expanded', String(input.checked));
    });
    form.querySelector('[name="interval_hours"]').disabled = !form.querySelector('[name="auto_enabled"]').checked;
    form.querySelector('[data-cloud-full-warning]').hidden = form.querySelector('[name="scope"]').value !== 'full';
    var status = form.querySelector('[data-save-status]');
    form.querySelector('[data-backup-run]').disabled = status.textContent !== '已保存' || !form.querySelector('[name="github_enabled"]:checked, [name="webdav_enabled"]:checked');
  }
  document.addEventListener('change', function (evt) { if (evt.target.closest('[data-cloud-backup]')) syncCloudBackup(); });
  syncCloudBackup();

  function selectSettingsGroup(target, scroll) {
    var links = Array.from(document.querySelectorAll('[data-settings-groups]'));
    if (!links.length) return;
    var selected = links.find(function (link) { return link.dataset.settingsGroups.split(' ').includes(target); }) || links[0];
    var keys = selected.dataset.settingsGroups.split(' ');
    document.querySelectorAll('[data-settings-section]').forEach(function (section) { section.hidden = !keys.includes(section.id); });
    links.forEach(function (link) {
      if (link === selected) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    });
    if (scroll) window.scrollTo(0, 0);
  }
  document.addEventListener('click', function (evt) {
    var link = evt.target.closest('[data-settings-groups]');
    if (!link || evt.ctrlKey || evt.metaKey || evt.shiftKey || evt.altKey) return;
    evt.preventDefault();
    history.pushState(null, '', link.getAttribute('href'));
    selectSettingsGroup(location.hash.slice(1), true);
  });
  window.addEventListener('hashchange', function () { selectSettingsGroup(location.hash.slice(1), false); });
  selectSettingsGroup(location.hash.slice(1), false);

  var galleryCaptionsHidden = false, galleryDockAutoHide = false, galleryDockRevealed = false;
  var galleryMenus = {category: false, more: false}, galleryCategory = 'monitoring';
  try {
    galleryCaptionsHidden = localStorage.getItem('reelready.gallery.captionsHidden') === 'true';
    galleryDockAutoHide = localStorage.getItem('reelready.gallery.dockAutoHide') === 'true';
  } catch (_) { /* Browser preferences may be unavailable in private mode. */ }
  try {
    galleryMenus = JSON.parse(sessionStorage.getItem('reelready.gallery.menus') || '{}') || {};
    galleryCategory = sessionStorage.getItem('reelready.gallery.category') || 'monitoring';
    galleryDockRevealed = sessionStorage.getItem('reelready.gallery.revealed') === 'true';
  } catch (_) {}
  function syncGalleryPreferences() {
    document.body.classList.toggle('gallery-captions-hidden', galleryCaptionsHidden);
    document.body.classList.toggle('gallery-dock-autohide', galleryDockAutoHide);
    document.body.classList.toggle('gallery-dock-revealed', galleryDockRevealed);
    var dock = document.querySelector('.gallery-dock');
    if (dock) dock.inert = galleryDockAutoHide && !galleryDockRevealed;
    if (dock) { try { sessionStorage.setItem('reelready.gallery.revealed', String(galleryDockRevealed)); } catch (_) {} }
    document.querySelectorAll('[data-gallery-persistent]').forEach(function (panel) { panel.open = !!galleryMenus[panel.dataset.galleryPersistent]; });
    document.body.classList.toggle('gallery-both-open', !!galleryMenus.category && !!galleryMenus.more);
    if (dock) {
      if (dock.dataset.movieTab) {
        galleryCategory = dock.dataset.movieTab;
        try { sessionStorage.setItem('reelready.gallery.category', galleryCategory); } catch (_) {}
      }
      var current = dock.querySelector('[data-gallery-category="' + galleryCategory + '"]') || dock.querySelector('[data-gallery-category]');
      if (current) {
        dock.querySelector('[data-gallery-category-label]').textContent = current.querySelector('span').textContent;
        dock.querySelector('[data-gallery-category-count]').textContent = current.querySelector('.count').textContent;
        dock.querySelectorAll('[data-gallery-category]').forEach(function (link) {
          link.classList.toggle('active', link === current);
          if (link === current) link.setAttribute('aria-current', 'page');
          else link.removeAttribute('aria-current');
        });
      }
    }
    var captions = document.querySelector('[data-gallery-captions]');
    if (captions) {
      captions.setAttribute('aria-pressed', String(galleryCaptionsHidden));
      captions.title = galleryCaptionsHidden ? '显示片名和评分' : '隐藏片名和评分';
      captions.setAttribute('aria-label', captions.title);
    }
    var collapse = document.querySelector('[data-gallery-collapse]');
    if (collapse) {
      collapse.setAttribute('aria-pressed', String(galleryDockAutoHide));
      collapse.title = galleryDockAutoHide ? '固定显示工具栏' : '自动收起工具栏，移到页面底部展开';
      collapse.setAttribute('aria-label', galleryDockAutoHide ? '固定显示工具栏' : '自动收起工具栏');
    }
    var reveal = document.querySelector('[data-gallery-reveal]');
    if (reveal) reveal.setAttribute('aria-expanded', String(!galleryDockAutoHide || galleryDockRevealed));
  }
  function hideGalleryDock() {
    var shell = document.querySelector('.gallery-dock-shell');
    if (!shell || !galleryDockAutoHide || shell.matches(':hover') || shell.querySelector('[data-gallery-panel][open], input:focus, select:focus')) return;
    galleryDockRevealed = false;
    syncGalleryPreferences();
  }
  document.addEventListener('click', function (evt) {
    if (evt.target.closest('[data-gallery-captions]')) {
      galleryCaptionsHidden = !galleryCaptionsHidden;
      try { localStorage.setItem('reelready.gallery.captionsHidden', String(galleryCaptionsHidden)); } catch (_) {}
      syncGalleryPreferences();
    }
    if (evt.target.closest('[data-gallery-collapse]')) {
      galleryDockAutoHide = !galleryDockAutoHide;
      galleryDockRevealed = false;
      document.querySelectorAll('[data-gallery-panel][open]:not([data-gallery-persistent])').forEach(function (panel) { panel.open = false; });
      if (galleryDockAutoHide) evt.target.closest('button').blur();
      try { localStorage.setItem('reelready.gallery.dockAutoHide', String(galleryDockAutoHide)); } catch (_) {}
      syncGalleryPreferences();
    }
    if (evt.target.closest('[data-gallery-reveal]')) {
      galleryDockRevealed = true;
      syncGalleryPreferences();
      document.querySelector('.gallery-category > summary').focus();
    }
  });
  document.addEventListener('pointerover', function (evt) {
    if (evt.target.closest('[data-gallery-reveal]')) {
      galleryDockRevealed = true;
      syncGalleryPreferences();
    }
  });
  document.addEventListener('pointerout', function (evt) {
    var shell = evt.target.closest('.gallery-dock-shell');
    if (shell && !shell.contains(evt.relatedTarget)) hideGalleryDock();
  });
  document.addEventListener('focusin', function (evt) {
    if (evt.target.matches('[data-gallery-reveal]')) {
      galleryDockRevealed = true;
      syncGalleryPreferences();
    }
  });
  document.addEventListener('focusout', function (evt) {
    var shell = evt.target.closest('.gallery-dock-shell');
    if (shell && !shell.contains(evt.relatedTarget)) hideGalleryDock();
  });
  syncGalleryPreferences();

  var jobsExpanded = false;
  function restoreJobsPanel() {
    var panel = document.getElementById('jobs');
    if (panel && panel.matches('details')) panel.open = jobsExpanded;
    syncGalleryJobStatus();
  }
  document.addEventListener('toggle', function (evt) {
    if (evt.target.id !== 'jobs' || !evt.target.isConnected) return;
    jobsExpanded = evt.target.open;
  }, true);
  document.addEventListener('htmx:afterSwap', restoreJobsPanel);
  document.addEventListener('htmx:beforeSwap', function (evt) {
    if (evt.detail.target && evt.detail.target.id === 'jobs') jobsExpanded = evt.detail.target.open;
  });
  restoreJobsPanel();
  document.addEventListener('click', function (evt) {
    document.querySelectorAll('.site-row-menu[open]').forEach(function (menu) {
      if (!menu.contains(evt.target) || evt.target.closest('button')) menu.open = false;
    });
    var add = document.querySelector('.movie-add-panel[open]');
    if (add && !add.contains(evt.target)) add.open = false;
    var jobs = document.querySelector('#jobs[open]');
    if (jobs && !jobs.contains(evt.target) && !evt.target.closest('[data-gallery-jobs]')) jobs.open = false;
    document.querySelectorAll('[data-gallery-panel][open]:not([data-gallery-persistent])').forEach(function (panel) {
      if (!panel.contains(evt.target)) panel.open = false;
    });
    if (evt.target.closest('[data-gallery-jobs]')) {
      var panel = document.getElementById('jobs');
      if (panel) { panel.open = !panel.open; jobsExpanded = panel.open; }
      document.querySelectorAll('[data-gallery-panel][open]:not([data-gallery-persistent])').forEach(function (el) { el.open = false; });
    }
    if (evt.target.closest('[data-gallery-jobs-close]')) {
      var panel = document.getElementById('jobs');
      if (panel) { panel.open = false; jobsExpanded = false; }
      var more = document.querySelector('.gallery-more > summary');
      if (more) more.focus();
    }
  });
  document.addEventListener('keydown', function (evt) {
    var add = document.querySelector('.movie-add-panel[open]');
    if (evt.key === 'Escape' && add) { add.open = false; add.querySelector('summary').focus(); }
    var jobs = document.querySelector('#jobs[open]');
    if (evt.key === 'Escape' && jobs) {
      jobs.open = false; jobsExpanded = false;
      (document.querySelector('.gallery-more > summary') || jobs.querySelector('summary')).focus();
    }
    if (evt.key === 'Escape') document.querySelectorAll('[data-gallery-panel][open]:not([data-gallery-persistent])').forEach(function (panel) {
      panel.open = false; panel.querySelector('summary').focus();
    });
  });
  function syncGalleryJobStatus() {
    var errors = document.querySelectorAll('#jobs .job-grid .job-dot.error').length;
    var badge = document.querySelector('.gallery-alert');
    if (badge) badge.hidden = !errors;
    var count = document.querySelector('[data-gallery-error-count]');
    if (count) count.textContent = errors || '';
    var more = document.querySelector('.gallery-more > summary');
    if (more) more.setAttribute('aria-label', errors ? '更多功能，' + errors + ' 项任务异常' : '更多功能');
  }
  document.addEventListener('toggle', function (evt) {
    var panel = evt.target;
    if (!panel.matches('[data-gallery-panel]') || !panel.isConnected) return;
    if (panel.dataset.galleryPersistent) {
      galleryMenus[panel.dataset.galleryPersistent] = panel.open;
      try { sessionStorage.setItem('reelready.gallery.menus', JSON.stringify(galleryMenus)); } catch (_) {}
      document.body.classList.toggle('gallery-both-open', !!galleryMenus.category && !!galleryMenus.more);
      if (!panel.open) hideGalleryDock();
      return;
    }
    if (!panel.open) { hideGalleryDock(); return; }
    document.querySelectorAll('[data-gallery-panel][open]:not([data-gallery-persistent])').forEach(function (other) { if (other !== panel) other.open = false; });
    var field = panel.querySelector('input:not([type="hidden"])');
    if (field) field.focus();
  }, true);
  var disclosureState = {};
  document.addEventListener('toggle', function (evt) {
    if (evt.target.matches('[data-disclosure-state]')) disclosureState[evt.target.dataset.disclosureState] = evt.target.open;
  }, true);
  document.addEventListener('htmx:afterSwap', function () {
    document.querySelectorAll('[data-disclosure-state]').forEach(function (panel) {
      if (Object.prototype.hasOwnProperty.call(disclosureState, panel.dataset.disclosureState)) panel.open = disclosureState[panel.dataset.disclosureState];
    });
  });
  window.addEventListener('beforeunload', function (evt) {
    if (saving || document.querySelector('form[data-unsaved="true"]')) { evt.preventDefault(); evt.returnValue = ''; }
  });

  var draggedRegion = null;
  document.addEventListener('dragstart', function (evt) {
    if (!evt.target.matches('.region-drag')) return;
    draggedRegion = evt.target.closest('[data-choice]');
    evt.dataTransfer.effectAllowed = 'move';
    evt.dataTransfer.setData('text/plain', draggedRegion.dataset.choice);
    draggedRegion.classList.add('dragging');
  });
  document.addEventListener('dragover', function (evt) {
    var row = evt.target.closest('.region-options [data-choice]');
    if (draggedRegion && row && row.parentNode === draggedRegion.parentNode) { evt.preventDefault(); evt.dataTransfer.dropEffect = 'move'; }
  });
  document.addEventListener('drop', function (evt) {
    var row = evt.target.closest('.region-options [data-choice]');
    if (!draggedRegion || !row || row.parentNode !== draggedRegion.parentNode) return;
    evt.preventDefault();
    if (row !== draggedRegion) {
      row.parentNode.insertBefore(draggedRegion, row);
      syncSettingChoices(row.closest('.field'));
      queueSave(row.closest('form'), 100);
    }
  });
  document.addEventListener('dragend', function () {
    if (draggedRegion) draggedRegion.classList.remove('dragging');
    draggedRegion = null;
  });

  function syncSettingChoices(field) {
    var rows = Array.from(field.querySelectorAll('[data-choice]'));
    var order = field.querySelector('[data-choice-order]');
    if (order) order.value = rows.map(function (row) { return row.dataset.choice; }).join(',');
    field.querySelector('[data-choice-value]').value = rows.filter(function (row) { return row.querySelector('[data-setting-choice]').checked; }).map(function (row) { return row.dataset.choice; }).join(',');
    rows.forEach(function (row, index) {
      var up = row.querySelector('[data-order="up"]');
      var down = row.querySelector('[data-order="down"]');
      if (up) up.disabled = index === 0;
      if (down) down.disabled = index === rows.length - 1;
    });
  }
  document.querySelectorAll('[data-choice-value]').forEach(function (input) { syncSettingChoices(input.closest('.field')); });
  document.addEventListener('change', function (evt) {
    if (evt.target.matches('[data-setting-choice]')) syncSettingChoices(evt.target.closest('.field'));
  });
  document.addEventListener('click', function (evt) {
    var button = evt.target.closest('[data-order]');
    if (!button) return;
    var row = button.closest('[data-choice]');
    if (button.dataset.order === 'up' && row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
    if (button.dataset.order === 'down' && row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row);
    syncSettingChoices(row.closest('.field'));
    if (row.closest('form[data-autosave]')) queueSave(row.closest('form'), 100);
  });
  document.addEventListener('focusin', function (evt) {
    if (evt.target.matches('[data-secret-mask="true"]') && evt.target.value === '••••••••••••') evt.target.value = '';
  });
  document.addEventListener('focusout', function (evt) {
    if (evt.target.matches('[data-secret-mask="true"]') && !evt.target.value) evt.target.value = '••••••••••••';
  });
  document.body.addEventListener('htmx:beforeRequest', function (evt) {
    var form = evt.detail.elt;
    if (!form.matches('form')) return;
    form.querySelectorAll('[data-secret-mask]').forEach(function (input) {
      input.dataset.secretPending = input.value && input.value !== '••••••••••••' ? 'true' : 'false';
    });
  });
  document.body.addEventListener('htmx:afterRequest', function (evt) {
    var form = evt.detail.elt;
    if (!form.matches('form') || !evt.detail.successful) return;
    var trigger = evt.detail.xhr.getResponseHeader('HX-Trigger') || '';
    var message;
    try { message = JSON.parse(trigger).toast.message; } catch (_) { return; }
    if (message !== '设置已保存') return;
    form.querySelectorAll('[data-secret-pending="true"]').forEach(function (input) {
      input.dataset.secretMask = 'true';
      input.dataset.secretPending = 'false';
      input.value = '••••••••••••';
      input.closest('.field').querySelector('[data-secret-status]').textContent = '已填写 · 输入新值可替换，留空保持不变';
    });
  });

  var confirmDialog = document.getElementById("confirm-dialog");
  var confirmRequest = null;
  document.body.addEventListener("htmx:confirm", function (evt) {
    if (!evt.detail.question || !confirmDialog) return;
    evt.preventDefault();
    if (confirmDialog.open) return;
    var source = evt.detail.elt;
    var destructive = source.classList.contains("btn-danger");
    var accept = document.getElementById("confirm-accept");
    document.getElementById("confirm-title").textContent = destructive ? "确认删除" : "确认操作";
    document.getElementById("confirm-message").textContent = evt.detail.question;
    document.getElementById("confirm-description").textContent = source.dataset.confirmDescription || (destructive ? "此操作无法撤销，请确认后继续。" : "确认后将执行此操作。稍后也可再次操作。");
    document.getElementById("confirm-symbol").hidden = !destructive;
    accept.textContent = destructive ? "确认删除" : "确认";
    accept.className = "btn " + (destructive ? "confirm-danger" : "btn-primary");
    confirmRequest = { source: source, issue: evt.detail.issueRequest };
    confirmDialog.returnValue = "";
    confirmDialog.showModal();
  });
  if (confirmDialog) {
    confirmDialog.addEventListener("close", function () {
      var request = confirmRequest;
      confirmRequest = null;
      if (request && confirmDialog.returnValue === "accept" && request.source.isConnected) request.issue(true);
    });
    confirmDialog.addEventListener("click", function (evt) {
      if (evt.target !== confirmDialog) return;
      var rect = confirmDialog.getBoundingClientRect();
      if (evt.clientX < rect.left || evt.clientX > rect.right || evt.clientY < rect.top || evt.clientY > rect.bottom) confirmDialog.close("cancel");
    });
  }

  function showToast(message, level) {
    var box = document.getElementById("toasts");
    if (!box || !message) return;
    var el = document.createElement("div");
    el.className = "toast " + (level || "ok");
    var icon = document.createElement("span");
    icon.className = "icon";
    var text = document.createElement("span");
    text.textContent = message;
    el.appendChild(icon);
    el.appendChild(text);
    box.appendChild(el);
    setTimeout(function () {
      el.classList.add("leaving");
      setTimeout(function () { el.remove(); }, 300);
    }, level === "error" ? 6000 : 3500);
  }

  window.showToast = showToast;

  document.body.addEventListener("toast", function (evt) {
    showToast(evt.detail && evt.detail.message, evt.detail && evt.detail.level);
  });

  document.body.addEventListener("htmx:responseError", function (evt) {
    var status = evt.detail.xhr ? evt.detail.xhr.status : "";
    showToast("请求失败 " + status, "error");
  });

  document.body.addEventListener("htmx:sendError", function () {
    showToast("无法连接到 ReelReady", "error");
  });

  // Copy buttons: <button data-copy="#input-id">
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest("[data-copy]");
    if (!btn) return;
    var input = document.querySelector(btn.getAttribute("data-copy"));
    if (!input) return;
    var value = input.value;
    var done = function () { showToast("已复制"); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(value).then(done);
    } else {
      // Plain http on a LAN address is not a secure context; fall back to execCommand.
      input.select();
      document.execCommand("copy");
      done();
    }
  });

  // Reveal buttons: <button data-reveal="#input-id"> toggles a password input.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest("[data-reveal]");
    if (!btn) return;
    var input = document.querySelector(btn.getAttribute("data-reveal"));
    if (input) input.type = input.type === "password" ? "text" : "password";
  });

  // Toggle buttons: <button data-toggle="#element-id"> shows / hides an element.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest("[data-toggle]");
    if (!btn) return;
    var el = document.querySelector(btn.getAttribute("data-toggle"));
    if (el) el.hidden = !el.hidden;
  });

  // Email provider: SMTP host/port/security only matter for the "custom" provider.
  function syncEmailProvider() {
    var select = document.querySelector('select[data-email-provider]');
    if (!select) return;
    var custom = select.value === "custom";
    document.querySelectorAll("[data-custom-only]").forEach(function (el) {
      el.style.display = custom ? "" : "none";
    });
  }

  document.addEventListener("change", function (evt) {
    if (evt.target.matches("select[data-email-provider]")) syncEmailProvider();
  });

  syncEmailProvider();

  document.addEventListener("click", function (evt) {
    var manual = evt.target.closest("[data-manual-site]");
    if (!manual) return;
    var panel = document.querySelector("#manual-site");
    if (!panel) return;
    var discovery = panel.closest('.discovery-details');
    if (discovery) discovery.open = true;
    panel.open = true;
    var kind = panel.querySelector("#site-kind");
    if (!panel.querySelector("#site-name").value && !panel.querySelector("#site-url").value && !panel.querySelector("#site-api-key").value) {
      kind.value = "nexusphp";
      kind.dispatchEvent(new Event("change", { bubbles: true }));
    }
    panel.querySelector("#site-name").focus();
  });

  document.addEventListener("click", function (evt) {
    var button = evt.target.closest("[data-configure-api]");
    if (!button) return;
    var panel = document.querySelector("#manual-site");
    if (!panel) return;
    var discovery = panel.closest('.discovery-details');
    if (discovery) discovery.open = true;
    panel.open = true;
    var kind = panel.querySelector("#site-kind");
    kind.value = button.dataset.configureApi;
    kind.dispatchEvent(new Event("change", { bubbles: true }));
    panel.querySelector("#site-name").value = button.dataset.siteName;
    panel.querySelector("#site-url").value = button.dataset.siteUrl;
    panel.querySelector("#site-api-key").focus();
  });

  function filterSiteChoices() {
    var input = document.querySelector("[data-site-search]");
    if (!input) return;
    var query = input.value.trim().toLowerCase();
    var matches = 0;
    document.querySelectorAll("[data-site-choice]").forEach(function (choice) {
      choice.hidden = !choice.dataset.searchText.toLowerCase().includes(query);
      if (!choice.hidden) matches++;
    });
    var empty = document.querySelector(".site-search-empty");
    if (empty) empty.hidden = matches > 0;
  }
  document.addEventListener("input", function (evt) {
    if (evt.target.matches("[data-site-search]")) filterSiteChoices();
  });
  document.addEventListener("htmx:afterSwap", filterSiteChoices);

  function logoFallback(img) {
    img.hidden = true;
    if (img.nextElementSibling) img.nextElementSibling.hidden = false;
  }
  document.addEventListener("error", function (evt) {
    if (evt.target.matches && evt.target.matches("[data-site-logo]")) logoFallback(evt.target);
  }, true);
  document.querySelectorAll("[data-site-logo]").forEach(function (img) {
    if (img.complete && !img.naturalWidth) logoFallback(img);
  });

  // Remember each library filter separately, without writing storage on every scroll.
  var moviePositions = {}, moviePositionKey = null;
  var listingDock = document.querySelector('.gallery-view .gallery-dock[data-movie-tab]');
  if (listingDock) {
    var listingParams = new URLSearchParams(location.search);
    moviePositionKey = JSON.stringify([listingDock.dataset.movieTab, listingParams.get('availability') || 'all',
      listingParams.get('sort') || 'newest', listingParams.get('q') || '']);
    try { moviePositions = JSON.parse(sessionStorage.getItem('reelready.gallery.positions') || '{}') || {}; } catch (_) {}
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  }
  function saveMoviePosition() {
    if (!moviePositionKey) return;
    moviePositions[moviePositionKey] = {y: window.scrollY, at: Date.now()};
    var keys = Object.keys(moviePositions).sort(function (a, b) { return moviePositions[b].at - moviePositions[a].at; });
    keys.slice(30).forEach(function (key) { delete moviePositions[key]; });
    try { sessionStorage.setItem('reelready.gallery.positions', JSON.stringify(moviePositions)); } catch (_) {}
  }
  function restoreMoviePosition() {
    if (!moviePositionKey) return;
    var saved = moviePositions[moviePositionKey];
    window.scrollTo(0, saved && Number.isFinite(saved.y) ? saved.y : 0);
  }
  restoreMoviePosition();
  window.addEventListener('pageshow', restoreMoviePosition);
  window.addEventListener('pagehide', saveMoviePosition);
  document.addEventListener('visibilitychange', function () { if (document.hidden) saveMoviePosition(); });

  // Poll a small local revision, fetching HTML only when data actually changed.
  // One timer per page; hidden tabs make no polling requests.
  var movieTimer = null, movieRequest = null;
  function scheduleMovieSync(delay) {
    clearTimeout(movieTimer);
    if (!document.hidden && document.querySelector('[data-live-movies]')) movieTimer = setTimeout(syncMovies, delay);
  }
  async function syncMovies() {
    var view = document.querySelector('[data-live-movies]');
    if (!view || document.hidden || movieRequest) return;
    var delay = view.dataset.pending === 'true' ? 2000 : 15000;
    if (document.querySelector('dialog[open]') || view.querySelector('input:focus, textarea:focus, select:focus, [data-gallery-panel][open]:not([data-gallery-persistent])')) {
      scheduleMovieSync(delay);
      return;
    }
    var controller = new AbortController();
    movieRequest = controller;
    var timeout = setTimeout(function () { controller.abort(); }, 12000);
    try {
      var revisionUrl = '/movies/revision' + (view.dataset.movieId ? '?movie_id=' + encodeURIComponent(view.dataset.movieId) : '');
      var response = await fetch(revisionUrl, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('revision unavailable');
      var state = await response.json();
      var pendingChanged = !!view.dataset.movieId && (view.dataset.pending === 'true') !== state.pending;
      if (view.dataset.revision !== state.revision || pendingChanged) {
        response = await fetch(location.href, {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw new Error('view unavailable');
        var html = new DOMParser().parseFromString(await response.text(), 'text/html');
        var replacement = html.querySelector('[data-live-movies]');
        if (replacement && view.isConnected && !document.hidden) {
          // Keep expanded information and the current scroll position.
          var openDetails = Array.from(view.querySelectorAll('details')).map(function (el) { return el.open; });
          replacement.querySelectorAll('details').forEach(function (el, index) { el.open = !!openDetails[index]; });
          var scrollY = window.scrollY;
          view.replaceWith(replacement);
          if (window.htmx) htmx.process(replacement);
          syncGalleryJobStatus();
          syncGalleryPreferences();
          window.scrollTo(0, scrollY);
          view = replacement;
        }
      }
      delay = view.dataset.pending === 'true' ? 2000 : 15000;
    } catch (_) { delay = 30000; }
    finally {
      clearTimeout(timeout);
      movieRequest = null;
      scheduleMovieSync(delay);
    }
  }
  window.addEventListener('pageshow', function () { scheduleMovieSync(0); });
  window.addEventListener('pagehide', function () {
    clearTimeout(movieTimer);
    if (movieRequest) movieRequest.abort();
  });
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      clearTimeout(movieTimer);
      if (movieRequest) movieRequest.abort();
    } else scheduleMovieSync(0);
  });
  scheduleMovieSync(document.querySelector('[data-pending="true"]') ? 2000 : 15000);
})();
