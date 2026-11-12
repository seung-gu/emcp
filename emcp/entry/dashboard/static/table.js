// 최근 보고 표: 깨어 있던 ms 제목을 누르면 라디오 ms와 단계별 칸을 펴고 접는다.
(function () {
  var button = document.getElementById("steps-toggle"), table = document.getElementById("reports");
  if (!button || !table) return;
  button.addEventListener("click", function () {
    var open = table.classList.toggle("open");
    button.setAttribute("aria-expanded", open ? "true" : "false");
  });
})();
