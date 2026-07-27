(() => {
  const menuButton = document.querySelector("[data-menu-toggle]");
  const sidebar = document.querySelector("#sidebar");
  if (menuButton && sidebar) {
    menuButton.addEventListener("click", () => {
      sidebar.classList.toggle("open");
    });
  }

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const workerList = document.querySelector("#worker-list");
  if (!workerList) return;

  const loadButton = document.querySelector("#load-workers");
  const referenceDate = document.querySelector("#reference-date");
  const workerState = document.querySelector("#worker-state");
  const search = document.querySelector("#worker-search");
  const selectAll = document.querySelector("#select-all");
  const weekStart = document.querySelector("#week-start");
  const dayGrid = document.querySelector("#day-grid");
  let workers = [];

  const escapeHtml = (value) => {
    const element = document.createElement("div");
    element.textContent = value;
    return element.innerHTML;
  };
  const escapeAttribute = (value) =>
    escapeHtml(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");

  const renderWorkers = () => {
    const query = search.value.trim().toLocaleLowerCase("es");
    const filtered = workers.filter((worker) =>
      `${worker.name} ${worker.area}`.toLocaleLowerCase("es").includes(query)
    );
    workerList.innerHTML = filtered.map((worker) => `
      <label class="worker-option">
        <input type="checkbox" name="employee_names"
               value="${escapeAttribute(worker.name)}">
        <span class="worker-avatar">${escapeHtml(worker.name.charAt(0).toUpperCase())}</span>
        <span>
          <strong>${escapeHtml(worker.name)}</strong>
          <small>${escapeHtml(worker.area || "Sin área")}</small>
        </span>
      </label>
    `).join("");
    workerState.hidden = filtered.length > 0;
    if (!filtered.length) {
      workerState.innerHTML = "<span>No hay trabajadores que coincidan.</span>";
    }
  };

  loadButton.addEventListener("click", async () => {
    loadButton.disabled = true;
    workerState.hidden = false;
    workerState.innerHTML = "<span class=\"spinner\"></span> Consultando Hik-Connect…";
    workerList.innerHTML = "";
    try {
      const response = await fetch(
        `/api/trabajadores?fecha=${encodeURIComponent(referenceDate.value)}&actualizar=1`,
        { headers: { Accept: "application/json" } }
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "No fue posible consultar.");
      workers = payload.workers;
      search.disabled = false;
      selectAll.disabled = false;
      renderWorkers();
      if (!workers.length) {
        workerState.hidden = false;
        workerState.innerHTML = "<span>No se encontraron trabajadores en esa fecha.</span>";
      }
    } catch (error) {
      workerState.hidden = false;
      workerState.innerHTML = `<span class="danger-text">${escapeHtml(error.message)}</span>`;
    } finally {
      loadButton.disabled = false;
    }
  });

  search.addEventListener("input", renderWorkers);
  selectAll.addEventListener("click", () => {
    workerList.querySelectorAll('input[type="checkbox"]').forEach((input) => {
      input.checked = true;
    });
  });

  const toIsoLocal = (day) => {
    const year = day.getFullYear();
    const month = String(day.getMonth() + 1).padStart(2, "0");
    const date = String(day.getDate()).padStart(2, "0");
    return `${year}-${month}-${date}`;
  };

  const dayNames = [
    "domingo", "lunes", "martes", "miércoles",
    "jueves", "viernes", "sábado"
  ];
  const monthNames = [
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic"
  ];

  const renderDays = () => {
    if (!weekStart.value) return;
    const [year, month, day] = weekStart.value.split("-").map(Number);
    const selected = new Set(
      [...dayGrid.querySelectorAll("input:checked")].map((input) => input.value)
    );
    const start = new Date(year, month - 1, day);
    dayGrid.innerHTML = "";
    for (let index = 0; index < 7; index += 1) {
      const current = new Date(start);
      current.setDate(start.getDate() + index);
      const iso = toIsoLocal(current);
      const label = document.createElement("label");
      label.className = "day-option";
      label.innerHTML = `
        <input type="checkbox" name="work_dates" value="${iso}"
               ${selected.has(iso) ? "checked" : ""}>
        <span>
          <small>${dayNames[current.getDay()]} · ${monthNames[current.getMonth()]}</small>
          <strong>${current.getDate()}</strong>
        </span>
      `;
      dayGrid.appendChild(label);
    }
  };
  weekStart.addEventListener("change", renderDays);
})();
