/* ──────────────────────────────────────────────────────────────────────────
 * Click-to-sort for every data table — no dependencies, no server round-trip.
 *
 * Any <table> with a <thead> and at least two body rows becomes sortable: click
 * a column header to sort, click again to reverse. Sorting is type-aware —
 * currency ($1,234.56), percentages (+1.93%), plain numbers, accounting
 * negatives ((478) -> -478), and ISO / US dates are compared by value, not as
 * text; everything else sorts alphabetically (case-insensitive).
 *
 * Notes for this app:
 *  - Totals live in <tfoot>, which is never reordered.
 *  - Grouped tables (multiple <tbody>) sort WITHIN each group, preserving groups.
 *  - Privacy mode hides numbers with CSS only, so the real values are still in
 *    the cell text and sorting stays correct even while masked.
 *  - Rows keep their event handlers (e.g. click-to-open) because we reorder the
 *    existing <tr> nodes rather than rebuilding them.
 *  - Opt a table (or a single <th>) out with the data-no-sort attribute. A cell
 *    may override its sort key with data-sort="...".
 * ────────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  function rawText(cell) {
    if (cell.hasAttribute("data-sort")) return cell.getAttribute("data-sort").trim();
    return (cell.textContent || "").replace(/\s+/g, " ").trim();
  }

  // "$1,234.56" -> 1234.56 · "+1.93%" -> 1.93 · "$-478" -> -478 · "(478)" -> -478
  function asNumber(s) {
    if (!s) return null;
    var neg = /^\(.*\)$/.test(s); // accounting parentheses = negative
    var cleaned = s.replace(/[(),\s]/g, "").replace(/[^0-9.+\-]/g, "").replace(/^\+/, "");
    if (cleaned === "" || cleaned === "-" || cleaned === "." || cleaned === "+") return null;
    var n = Number(cleaned);
    if (!isFinite(n)) return null;
    return neg ? -Math.abs(n) : n;
  }

  // ISO (2026-06-26), US (6/26/2026), or "Jun 26, 2026" -> epoch ms
  function asDate(s) {
    if (!s) return null;
    if (
      /^\d{4}-\d{2}-\d{2}([T\s]|$)/.test(s) ||
      /^\d{1,2}\/\d{1,2}\/\d{2,4}($|\s)/.test(s) ||
      /^[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}($|\s)/.test(s)
    ) {
      var t = Date.parse(s);
      return isNaN(t) ? null : t;
    }
    return null;
  }

  // Decide a column's type from a majority vote of its non-empty cells.
  function columnType(cells) {
    var dates = 0, nums = 0, total = 0;
    for (var i = 0; i < cells.length; i++) {
      var s = rawText(cells[i]);
      if (!s) continue;
      total++;
      if (asDate(s) !== null) dates++;
      else if (asNumber(s) !== null) nums++;
    }
    if (total === 0) return "str";
    if (dates / total >= 0.6) return "date";
    if (nums / total >= 0.6) return "num";
    return "str";
  }

  function keyFor(cell, type) {
    var s = rawText(cell);
    if (type === "num") return asNumber(s);
    if (type === "date") return asDate(s);
    return s ? s.toLowerCase() : "";
  }

  function isEmpty(k) {
    return k === null || k === undefined || k === "";
  }

  function sortTbody(tbody, colIdx, type, dir) {
    var rows = Array.prototype.slice.call(tbody.rows);
    var sortable = [], trailing = [];
    rows.forEach(function (r, i) {
      if (r.cells.length > colIdx) sortable.push({ r: r, k: keyFor(r.cells[colIdx], type), i: i });
      else trailing.push(r); // rows without this column (e.g. spacers) stay at the end
    });
    sortable.sort(function (a, b) {
      var ae = isEmpty(a.k), be = isEmpty(b.k);
      if (ae && be) return a.i - b.i;
      if (ae) return 1; // blanks always sink, regardless of direction
      if (be) return -1;
      var cmp = type === "str" ? (a.k < b.k ? -1 : a.k > b.k ? 1 : 0) : a.k - b.k;
      if (cmp === 0) return a.i - b.i; // stable
      return dir === "asc" ? cmp : -cmp;
    });
    var frag = document.createDocumentFragment();
    sortable.forEach(function (o) { frag.appendChild(o.r); });
    trailing.forEach(function (r) { frag.appendChild(r); });
    tbody.appendChild(frag);
  }

  function sortBy(table, headRow, idx) {
    var th = headRow.cells[idx];
    var bodies = Array.prototype.slice.call(table.tBodies);

    var sample = [];
    bodies.forEach(function (b) {
      Array.prototype.forEach.call(b.rows, function (r) {
        if (r.cells.length > idx) sample.push(r.cells[idx]);
      });
    });
    var type = columnType(sample);

    var prev = th.getAttribute("aria-sort");
    var dir;
    if (prev === "ascending") dir = "desc";
    else if (prev === "descending") dir = "asc";
    else dir = type === "str" ? "asc" : "desc"; // first click: A->Z for text, big->small for numbers/dates

    Array.prototype.forEach.call(headRow.cells, function (c) {
      if (c === th) return;
      c.removeAttribute("aria-sort");
      var ci = c.querySelector(".kfo-sort-ind");
      if (ci) ci.textContent = "";
    });
    th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
    var ind = th.querySelector(".kfo-sort-ind");
    if (ind) ind.textContent = dir === "asc" ? " ▲" : " ▼";

    bodies.forEach(function (b) { sortTbody(b, idx, type, dir); });
  }

  function enable(table) {
    if (table.dataset.sortable) return;
    if (table.hasAttribute("data-no-sort") || table.closest("[data-no-sort]")) return;
    if (!table.tHead || !table.tHead.rows.length) return;
    var rowCount = 0;
    Array.prototype.forEach.call(table.tBodies, function (b) { rowCount += b.rows.length; });
    if (rowCount < 2) return;

    var headRow = table.tHead.rows[table.tHead.rows.length - 1];
    table.dataset.sortable = "1";
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      if (th.hasAttribute("data-no-sort")) return;
      th.classList.add("kfo-sort-th");
      th.setAttribute("role", "button");
      th.setAttribute("tabindex", "0");
      th.title = "Sort by " + (th.textContent || "").trim();
      var ind = document.createElement("span");
      ind.className = "kfo-sort-ind";
      ind.setAttribute("aria-hidden", "true");
      th.appendChild(ind);
      var run = function () { sortBy(table, headRow, idx); };
      th.addEventListener("click", run);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); run(); }
      });
    });
  }

  function scan(root) {
    var tables = (root || document).querySelectorAll("table");
    Array.prototype.forEach.call(tables, enable);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { scan(document); });
  } else {
    scan(document);
  }
  // Re-scan content swapped in by htmx (and similar partial updates).
  document.body.addEventListener("htmx:afterSwap", function (e) { scan(e.target); });
  document.body.addEventListener("htmx:load", function (e) { scan(e.target); });
})();
