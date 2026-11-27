/* Matting-Flash 桌面壳加载页桥接。
 * - Tauri 环境：直接复用 withGlobalTauri 暴露的 window.__TAURI__（core.invoke / event.listen）。
 * - 浏览器直开 index.html：全部降级为 no-op，页面显示「请通过桌面应用启动」。 */
(function () {
  "use strict";
  var tauri = window.__TAURI__;
  var isTauri = !!(tauri && tauri.core && typeof tauri.core.invoke === "function");

  window.__MATTING_DESKTOP__ = {
    isTauri: isTauri,
    invoke: function (command, args) {
      if (!isTauri) return Promise.reject(new Error("浏览器模式无桌面桥"));
      return tauri.core.invoke(command, args);
    },
    listen: function (event, handler) {
      if (!isTauri) return Promise.resolve(function () {});
      return tauri.event.listen(event, handler);
    },
  };

  // 自更新第 7 步：Rust 在启动安装器前发出该事件，应用随即有序退出；
  // 安装器带 /RELAUNCH 会在装完后自动把应用拉起，无需用户手动点击图标。
  window.__MATTING_DESKTOP__.listen("update-installing", function (r) {
    var body = document.getElementById("mfdu-body");
    var actions = document.getElementById("mfdu-actions");
    var message = (r && r.payload && r.payload.message) || (r && r.message) || "应用即将关闭，安装程序将自动完成更新并重新启动。";
    if (body) {
      body.textContent = message;
      body.style.color = "#15803d";
      body.style.fontWeight = "600";
    }
    if (actions) actions.innerHTML = "";
  });
})();
