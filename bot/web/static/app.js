// Мини-приложение «Афиша и места». Без сборщиков и фреймворков: один файл, только DOM API.
// Текст из данных вставляется через textContent — никакого innerHTML с внешними строками.
"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const view = document.getElementById("view");
const SEAT_GAP = 4;
const ROW_SAGITTA = 10; // насколько края ряда загибаются к сцене, px

const state = { shows: [], show: null, map: null, selected: new Set(), hold: null, timer: null, me: null };

// ---------- утилиты ----------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(`/api${path}`, {
    method,
    headers: { "Authorization": `tma ${tg.initData}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error((data.error && data.error.message) || "Сервер не ответил. Попробуйте ещё раз.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function alertUser(message) {
  if (tg.showAlert) tg.showAlert(message); else window.alert(message);
}

function haptic(kind) {
  if (!tg.HapticFeedback) return;
  if (kind === "select") tg.HapticFeedback.selectionChanged();
  else tg.HapticFeedback.notificationOccurred(kind);
}

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function seatLabel(key) {
  const [row, number] = key.slice(1).split("-s");
  return `ряд ${row}, место ${number}`;
}

function setScreen(title, subtitle, ...content) {
  view.replaceChildren(el("h1", { text: title }), subtitle ? el("p", { class: "sub", text: subtitle }) : null, ...content);
  window.scrollTo(0, 0);
}

function stopTimer() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

function mainButton(text, handler) {
  tg.MainButton.offClick(mainButton.handler || (() => {}));
  mainButton.handler = handler;
  if (!text) { tg.MainButton.hide(); return; }
  tg.MainButton.setText(text);
  tg.MainButton.onClick(handler);
  tg.MainButton.show();
}

function backButton(handler) {
  tg.BackButton.offClick(backButton.handler || (() => {}));
  backButton.handler = handler;
  if (handler) { tg.BackButton.onClick(handler); tg.BackButton.show(); } else tg.BackButton.hide();
}

// ---------- афиша ----------
async function showList() {
  stopTimer();
  state.show = null;
  mainButton(null);
  backButton(null);
  try {
    const data = await api("/shows");
    document.getElementById("venue").textContent = data.venue;
    state.shows = data.shows;
  } catch (error) {
    setScreen("Афиша", null, el("p", { class: "error", text: error.message }));
    return;
  }
  if (!state.shows.length) {
    setScreen("Афиша", null, el("div", { class: "note" },
      el("strong", { text: "Ближайших сеансов нет" }), "Загляните позже — афиша обновляется."));
    return;
  }
  const template = document.getElementById("tpl-show");
  const cards = state.shows.map((show) => {
    const card = template.content.firstElementChild.cloneNode(true);
    card.querySelector(".show-date").textContent = show.date;
    card.querySelector(".show-time").textContent = show.time;
    card.querySelector(".show-title").textContent = show.title;
    card.querySelector(".show-desc").textContent = show.description;
    card.querySelector(".show-free").textContent =
      show.free ? `${show.free} ${plural(show.free, "место", "места", "мест")}` : "мест нет";
    card.disabled = show.free === 0;
    card.addEventListener("click", () => openShow(show.id));
    return card;
  });
  setScreen("Афиша", "Выберите сеанс — схема зала откроется следующим шагом.", ...cards);
}

// ---------- зал ----------
async function openShow(showId, keepSelection = false) {
  try {
    state.map = await api(`/shows/${showId}/seats`);
  } catch (error) {
    alertUser(error.message);
    return showList();
  }
  state.show = state.shows.find((show) => show.id === showId) || { id: showId, title: state.map.show.title };
  state.hold = state.map.hold;
  const available = new Set();
  for (const row of state.map.rows) for (const seat of row.seats) {
    if (seat && seat.state !== "taken") available.add(seat.key);
  }
  if (state.hold) state.selected = new Set(state.hold.seats);
  else if (keepSelection) state.selected = new Set([...state.selected].filter((key) => available.has(key)));
  else state.selected = new Set();
  backButton(showList);
  renderHall();
}

function priceOf(key) {
  for (const row of state.map.rows) for (const seat of row.seats) {
    if (seat && seat.key === key) return state.map.zones[seat.zone].price;
  }
  return 0;
}

function renderHall() {
  const map = state.map;
  const maxCells = Math.max(...map.rows.map((row) => row.seats.length));
  const usable = Math.min(window.innerWidth, 560) - 16 - 2 * (18 + 6);
  const seatSize = Math.max(14, Math.min(26, Math.floor((usable - (maxCells - 1) * SEAT_GAP) / maxCells)));

  const stage = el("div", { class: "stage", "aria-hidden": "true" });
  stage.innerHTML = '<svg viewBox="0 0 360 34" preserveAspectRatio="none"><path d="M6 30 Q180 -10 354 30"'
    + ' fill="none" stroke="currentColor" stroke-width="2" opacity=".55"/></svg>'; // статичная разметка, не данные
  stage.append(el("span", { class: "stage-label", text: "Сцена" }));

  const rows = el("div", { class: "rows", role: "group", "aria-label": "Схема зала" });
  for (const row of map.rows) {
    const seats = el("div", { class: "seats" });
    const center = (row.seats.length - 1) / 2;
    row.seats.forEach((seat, index) => {
      const t = center ? (index - center) / center : 0;
      const lift = `${(-ROW_SAGITTA * t * t).toFixed(1)}px`;
      if (!seat) {
        seats.append(el("span", { class: "aisle", "aria-hidden": "true" }));
        return;
      }
      const zone = map.zones[seat.zone];
      const taken = seat.state === "taken";
      const button = el("button", {
        type: "button", class: "seat", "data-zone": seat.zone, disabled: taken,
        "aria-pressed": String(state.selected.has(seat.key)),
        "aria-label": `Ряд ${row.label}, место ${seat.n}, ${zone.name}, ${zone.price} звёзд${taken ? ", занято" : ""}`,
        title: `Ряд ${row.label}, место ${seat.n} · ${zone.price} ⭐`,
        onclick: () => toggleSeat(seat.key, button),
      });
      button.style.setProperty("--lift", lift);
      seats.append(button);
    });
    rows.append(el("div", { class: "row" },
      el("span", { class: "row-label", text: row.label }), seats, el("span", { class: "row-label", text: row.label })));
  }

  const hall = el("section", { class: "hall" }, stage, rows);
  hall.style.setProperty("--seat", `${seatSize}px`);

  const legend = el("div", { class: "legend" },
    ...Object.entries(map.zones).map(([code, zone]) =>
      el("span", { class: `z-${code}` }, el("i"), `${zone.name} · ${zone.price} ⭐`)),
    el("span", { class: "z-mine" }, el("i"), "Ваш выбор"),
    el("span", { class: "z-taken" }, el("i"), "Занято"));

  const time = state.show.date ? `${state.show.date}, ${state.show.time} · ${map.hall}` : map.hall;
  setScreen(map.show.title, time, hall, legend, el("section", { class: "summary", id: "summary" }));
  renderSummary();
}

function toggleSeat(key, button) {
  if (state.selected.has(key)) state.selected.delete(key);
  else if (state.selected.size >= state.map.max_seats) {
    alertUser(`За один раз можно выбрать до ${state.map.max_seats} мест.`);
    return;
  } else state.selected.add(key);
  button.setAttribute("aria-pressed", String(state.selected.has(key)));
  haptic("select");
  renderSummary();
}

function sameAsHold() {
  if (!state.hold) return false;
  const held = new Set(state.hold.seats);
  return held.size === state.selected.size && [...state.selected].every((key) => held.has(key));
}

function renderSummary() {
  const box = document.getElementById("summary");
  stopTimer();
  if (!state.selected.size) {
    box.replaceChildren(el("p", { class: "summary-empty", text: "Нажмите на свободные кресла на схеме." }));
    mainButton(null);
    return;
  }
  const keys = [...state.selected].sort((a, b) => a.localeCompare(b, "ru", { numeric: true }));
  const total = keys.reduce((sum, key) => sum + priceOf(key), 0);
  const count = keys.length;
  const timer = el("p", { class: "timer" });
  box.replaceChildren(
    el("ul", { class: "chips" }, ...keys.map((key) => el("li", { class: "chip", text: seatLabel(key) }))),
    el("div", { class: "total" },
      el("span", { text: `${count} ${plural(count, "место", "места", "мест")}` }), el("strong", { text: `${total} ⭐` })),
    timer);

  if (sameAsHold()) {
    const expires = new Date(state.hold.expires_at).getTime();
    const tick = () => {
      const left = Math.max(0, Math.round((expires - Date.now()) / 1000));
      timer.textContent = `Места держатся за вами ещё ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      timer.classList.toggle("urgent", left < 60);
      if (!left) {
        stopTimer();
        alertUser("Время брони вышло — места снова свободны.");
        openShow(state.show.id, true);
      }
    };
    tick();
    state.timer = setInterval(tick, 1000);
  } else {
    timer.textContent = "После нажатия «Оплатить» места закрепятся за вами на время оплаты.";
  }
  mainButton(`Оплатить ${total} ⭐`, checkout);
}

