// A list keeps the row last clicked marked, also after a reload of this tab:
// <table data-selected-key="a storage key"> with rows <tr data-row="an id">.
(() => {
  for (const table of document.querySelectorAll("table[data-selected-key]")) {
    const key = table.dataset.selectedKey;
    const mark = (id) => {
      for (const row of table.querySelectorAll("tr[data-row]")) row.classList.toggle("selected", row.dataset.row === id);
    };
    try { mark(sessionStorage.getItem(key)); } catch {}
    table.addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-row]");
      if (!row) return;
      mark(row.dataset.row);
      try { sessionStorage.setItem(key, row.dataset.row); } catch {}
    });
  }
})();
