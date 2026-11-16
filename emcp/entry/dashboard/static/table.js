// 보고 표: 깨어 있던 ms 제목(관리자 페이지에서는 라디오 ms 제목도)을 누르면 측정값 칸과 시간 칸(라디오 ms, 단계)을
// 서로 바꿔 보인다. 버튼은 aria-controls 로 자기 표를 가리킨다. 관리자 페이지에는 보드마다 표가 있어 표마다 따로
// 바뀌고, 한 표를 가리키는 버튼이 둘이면 둘 다 같은 상태를 보인다.
(function () {
  var buttons = document.querySelectorAll("button.more[aria-controls]");
  Array.prototype.forEach.call(buttons, function (button) {
    var table = document.getElementById(button.getAttribute("aria-controls"));
    if (!table) return;
    button.addEventListener("click", function () {
      var open = table.classList.toggle("open");
      var same = document.querySelectorAll('button.more[aria-controls="' + table.id + '"]');
      Array.prototype.forEach.call(same, function (b) {
        b.setAttribute("aria-expanded", open ? "true" : "false");
        b.title = open ? "측정값 보기" : "단계별 시간 보기";
      });
    });
  });
})();
