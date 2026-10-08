(function () {
  "use strict";

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
})();
