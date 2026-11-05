(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("plot-data").textContent);
  var boxes = Array.prototype.slice.call(document.querySelectorAll(".plot"));
  var toolbar = document.getElementById("toolbar");
  var rangeEl = document.getElementById("range");
  var tip = document.getElementById("tip");

  function empty(el, msg) { el.innerHTML = ""; var p = document.createElement("p");
    p.className = "empty"; p.textContent = msg; el.appendChild(p); }
  if (typeof uPlot === "undefined") {
    boxes.forEach(function (el) { empty(el, "그래프 라이브러리를 불러오지 못했습니다."); });
    toolbar.hidden = true;
    return;
  }

  var HOUR = 3600, DAY = 86400, HEIGHT = 180;
  var GAP = 2 * HOUR;       // 보고가 이만큼 끊기면 선을 잇지 않는다. 이으면 없던 값이 있던 것처럼 보인다
  var MIN_SPAN = HOUR;      // 이보다 좁게는 확대하지 않는다
  var WEEK = ["일", "월", "화", "수", "목", "금", "토"];
  var ts = data.t;
  var TZ = data.tz;         // 날씨 위치의 시간대. 축과 시각 글자를 이 시간대로 그린다
  function zoned(t) { return uPlot.tzDate(new Date(t * 1000), TZ); }
  var first = ts[0], last = ts[ts.length - 1];
  var view = { min: Math.max(first, last - 3 * DAY), max: last };
  var charts = [];
  var pinned = null;        // 눌러 둔 점 { chart, i }

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function local(t) {
    var d = zoned(t);
    return (d.getMonth() + 1) + "/" + d.getDate() + " (" + WEEK[d.getDay()] + ") " +
      pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function utc(t) {
    var d = new Date(t * 1000);
    return (d.getUTCMonth() + 1) + "/" + d.getUTCDate() + " " +
      pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }
  function length(s) {
    var d = Math.floor(s / DAY), h = Math.floor(s % DAY / HOUR), m = Math.round(s % HOUR / 60);
    if (m === 60) { h += 1; m = 0; }
    if (d) return d + "일" + (h ? " " + h + "시간" : "");
    if (h) return h + "시간" + (m ? " " + m + "분" : "");
    return m + "분";
  }

  // 한 그래프의 점들. 값이 없는 보고는 빼고, 오래 끊긴 자리에는 null 을 끼워 선을 끊는다.
  // src 는 각 점이 원래 몇 번째 보고였는지다. 눌렀을 때 그 보고의 펌웨어를 찾는 데 쓴다.
  // 축이 고정된 그래프는 축 밖 값을 가장자리에 붙여 그린다. 눌렀을 때는 실제 값(raw)을 보인다.
  function points(key) {
    var vals = data.series[key], f = data.fixed[key], x = [], y = [], raw = [], src = [];
    for (var k = 0; k < ts.length; k++) {
      var v = vals[k];
      if (v === null || v === undefined) continue;
      if (x.length && ts[k] - x[x.length - 1] > GAP) {
        x.push(x[x.length - 1] + 1); y.push(null); raw.push(null); src.push(-1);
      }
      x.push(ts[k]); y.push(f ? Math.max(f[0], Math.min(f[1], v)) : v); raw.push(v); src.push(k);
    }
    return { x: x, y: y, raw: raw, src: src };
  }

  function nearest(ys, i) {
    if (i === null || i === undefined) return null;
    for (var k = 0; k < ys.length; k++) {
      if (ys[i - k] != null) return i - k;
      if (ys[i + k] != null) return i + k;
    }
    return null;
  }

  // 하루보다 촘촘한 눈금은 시각만, 날짜가 바뀌는 눈금에만 날짜를 위에 붙인다.
  function xLabels(u, splits, ax, space, incr) {
    var prev = null;
    return splits.map(function (t) {
      var d = zoned(t), day = (d.getMonth() + 1) + "/" + d.getDate();
      if (incr >= DAY) return day;
      var s = (day !== prev ? day : "") + "\n" + pad(d.getHours()) + ":" + pad(d.getMinutes());
      prev = day;
      return s;
    });
  }

  function palette() {
    // 색은 page.css 의 변수에서. 그래프마다 --plot-<항목> 색이 있고, 없으면 강조색. 다크 모드로 바뀌면
    // build() 가 다시 읽는다.
    var s = getComputedStyle(document.body);
    var v = function (name) { return s.getPropertyValue(name).trim(); };
    return { fg: s.color, bg: s.backgroundColor, axis: v("--muted"), grid: v("--line"), guide: v("--control"),
             fillAlpha: parseFloat(v("--plot-fill-alpha")) || 0.1,
             color: function (key) { return v("--plot-" + key) || v("--accent"); } };
  }

  // "#12936b" -> "rgba(18,147,107,a)". 선 아래 면은 선과 같은 색을 옅게 칠한다.
  function tint(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }

  function drawGuides(u, guides, p) {
    if (!guides.length) return;
    var ctx = u.ctx, b = u.bbox, r = uPlot.pxRatio;
    ctx.save();
    ctx.strokeStyle = p.guide; ctx.fillStyle = p.axis; ctx.lineWidth = r;
    ctx.setLineDash([3 * r, 5 * r]);
    ctx.font = (11 * r) + "px system-ui, sans-serif";
    ctx.textAlign = "right"; ctx.textBaseline = "bottom";
    guides.forEach(function (g) {
      var y = Math.round(u.valToPos(g[0], "y", true));
      ctx.beginPath(); ctx.moveTo(b.left, y); ctx.lineTo(b.left + b.width, y); ctx.stroke();
    });
    // 이름표는 선을 다 그은 뒤 바탕을 깔고 올린다. 그래야 꺾은선에 묻히지 않는다.
    ctx.textBaseline = "middle";
    guides.forEach(function (g) {
      var y = Math.round(u.valToPos(g[0], "y", true)), text = g[1] + " " + g[0];
      var w = ctx.measureText(text).width, x = b.left + b.width - 4 * r;
      ctx.fillStyle = p.bg; ctx.fillRect(x - w - 3 * r, y - 7 * r, w + 6 * r, 14 * r);
      ctx.fillStyle = p.axis; ctx.fillText(text, x, y);
    });
    ctx.restore();
  }

  function line(text, cls) {
    var s = document.createElement(cls === "b" ? "b" : "span");
    if (cls === "dim") s.className = "dim";
    s.textContent = text;
    return s;
  }

  function placeTip(c) {
    var u = c.u, i = pinned.i, x = u.data[0][i];
    if (x < u.scales.x.min || x > u.scales.x.max) { tip.hidden = true; return; }
    var box = u.over.getBoundingClientRect();
    var left = u.valToPos(x, "x"), top = u.valToPos(u.data[1][i], "y");
    tip.style.left = (box.left + window.scrollX + left) + "px";
    tip.style.top = (box.top + window.scrollY + top) + "px";
    tip.classList.toggle("flip", left > box.width * 0.6);
    tip.hidden = false;
  }

  function drawPin(c, p) {
    if (!pinned || pinned.chart !== c) return;
    var u = c.u, i = pinned.i, x = u.data[0][i];
    if (x >= u.scales.x.min && x <= u.scales.x.max) {
      var r = uPlot.pxRatio, ctx = u.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.arc(u.valToPos(x, "x", true), u.valToPos(u.data[1][i], "y", true), 5 * r, 0, 2 * Math.PI);
      ctx.fillStyle = c.color; ctx.fill();
      ctx.lineWidth = 2 * r; ctx.strokeStyle = p.bg; ctx.stroke();
      ctx.restore();
    }
    placeTip(c);
  }

  function pin(c, i) {
    var before = pinned && pinned.chart;
    var k = c.src[i];
    pinned = { chart: c, i: i };
    tip.innerHTML = "";
    tip.appendChild(line(local(ts[k]), "b"));
    tip.appendChild(line("UTC " + utc(ts[k]), "dim"));
    tip.appendChild(line(c.raw[i] + " " + c.unit));
    tip.appendChild(line("펌웨어 " + (data.fw_version[k] || "알 수 없음 (버전을 안 보내던 때)")));
    if (data.mac) {
      tip.appendChild(line(data.name[k] || data.mac[k] || "MAC 없음", "dim"));
      if (data.name[k] && data.mac[k]) tip.appendChild(line(data.mac[k], "dim"));
    }
    if (before && before !== c) before.u.redraw(false);
    c.u.redraw(false);
  }

  function unpin() {
    if (!pinned) return;
    var c = pinned.chart;
    pinned = null;
    tip.hidden = true;
    c.u.redraw(false);
  }

  function showRange() {
    rangeEl.textContent = local(view.min) + " – " + local(view.max) + " · " + length(view.max - view.min);
  }

  // 모든 그래프가 같은 기간을 본다. 데이터가 있는 범위 밖으로는 나가지 않는다.
  function setView(a, b) {
    if (b - a < MIN_SPAN) { var m = (a + b) / 2; a = m - MIN_SPAN / 2; b = m + MIN_SPAN / 2; }
    if (b - a >= last - first) { a = first; b = last; }
    else {
      if (a < first) { b += first - a; a = first; }
      if (b > last) { a -= b - last; b = last; }
    }
    view.min = a; view.max = b;
    charts.forEach(function (c) { c.u.setScale("x", { min: a, max: b }); });
    showRange();
  }

  function zoom(f, at) {
    if (at === undefined) at = (view.min + view.max) / 2;
    setView(at - (at - view.min) * f, at + (view.max - at) * f);
  }

  function build() {
    charts.forEach(function (c) { c.u.destroy(); });
    charts = [];
    pinned = null;
    tip.hidden = true;
    var p = palette();
    boxes.forEach(function (el) {
      var key = el.dataset.key, pts = points(key);
      el.innerHTML = "";
      if (pts.y.filter(function (v) { return v !== null; }).length < 2) {
        empty(el, "그래프를 그리려면 보고가 2건 이상 필요합니다.");
        return;
      }
      var c = { el: el, unit: el.dataset.unit, src: pts.src, raw: pts.raw, color: p.color(key) };
      var guides = data.guides[key] || [];
      var fixed = data.fixed[key];
      var axis = { stroke: p.axis, grid: { stroke: p.grid, width: 1 }, ticks: { stroke: p.grid, width: 1 } };
      c.u = new uPlot({
        width: el.clientWidth, height: HEIGHT,
        legend: { show: false },
        tzDate: zoned,                        // 눈금 위치도 날씨 위치의 자정·정시에 맞춘다
        scales: { x: { time: true, auto: false, min: view.min, max: view.max },
                  y: fixed ? { range: fixed } : {} },
        // 면은 늘 아래쪽으로 채운다. 기본값은 0 쪽이라 신호세기처럼 음수인 값은 선 위가 채워진다.
        series: [{}, { stroke: c.color, fill: tint(c.color, p.fillAlpha), fillTo: function (u) { return u.scales.y.min; },
                       width: 2, spanGaps: false,
                       points: { size: 5, stroke: c.color, fill: p.bg } }],
        axes: [Object.assign({ size: 46, values: xLabels }, axis), Object.assign({ size: 56 }, axis)],
        cursor: {
          y: false,
          drag: { x: true, y: false, setScale: false },
          dataIdx: function (u, si, i) { return si === 0 ? i : nearest(u.data[1], i); },
          points: { size: 9, width: 2, stroke: c.color, fill: p.bg },
          bind: { dblclick: function () { return null; } }
        },
        hooks: {
          setSelect: [function (u) {
            if (u.select.width < 5) return;
            var a = u.posToVal(u.select.left, "x"), b = u.posToVal(u.select.left + u.select.width, "x");
            u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
            setView(a, b);
          }],
          drawSeries: [function (u) { drawGuides(u, guides, p); }],
          draw: [function (u) { drawPin(c, p); }]
        }
      }, [pts.x, pts.y], el);

      var over = c.u.over, downX = 0;
      over.addEventListener("mousedown", function (e) { downX = e.clientX; });
      over.addEventListener("click", function (e) {
        if (Math.abs(e.clientX - downX) > 3) return;      // 끌어서 구간을 고른 것
        var x = e.clientX - over.getBoundingClientRect().left;
        var i = nearest(c.u.data[1], c.u.posToIdx(x));
        if (i === null) return;
        if (pinned && pinned.chart === c && pinned.i === i) unpin(); else pin(c, i);
      });
      over.addEventListener("dblclick", function () { setView(first, last); });
      over.addEventListener("wheel", function (e) {
        if (e.ctrlKey || e.metaKey) {               // 트랙패드 핀치도 ctrlKey 가 붙어 온다
          e.preventDefault();
          var at = c.u.posToVal(e.clientX - over.getBoundingClientRect().left, "x");
          zoom(Math.pow(1.002, e.deltaY), at);
        } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
          e.preventDefault();
          var dt = e.deltaX / over.clientWidth * (view.max - view.min);
          setView(view.min + dt, view.max + dt);
        }
      }, { passive: false });
      charts.push(c);
    });
    showRange();
  }

  if (ts.length < 2) {
    toolbar.hidden = true;
    boxes.forEach(function (el) { empty(el, "그래프를 그리려면 보고가 2건 이상 필요합니다."); });
    return;
  }

  toolbar.addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.zoom) return zoom(+b.dataset.zoom);
    var s = +b.dataset.span;
    if (s) setView(last - s, last); else setView(first, last);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") unpin(); });
  window.addEventListener("resize", function () {
    charts.forEach(function (c) { c.u.setSize({ width: c.el.clientWidth, height: HEIGHT }); });
  });
  if (window.matchMedia) {
    window.addEventListener("themechange", build);          // 다크 모드 스위치를 바꿨을 때 (theme.js)
  }
  build();
})();
