// 다크 모드 스위치. 기본은 밝은 화면이고, 켜면 html 에 data-theme="dark" 를 단다. 고른 것은 이 브라우저에만
// 남는다(localStorage). <head> 에서 바로 돌아서 첫 화면부터 고른 모드로 그린다. 바꾸면 themechange 를 알려
// 그래프(plots.js)가 새 색으로 다시 그린다.
(function () {
  var KEY = "emcp-dark";
  function read() {
    try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; }
  }
  function apply(dark) {
    if (dark) document.documentElement.dataset.theme = "dark";
    else delete document.documentElement.dataset.theme;
  }
  apply(read());
  document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.getElementById("dark-mode");
    if (!toggle) return;
    toggle.checked = read();
    toggle.addEventListener("change", function () {
      try { localStorage.setItem(KEY, toggle.checked ? "1" : "0"); } catch (e) { /* 저장을 막아 둔 브라우저는 이번에만 바뀐다 */ }
      apply(toggle.checked);
      window.dispatchEvent(new Event("themechange"));
    });
  });
})();
