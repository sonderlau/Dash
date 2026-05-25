const THEME_KEY = "dash-theme";
const UI_STATE_KEY = "dash-ui-state";
const FILTER_COOKIE_KEY = "dash-filter-keywords";
const COOKIE_MAX_AGE = 60 * 60 * 24 * 365;
const FILTER_SEPARATORS = /[\n,，;；|]+/;

const state = {
  index: null,
  currentDate: null,
  currentPayload: null,
  currentPapers: [],
  visiblePapers: [],
  selectedCategories: null,
  searchQuery: "",
  keywordFilter: "",
  pendingRenderToken: 0,
};

const elements = {
  siteTitle: document.querySelector("#site-title"),
  siteDescription: document.querySelector("#site-description"),
  latestDate: document.querySelector("#latest-date"),
  currentDateTitle: document.querySelector("#current-date-title"),
  currentDateMeta: document.querySelector("#current-date-meta"),
  toolbarCategoryCounts: document.querySelector("#toolbar-category-counts"),
  dateInput: document.querySelector("#date-input"),
  dateHint: document.querySelector("#date-hint"),
  searchInput: document.querySelector("#search-input"),
  filterInput: document.querySelector("#filter-input"),
  themeToggle: document.querySelector("#theme-toggle"),
  paperList: document.querySelector("#paper-list"),
  paperTemplate: document.querySelector("#paper-card-template"),
  modal: document.querySelector("#paper-modal"),
  modalClose: document.querySelector("#paper-modal-close"),
  modalCategory: document.querySelector("#modal-category"),
  modalTitle: document.querySelector("#modal-title"),
  modalAuthors: document.querySelector("#modal-authors"),
  modalTags: document.querySelector("#modal-tags"),
  modalTldr: document.querySelector("#modal-tldr"),
  modalMotivation: document.querySelector("#modal-motivation"),
  modalMethod: document.querySelector("#modal-method"),
  modalResult: document.querySelector("#modal-result"),
  modalConclusion: document.querySelector("#modal-conclusion"),
  modalAbstract: document.querySelector("#modal-abstract"),
  modalAbs: document.querySelector("#modal-abs"),
  modalPdf: document.querySelector("#modal-pdf"),
};

async function fetchJson(path) {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`Failed to fetch ${path}: ${response.status}`);
  }
  return response.json();
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
}

