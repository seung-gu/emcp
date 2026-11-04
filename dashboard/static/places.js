// 위치 바꾸기: 도시를 검색해 후보 하나를 고르면 저장 칸이 채워진다. 지명은 사람이 쓴 글자이니 textContent 로만 넣는다.
(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    var search = e.target.closest(".place-search");
    if (!search) return;
    e.preventDefault();
    var pick = search.nextElementSibling, list = pick.querySelector(".places"), save = pick.querySelector("button");
    function field(name) { return pick.querySelector('input[name="' + name + '"]'); }
    function note(text) {
      var li = document.createElement("li");
      li.className = "dim";
      li.textContent = text;
      list.appendChild(li);
    }
    list.innerHTML = "";
    save.disabled = true;
    pick.hidden = false;
    note("검색 중…");
    fetch("/dashboard/places?q=" + encodeURIComponent(search.querySelector('input[name="q"]').value.trim()),
          { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (places) {
        list.innerHTML = "";
        if (!places.length) return note("찾지 못했습니다. 로마자로 다시 검색해 보세요 (예: Gwangju).");
        places.forEach(function (p) {
          var li = document.createElement("li"), label = document.createElement("label");
          var radio = document.createElement("input");
          radio.type = "radio";
          radio.name = "place";
          radio.addEventListener("change", function () {
            field("name").value = p.name;
            field("lat").value = p.lat;
            field("lon").value = p.lon;
            save.disabled = false;
          });
          label.appendChild(radio);
          label.appendChild(document.createTextNode(" " + p.name + (p.where ? " · " + p.where : "")));
          li.appendChild(label);
          list.appendChild(li);
        });
      })
      .catch(function () { list.innerHTML = ""; note("검색에 실패했습니다. 잠시 뒤에 다시 해 주세요."); });
  });
})();
