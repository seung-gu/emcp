// 최근 보고 표: 깨어 있던 ms 제목을 누르면 측정값 칸과 시간 칸(라디오 ms, 단계)을 서로 바꿔 보인다.
(function () {
  var button = document.getElementById("steps-toggle"), table = document.getElementById("reports");
  if (!button || !table) return;
  button.addEventListener("click", function () {
    var open = table.classList.toggle("open");
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.title = open ? "측정값 보기" : "단계별 시간 보기";
  });
})();
