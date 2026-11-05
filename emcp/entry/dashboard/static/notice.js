// 안내(?notice=)와 QR 로 채운 코드(?code=)는 한 번만 보이게, 화면에 그린 뒤 주소창에서 지운다. 새로고침하면
// 깨끗한 /dashboard 가 열린다. 보드 고르기(?board=)는 남긴다.
(function () {
  var url = new URL(location.href);
  if (!url.searchParams.has("notice") && !url.searchParams.has("code")) return;
  url.searchParams.delete("notice");
  url.searchParams.delete("code");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
})();