async function checkout() {
  tg.MainButton.showProgress(false);
  try {
    if (!sameAsHold()) {
      const result = await api(`/shows/${state.show.id}/hold`, { method: "POST", body: { seats: [...state.selected] } });
      state.hold = result.hold;
    }
    const { link } = await api(`/holds/${state.hold.id}/invoice`, { method: "POST" });
    tg.openInvoice(link, (status) => {
      if (status === "paid") return showPaid();
      if (status === "failed") alertUser("Оплата не прошла. Места пока держатся за вами — можно попробовать снова.");
      renderSummary();
    });
  } catch (error) {
    haptic("error");
    alertUser(error.message);
    if (error.status === 409) await openShow(state.show.id, true);
  } finally {
    tg.MainButton.hideProgress();
  }
}

function showPaid() {
  stopTimer();
  haptic("success");
  mainButton(null);
  backButton(showList);
  setScreen("Билеты ваши", null,
    el("div", { class: "note" },
      el("strong", { text: "Оплата прошла" }),
      "QR-коды билетов придут в чат с ботом через несколько секунд. Покажите их на входе."),
    el("button", { type: "button", class: "button", text: "К афише", onclick: showList }));
}

// ---------- мои билеты и проверка на входе ----------
async function showTickets() {
  stopTimer();
  mainButton(null);
  backButton(showList);
  let data;
  try {
    data = await api("/me");
  } catch (error) {
    setScreen("Мои билеты", null, el("p", { class: "error", text: error.message }));
    return;
  }
  if (!data.tickets.length) {
    setScreen("Мои билеты", null, el("div", { class: "note" },
      el("strong", { text: "Билетов пока нет" }), "Выберите сеанс в афише — билеты появятся здесь и в чате."),
      el("button", { type: "button", class: "button", text: "Открыть афишу", onclick: showList }));
    return;
  }
  setScreen("Мои билеты", "QR-коды — в чате с ботом (команда /tickets).",
    ...data.tickets.map((ticket) => el("article", { class: `ticket${ticket.used ? " used" : ""}` },
      el("span", { class: "ticket-title", text: ticket.title }),
      el("span", { class: "ticket-when", text: `${ticket.date} · ${ticket.time}` }),
      el("span", { class: "ticket-seat", text: ticket.used ? `${ticket.seat} · использован` : ticket.seat }))));
}

function scanTickets() {
  tg.showScanQrPopup({ text: "Наведите камеру на QR-код билета" }, (code) => {
    api("/staff/check", { method: "POST", body: { code } })
      .then((result) => { haptic(result.ok ? "success" : "error"); alertUser(result.message); })
      .catch((error) => alertUser(error.message));
    return true; // закрыть сканер после первого кода
  });
}

// ---------- старт ----------
async function start() {
  if (!tg || !tg.initData) {
    view.replaceChildren(el("div", { class: "note" },
      el("strong", { text: "Откройте из Telegram" }), "Это мини-приложение работает внутри чата с ботом."));
    return;
  }
  document.documentElement.dataset.tg = "1";
  tg.ready();
  tg.expand();
  document.getElementById("nav-tickets").addEventListener("click", showTickets);
  const scan = document.getElementById("nav-scan");
  scan.addEventListener("click", scanTickets);
  api("/me").then((me) => { state.me = me; scan.hidden = !me.is_staff; }).catch(() => {});
  await showList();
}

start();
