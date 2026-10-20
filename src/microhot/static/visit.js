// "New since your last visit": stories picked after your previous visit get a dot.
// The visit times stay in this browser only (localStorage); nothing is sent anywhere.
(function () {
  try {
    var key = "microhot:visit";
    var now = Date.now();
    var visit = JSON.parse(localStorage.getItem(key) || "{}");
    if (!visit.last || now - visit.last > 30 * 60 * 1000) {
      visit.prev = visit.last || 0; // a new session starts after 30 minutes away
    }
    visit.last = now;
    localStorage.setItem(key, JSON.stringify(visit));
    if (!visit.prev) return;
    var label = document.documentElement.getAttribute("data-unseen") || "";
    document.querySelectorAll("[data-picked]").forEach(function (el) {
      var picked = Date.parse(el.getAttribute("data-picked"));
      if (picked && picked > visit.prev) {
        el.classList.add("unseen");
        var hint = document.createElement("span");
        hint.className = "sr-only";
        hint.textContent = label;
        el.insertBefore(hint, el.firstChild);
      }
    });
  } catch (e) {
    // Storage is unavailable (private window, blocked site data): no markers, nothing breaks.
  }
})();
