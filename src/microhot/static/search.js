// Search over every edition on this site. The index (/search.json) is built with the site;
// matching happens in the browser, so nothing leaves the page.
(function () {
  var input = document.getElementById("q");
  var list = document.getElementById("results");
  var status = document.getElementById("search-status");
  if (!input || !list || !status) return;
  var zh = document.documentElement.lang.indexOf("zh") === 0;
  var col = zh ? 1 : 0;
  var prefix = zh ? "/zh-tw" : "";
  var data = null;

  function load() {
    if (data) return Promise.resolve(data);
    status.textContent = status.getAttribute("data-loading");
    return fetch("/search.json", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (rows) { data = rows; status.textContent = ""; return rows; });
  }

  function show(query) {
    var q = query.trim().toLowerCase();
    list.textContent = "";
    if (!q) { status.textContent = ""; return; }
    var terms = q.split(/\s+/);
    var hits = data.filter(function (r) {
      var hay = (r.t.join(" ") + " " + r.m.join(" ") + " " + r.s).toLowerCase();
      return terms.every(function (term) { return hay.indexOf(term) !== -1; });
    }).slice(0, 50);
    status.textContent = hits.length ? "" : status.getAttribute("data-none");
    hits.forEach(function (r) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = r.u.charAt(0) === "/" ? prefix + r.u : r.u;
      a.textContent = r.t[col] || r.t[0];
      var meta = document.createElement("span");
      meta.className = "meta-inline";
      meta.textContent = " " + r.d + ", " + r.s;
      var p = document.createElement("p");
      p.className = "summary";
      p.textContent = r.m[col] || r.m[0];
      li.appendChild(a);
      li.appendChild(meta);
      li.appendChild(p);
      list.appendChild(li);
    });
  }

  var timer;
  input.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { load().then(function () { show(input.value); }); }, 120);
  });
  var initial = new URLSearchParams(location.search).get("q");
  if (initial) {
    input.value = initial;
    load().then(function () { show(initial); });
  }
})();
