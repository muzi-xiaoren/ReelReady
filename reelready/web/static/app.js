(function () {
  "use strict";

  var saveQueue = [], saving = false;
  function saveStatus(form, message, failed) {
    var status = form.querySelector('[data-save-status]');
    status.textContent = message;
    status.classList.toggle('error', !!failed);
    form.querySelectorAll('[data-settings-test]').forEach(function (button) { button.disabled = message !== '已保存'; });
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
    if (!form.checkValidity() || Array.from(form.querySelectorAll('input[type="number"]')).some(function (input) { return input.value === ''; })) {
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
})();
