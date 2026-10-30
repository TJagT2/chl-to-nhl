// CHL Prospect Explorer: search-as-you-type, sortable tables, and simple bar charts.

// ---- Search ------------------------------------------------------------------
function attachSearch(inputId, listId) {
  const input = document.getElementById(inputId);
  const list = document.getElementById(listId);
  if (!input || !list) return;
  let timer = null, focus = -1;

  const render = items => {
    focus = -1;
    list.innerHTML = items.map(p => `
      <li><a href="/player/${p.id}">
        <span>${escapeHtml(p.name)}</span>
        <span class="tag">${p.leagues} · ${p.years}${p.nhl ? " · NHL" : ""}</span>
      </a></li>`).join("");
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { list.innerHTML = ""; return; }
    timer = setTimeout(async () => {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      render(await res.json());
    }, 150);
  });

  input.addEventListener("keydown", e => {
    const links = [...list.querySelectorAll("a")];
    if (!links.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      focus = (focus + (e.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
      links.forEach((a, i) => a.classList.toggle("focus", i === focus));
    } else if (e.key === "Enter") {
      e.preventDefault();
      window.location = links[Math.max(focus, 0)].href;
    } else if (e.key === "Escape") {
      list.innerHTML = "";
    }
  });

  document.addEventListener("click", e => {
    if (!list.contains(e.target) && e.target !== input) list.innerHTML = "";
  });
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

// ---- Sortable tables ---------------------------------------------------------
function makeSortable(table) {
  const headers = [...table.tHead.rows[0].cells];
  headers.forEach((th, col) => {
    th.addEventListener("click", () => {
      // first click sorts high-to-low; clicking again flips the order
      const asc = th.getAttribute("aria-sort") === "descending";
      headers.forEach(h => h.removeAttribute("aria-sort"));
      th.setAttribute("aria-sort", asc ? "ascending" : "descending");
      const body = table.tBodies[0];
      const rows = [...body.rows];
      const value = row => {
        const cell = row.cells[col];
        const raw = cell.dataset.sort ?? cell.textContent.trim();
        const n = parseFloat(String(raw).replace(/[,%x]/g, ""));
        return Number.isNaN(n) ? String(raw).toLowerCase() : n;
      };
      rows.sort((a, b) => {
        const va = value(a), vb = value(b);
        const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
        return asc ? cmp : -cmp;
      });
      rows.forEach(r => body.appendChild(r));
    });
  });
}

// ---- Bar chart (inline SVG, single series) -----------------------------------
// data: [{label, value, ...}], opts: {format(v), tooltip(d), title}
function barChart(containerId, data, opts = {}) {
  const el = document.getElementById(containerId);
  if (!el || !data.length) return;
  // draw at the container's real width so text stays readable on phones
  const W = Math.max(320, el.clientWidth || 900), H = 260, m = {top: opts.title ? 30 : 16, right: 8, bottom: 28, left: 8};
  const max = Math.max(...data.map(d => d.value || 0)) * 1.12 || 1;
  const band = (W - m.left - m.right) / data.length;
  const bw = Math.min(56, band * 0.6);
  const y = v => m.top + (H - m.top - m.bottom) * (1 - v / max);
  const fmt = opts.format || (v => v);
  const ns = "http://www.w3.org/2000/svg";

  let svg = `<svg viewBox="0 0 ${W} ${H}" xmlns="${ns}">`;
  if (opts.title) svg += `<text class="title" x="${m.left}" y="14">${opts.title}</text>`;
  svg += `<g class="grid"><line x1="${m.left}" x2="${W - m.right}" y1="${y(0)}" y2="${y(0)}"/></g>`;
  data.forEach((d, i) => {
    const cx = m.left + band * i + band / 2;
    const top = y(d.value || 0), h = y(0) - top;
    const r = Math.min(4, h / 2);
    // bar with 4px rounded top corners, square at the baseline
    svg += `<path class="bar" data-i="${i}" d="M${cx - bw / 2},${y(0)} V${top + r} Q${cx - bw / 2},${top} ${cx - bw / 2 + r},${top}
      H${cx + bw / 2 - r} Q${cx + bw / 2},${top} ${cx + bw / 2},${top + r} V${y(0)} Z"/>`;
    // invisible, larger hit area for hover
    svg += `<rect data-i="${i}" x="${m.left + band * i}" y="${m.top}" width="${band}" height="${H - m.top - m.bottom}" fill="transparent"/>`;
    if (data.length <= 12) svg += `<text class="value" x="${cx}" y="${top - 5}" text-anchor="middle">${fmt(d.value)}</text>`;
    svg += `<g class="axis"><text x="${cx}" y="${H - 8}" text-anchor="middle">${d.label}</text></g>`;
  });
  svg += "</svg>";
  el.innerHTML = svg + `<div class="tooltip"></div>`;

  const tip = el.querySelector(".tooltip");
  const bars = el.querySelectorAll(".bar");
  el.querySelectorAll("[data-i]").forEach(node => {
    node.addEventListener("mousemove", e => {
      const i = +node.dataset.i, d = data[i];
      bars.forEach((b, j) => b.classList.toggle("hot", j === i));
      tip.textContent = opts.tooltip ? opts.tooltip(d) : `${d.label}: ${fmt(d.value)}`;
      const box = el.getBoundingClientRect();
      tip.style.left = `${e.clientX - box.left}px`;
      tip.style.top = `${e.clientY - box.top}px`;
      tip.classList.add("show");
    });
    node.addEventListener("mouseleave", () => {
      tip.classList.remove("show");
      bars.forEach(b => b.classList.remove("hot"));
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  attachSearch("search", "search-results");
  document.querySelectorAll("table.sortable").forEach(makeSortable);
});
