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
    const daysSinceThursday = (start.getDay() - 4 + 7) % 7;
    start.setDate(start.getDate() - daysSinceThursday);
    weekStart.value = toIsoLocal(start);
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

(() => {
  const dialog = document.querySelector("#authorization-dialog");
  if (!dialog) return;

  const employeeInput = dialog.querySelector("#authorization-employee-value");
  const employeeName = dialog.querySelector("#authorization-employee-name");
  const startInput = dialog.querySelector("#authorization-start");
  const endInput = dialog.querySelector("#authorization-end");
  const dayInputs = [...dialog.querySelectorAll("[data-dialog-day]")];

  document.querySelectorAll("[data-add-authorization]").forEach((button) => {
    button.addEventListener("click", () => {
      employeeInput.value = button.dataset.employee;
      employeeName.textContent = button.dataset.employee;
      startInput.value = "17:00";
      endInput.value = "19:00";
      dayInputs.forEach((input) => {
        input.checked = false;
      });
      dialog.showModal();
    });
  });

  dialog.querySelectorAll("[data-dialog-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
})();

(() => {
  const dialog = document.querySelector("#report-detail-dialog");
  if (!dialog) return;

  const employeeName = dialog.querySelector("#report-detail-name");
  const detailBody = dialog.querySelector("#report-detail-body");

  document.querySelectorAll("[data-report-worker]").forEach((button) => {
    button.addEventListener("click", () => {
      const template = document.querySelector(
        `#${CSS.escape(button.dataset.template)}`
      );
      if (!template) return;
      employeeName.textContent = button.dataset.employee;
      detailBody.replaceChildren(template.content.cloneNode(true));
      dialog.showModal();
    });
  });

  dialog.querySelectorAll("[data-report-dialog-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
})();

(() => {
  const normalizeSearch = (value) => value
    .trim()
    .toLocaleLowerCase("es")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");

  const browser = document.querySelector("[data-employees-browser]");
  if (!browser) return;

  const search = browser.querySelector("[data-employees-search]");
  const areaFilter = browser.querySelector("[data-employees-area-filter]");
  const empty = browser.querySelector("[data-employees-empty]");
  const rows = [...browser.querySelectorAll("[data-employee-row]")];
  if (!search || !areaFilter) return;

  const filterRows = () => {
    const query = normalizeSearch(search.value);
    const area = normalizeSearch(areaFilter.value);
    let visible = 0;
    rows.forEach((row) => {
      const matches = normalizeSearch(row.dataset.employeeName).includes(query)
        && (!area || normalizeSearch(row.dataset.employeeArea) === area);
      row.hidden = !matches;
      row.classList.toggle("is-filtered-out", !matches);
      if (matches) visible += 1;
    });
    if (empty) empty.hidden = visible > 0;
  };

  search.addEventListener("input", filterRows);
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    filterRows();
  });
  areaFilter.addEventListener("change", filterRows);
})();

(() => {
  const normalizeSearch = (value) => value
    .trim()
    .toLocaleLowerCase("es")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");

  const browser = document.querySelector("[data-home-browser]");
  if (!browser) return;

  const search = browser.querySelector("[data-home-search]");
  const areaFilter = browser.querySelector("[data-home-area-filter]");
  const profile = browser.querySelector("[data-home-profile]");
  const empty = browser.querySelector("[data-home-empty]");
  const workers = [...browser.querySelectorAll("[data-home-worker]")];

  const showWorker = (button) => {
    const template = document.getElementById(button.dataset.template);
    if (!template) return;
    workers.forEach((worker) => worker.classList.toggle("active", worker === button));
    profile.replaceChildren(template.content.cloneNode(true));
  };

  workers.forEach((button) => {
    button.addEventListener("click", () => showWorker(button));
  });

  const filterWorkers = () => {
    const query = normalizeSearch(search.value);
    const area = normalizeSearch(areaFilter.value);
    const matchingWorkers = [];
    workers.forEach((button) => {
      const matches = normalizeSearch(button.dataset.workerName).includes(query)
        && (!area || normalizeSearch(button.dataset.workerArea) === area);
      button.hidden = !matches;
      button.classList.toggle("is-filtered-out", !matches);
      if (matches) matchingWorkers.push(button);
    });
    empty.hidden = matchingWorkers.length > 0;
    if (matchingWorkers.length) {
      showWorker(matchingWorkers[0]);
    } else {
      workers.forEach((worker) => worker.classList.remove("active"));
      profile.replaceChildren();
    }
  };

  search.addEventListener("input", filterWorkers);
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    filterWorkers();
  });
  areaFilter.addEventListener("change", filterWorkers);

  if (workers.length) showWorker(workers[0]);
})();

(() => {
  const dialog = document.querySelector("#home-authorization-dialog");
  if (!dialog) return;

  const employeeName = dialog.querySelector("#home-authorization-name");
  const workDate = dialog.querySelector("#home-authorization-date");
  const allowedRange = dialog.querySelector("#home-authorization-range");
  const countedTime = dialog.querySelector("#home-authorization-counted");

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-home-authorization]");
    if (!button) return;
    employeeName.textContent = button.dataset.employee;
    workDate.textContent = button.dataset.date;
    allowedRange.textContent = button.dataset.range;
    countedTime.textContent = button.dataset.counted;
    dialog.showModal();
  });

  dialog.querySelectorAll("[data-home-authorization-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
})();