function initTheme() {
  const storedTheme = localStorage.getItem(THEME_KEY);
  const preferredTheme =
    storedTheme ||
    (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  applyTheme(preferredTheme);
}

function toggleTheme() {
  const nextTheme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(nextTheme);
}

function getSections(paper) {
  const sections = paper.summary_sections || {};
  return {
    tldr: sections.tldr || "",
    motivation: sections.motivation || "",
    method: sections.method || "",
    result: sections.result || "",
    conclusion: sections.conclusion || "",
  };
}

function setSiteMeta(indexPayload) {
  const site = indexPayload.site || {};
  document.title = site.title || "Dash";
  elements.siteTitle.textContent = site.title || "Dash";
  elements.siteDescription.textContent =
    site.description || "Personal daily arXiv reader with Chinese summaries.";
  elements.latestDate.textContent = indexPayload.latest_date || "-";
}

function loadUiState() {
  try {
    const parsed = JSON.parse(localStorage.getItem(UI_STATE_KEY) || "{}");
    state.searchQuery = typeof parsed.searchQuery === "string" ? parsed.searchQuery : "";
    if (Array.isArray(parsed.selectedCategories)) {
      state.selectedCategories = new Set(parsed.selectedCategories.filter(Boolean));
    } else {
      state.selectedCategories = null;
    }
  } catch {
    state.searchQuery = "";
    state.keywordFilter = "";
    state.selectedCategories = null;
  }

  state.keywordFilter = getCookie(FILTER_COOKIE_KEY) ?? "";
}

function persistUiState() {
  const payload = {
    searchQuery: state.searchQuery,
    selectedCategories: state.selectedCategories ? Array.from(state.selectedCategories) : null,
  };
  localStorage.setItem(UI_STATE_KEY, JSON.stringify(payload));
  setCookie(FILTER_COOKIE_KEY, state.keywordFilter, COOKIE_MAX_AGE);
}

function syncInputsFromState() {
  elements.searchInput.value = state.searchQuery;
  elements.filterInput.value = state.keywordFilter;
}

function normalizeText(value) {
  return String(value || "").trim().toLowerCase();
}

function parseKeywordFilter(raw) {
  return raw
    .split(FILTER_SEPARATORS)
    .map((item) => normalizeText(item))
    .filter(Boolean);
}

function setCookie(name, value, maxAgeSeconds) {
  document.cookie = `${encodeURIComponent(name)}=${encodeURIComponent(value)}; max-age=${maxAgeSeconds}; path=/; SameSite=Lax`;
}

function getCookie(name) {
  const encodedName = `${encodeURIComponent(name)}=`;
  const match = document.cookie
    .split("; ")
    .find((entry) => entry.startsWith(encodedName));
  if (!match) {
    return null;
  }
  return decodeURIComponent(match.slice(encodedName.length));
}

function buildPaperSearchIndex(paper) {
  const authors = Array.isArray(paper.authors) ? paper.authors.join(" ") : "";
  const sections = getSections(paper);
  paper._searchIndex = normalizeText(
    [
      paper.title,
      authors,
      sections.tldr,
      sections.motivation,
      sections.method,
      sections.result,
      sections.conclusion,
      paper.abstract_en,
    ].join(" ")
  );
  paper._filterIndex = normalizeText([paper.title, paper.abstract_en].join(" "));
}

function allCategoriesForCurrentPayload() {
  return state.currentPayload?.categories || state.index?.categories || [];
}

function effectiveSelectedCategories() {
  const available = allCategoriesForCurrentPayload();
  if (state.selectedCategories === null) {
    return new Set(available);
  }
  const selected = Array.from(state.selectedCategories).filter((category) => available.includes(category));
  return new Set(selected);
}

function syncCategorySelectionToAvailable() {
  if (state.selectedCategories === null) {
    return;
  }
  const available = new Set(allCategoriesForCurrentPayload());
  state.selectedCategories = new Set(
    Array.from(state.selectedCategories).filter((category) => available.has(category))
  );
}

function renderCategoryCounts(payload) {
  elements.toolbarCategoryCounts.innerHTML = "";
  const counts = payload.category_counts || {};
  const selected = effectiveSelectedCategories();
  const categories = payload.categories || state.index?.categories || Object.keys(counts);

  const allButton = document.createElement("button");
  allButton.type = "button";
  allButton.className = "category-all-toggle";
  allButton.textContent = "All";
  allButton.setAttribute("aria-pressed", selected.size === categories.length ? "true" : "false");
  allButton.addEventListener("click", toggleAllCategories);
  elements.toolbarCategoryCounts.append(allButton);

  for (const category of categories) {
    const count = counts[category] || 0;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "category-chip";
    button.dataset.category = category;
    button.dataset.selected = selected.has(category) ? "true" : "false";
    button.innerHTML = `<span class="category-chip-name">${category}</span><span class="category-chip-count">${count}</span>`;
    button.addEventListener("click", () => toggleCategory(category));
    elements.toolbarCategoryCounts.append(button);
  }

}

function getVisiblePapers() {
  const query = normalizeText(state.searchQuery);
  const keywords = parseKeywordFilter(state.keywordFilter);
  const selected = effectiveSelectedCategories();

  return state.currentPapers.filter((paper) => {
    if (!selected.has(paper.display_category)) {
      return false;
    }
    if (query && !paper._searchIndex.includes(query)) {
      return false;
    }
    if (keywords.length > 0 && !keywords.some((keyword) => paper._filterIndex.includes(keyword))) {
      return false;
    }
    return true;
  });
}

function openPaperModal(paper) {
  const sections = getSections(paper);
  elements.modalCategory.textContent = `${paper.display_category} · ${paper.published_date}`;
  elements.modalTitle.textContent = paper.title;
  elements.modalAuthors.textContent = paper.authors.join(", ");
  elements.modalTags.innerHTML = "";

  for (const category of paper.categories || []) {
    const tag = document.createElement("span");
    tag.className = "modal-tag";
    tag.textContent = category;
    elements.modalTags.append(tag);
  }

  elements.modalTldr.textContent = sections.tldr || "暂无总结。";
  elements.modalMotivation.textContent = sections.motivation || "暂无。";
  elements.modalMethod.textContent = sections.method || "暂无。";
  elements.modalResult.textContent = sections.result || "暂无。";
  elements.modalConclusion.textContent = sections.conclusion || "暂无。";
  elements.modalAbstract.textContent = paper.abstract_en || "No abstract available.";
  elements.modalAbs.href = paper.abs_url;
  elements.modalPdf.href = paper.pdf_url;
  elements.modal.showModal();
  document.body.classList.add("modal-open");
}

function closePaperModal() {
  if (elements.modal.open) {
    elements.modal.close();
  }
}

function renderPapers() {
  const papers = state.visiblePapers;
  elements.paperList.innerHTML = "";

  if (papers.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "当前筛选条件下没有论文。";
    elements.paperList.append(empty);
    return;
  }

  papers.forEach((paper) => {
    const node = elements.paperTemplate.content.firstElementChild.cloneNode(true);
    const sections = getSections(paper);
    node.querySelector(".category-badge").textContent = paper.display_category;
    node.querySelector(".paper-title").textContent = paper.title;
    node.querySelector(".paper-authors").textContent = paper.authors.join(", ");
    node.querySelector(".paper-tldr").textContent =
      sections.tldr ||
      (paper.summary_status?.startsWith("fallback")
        ? "该论文的中文总结暂时生成失败，详情中保留英文 abstract。"
        : "该论文的中文总结尚未生成。");

    node.addEventListener("click", () => openPaperModal(paper));
    elements.paperList.append(node);
  });
}

function updatePageMeta() {
  const payload = state.currentPayload;
  if (!payload) return;

  elements.currentDateTitle.textContent = state.currentDate || "No data";
  const visible = state.visiblePapers.length;
  const total = payload.paper_count || 0;
  const paperDates = (payload.paper_dates || []).slice(0, 3).join(", ");
  const datePart = paperDates ? ` · arXiv dates ${paperDates}` : "";
  elements.currentDateMeta.textContent = `${visible} / ${total} papers${datePart}`;
}

function scheduleRender() {
  const token = ++state.pendingRenderToken;
  window.requestAnimationFrame(() => {
    if (token !== state.pendingRenderToken) {
      return;
    }
    state.visiblePapers = getVisiblePapers();
    updatePageMeta();
    renderCategoryCounts(state.currentPayload || {});
    renderPapers();
  });
}

function toggleCategory(category) {
  syncCategorySelectionToAvailable();
  const available = allCategoriesForCurrentPayload();
  if (state.selectedCategories === null) {
    state.selectedCategories = new Set(available);
  }

  if (state.selectedCategories.has(category)) {
    state.selectedCategories.delete(category);
  } else {
    state.selectedCategories.add(category);
  }

  persistUiState();
  scheduleRender();
}

function toggleAllCategories() {
  const available = allCategoriesForCurrentPayload();
  const selected = effectiveSelectedCategories();
  if (selected.size === available.length && available.length > 0) {
    state.selectedCategories = new Set();
  } else {
    state.selectedCategories = new Set(available);
  }
  persistUiState();
  scheduleRender();
}

function configureDateInput(indexPayload) {
  const dates = indexPayload.available_dates || [];
  if (dates.length === 0) {
    elements.dateInput.value = "";
    elements.dateInput.disabled = true;
    elements.dateHint.textContent = "No local snapshots yet.";
    return;
  }

  const ascendingDates = [...dates].sort();
  elements.dateInput.disabled = false;
  elements.dateInput.min = ascendingDates[0];
  elements.dateInput.max = ascendingDates[ascendingDates.length - 1];
  elements.dateHint.textContent = `${dates.length} local snapshot${dates.length > 1 ? "s" : ""}`;
}

async function loadDay(day) {
  const payload = await fetchJson(`./data/${day}.json`);
  state.currentDate = day;
  state.currentPayload = payload;
  state.currentPapers = (payload.papers || []).map((paper) => {
    buildPaperSearchIndex(paper);
    return paper;
  });
  syncCategorySelectionToAvailable();
  elements.dateInput.value = day;
  scheduleRender();
}

async function init() {
  initTheme();
  loadUiState();
  syncInputsFromState();

  try {
    const indexPayload = await fetchJson("./data/index.json");
    state.index = indexPayload;
    setSiteMeta(indexPayload);
    configureDateInput(indexPayload);

    const latestDate = indexPayload.latest_date || indexPayload.available_dates?.[0];
    if (!latestDate) {
      elements.currentDateTitle.textContent = "No data";
      elements.currentDateMeta.textContent = "Run the pipeline to generate the first snapshot.";
      renderPapers();
      return;
    }

    await loadDay(latestDate);
  } catch (error) {
    elements.currentDateTitle.textContent = "Load failed";
    elements.currentDateMeta.textContent = error instanceof Error ? error.message : String(error);
    renderPapers();
  }
}

elements.dateInput.addEventListener("change", async (event) => {
  const nextDate = event.target.value;
  if (!nextDate || nextDate === state.currentDate) {
    return;
  }
  if (!state.index?.available_dates?.includes(nextDate)) {
    elements.dateInput.value = state.currentDate || "";
    return;
  }
  await loadDay(nextDate);
});

elements.searchInput.addEventListener("input", (event) => {
  state.searchQuery = event.target.value;
  persistUiState();
  scheduleRender();
});

elements.filterInput.addEventListener("input", (event) => {
  state.keywordFilter = event.target.value;
  persistUiState();
  scheduleRender();
});

elements.themeToggle.addEventListener("click", toggleTheme);
elements.modalClose.addEventListener("click", () => closePaperModal());
elements.modal.addEventListener("close", () => {
  document.body.classList.remove("modal-open");
});
elements.modal.addEventListener("click", (event) => {
  const content = elements.modal.querySelector(".paper-modal-content");
  const closeButton = elements.modalClose;
  if (!content) {
    return;
  }
  if (!content.contains(event.target) && event.target !== closeButton) {
    closePaperModal();
  }
});

init();
