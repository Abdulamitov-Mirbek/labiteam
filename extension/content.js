// Плашка «Сигнал.kg» в объявлении. Вся разметка собирается через createElement
// и textContent — без innerHTML, чтобы на чужом сайте не было XSS. Стили живут
// в Shadow DOM и не конфликтуют со стилями страницы.
(function () {
  "use strict";
  const { extractCoords, describe } = window.SignalKg;
  const COLORS = { green: "#1f9d55", yellow: "#e3a92b", red: "#d93636", none: "#8b919c" };
  const STYLE = `
    :host { all: initial; }
    .card { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:12px 0; padding:10px 14px;
            border:1px solid #e5e7eb; border-left:6px solid var(--c); border-radius:10px; background:#fff;
            font:14px/1.45 Inter, system-ui, -apple-system, "Segoe UI", sans-serif; color:#111827;
            box-shadow:0 1px 3px rgba(0,0,0,.06); }
    .card.floating { position:fixed; left:16px; bottom:16px; z-index:2147483647; max-width:460px; margin:0; }
    .text { flex:1; min-width:220px; }
    .brand { font-weight:700; margin-right:6px; }
    .meta { color:#6b7280; font-size:12px; margin-top:2px; }
    button { font:inherit; font-size:13px; font-weight:500; border:1px solid #111827; background:#fff; color:#111827;
             border-radius:7px; padding:6px 10px; min-height:34px; cursor:pointer; }
    button:focus-visible { outline:3px solid #93b4ff; outline-offset:2px; }
    button:disabled { opacity:.5; cursor:default; }
  `;

  let host = null;
  let currentUrl = "";

  function isListing(url) {
    return /house\.kg\/details\//.test(url) || /lalafo\.kg\/.+-id-\d+/.test(url);
  }

  function send(message) {
    return new Promise(resolve => {
      try {
        chrome.runtime.sendMessage(message, reply => resolve(reply || { ok: false, status: 0 }));
      } catch (e) {
        resolve({ ok: false, status: 0 });
      }
    });
  }

  async function findCoords() {
    const inPage = extractCoords(document.documentElement.outerHTML, location.href);
    if (inPage) return inPage;
    // lalafo — одностраничное приложение: после перехода по ссылке в DOM остаются
    // данные первой страницы, поэтому берём свежий HTML объявления.
    try {
      const response = await fetch(location.href, { credentials: "same-origin" });
      return extractCoords(await response.text(), location.href);
    } catch (e) {
      return null;
    }
  }

  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  let card = null;

  function mount(newCard) {
    card = newCard;
    host = document.createElement("signal-kg-badge");
    const shadow = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    shadow.append(style, card);
    place();
  }

  function place() {
    const title = document.querySelector("h1");
    card.classList.toggle("floating", !(title && title.parentElement));
    if (title && title.parentElement) title.insertAdjacentElement("afterend", host);
    else document.body.append(host);
  }

  // house.kg (React) через ~1 с после загрузки заменяет весь корневой блок страницы,
  // и плашка исчезает вместе с ним. Возвращаем её под новый заголовок.
  new MutationObserver(() => {
    if (host && !host.isConnected && location.href === currentUrl) place();
  }).observe(document.documentElement, { childList: true, subtree: true });

  function badge(color, text, meta, coords) {
    const card = el("div", null, "card");
    card.style.setProperty("--c", COLORS[color] || COLORS.none);
    const body = el("div", null, "text");
    const line = el("div");
    line.append(el("span", "📶 Сигнал.kg", "brand"), document.createTextNode(text));
    body.append(line);
    if (meta) body.append(el("div", meta, "meta"));
    card.append(body);
    if (coords) {
      const button = el("button", "Здесь нет связи");
      button.addEventListener("click", async () => {
        button.disabled = true;
        const reply = await send({ type: "complaint", lat: coords.lat, lon: coords.lon });
        button.textContent = reply.ok ? "Спасибо, жалоба передана оператору"
          : reply.status === 429 ? "Слишком много жалоб, попробуйте позже"
          : "Не удалось отправить";
        if (!reply.ok) setTimeout(() => { button.disabled = false; button.textContent = "Здесь нет связи"; }, 4000);
      });
      card.append(button);
    }
    return card;
  }

  async function run() {
    currentUrl = location.href;
    if (host) { host.remove(); host = null; }
    if (!isListing(currentUrl)) return;

    const coords = await findCoords();
    if (location.href !== currentUrl) return;  // пока искали, пользователь ушёл со страницы
    if (!coords) return;  // у объявления нет координат — плашку не показываем

    const reply = await send({ type: "signal", lat: coords.lat, lon: coords.lon });
    if (location.href !== currentUrl) return;
    if (reply.ok && reply.body && reply.body.status === "ok") {
      const s = reply.body;
      const meta = `Медиана замеров в квадрате 200×200 м · ${s.samples} замеров` +
        (s.estimated ? " · оценка по соседнему квадрату" : "");
      mount(badge(s.color, describe(s), meta, coords));
    } else if (reply.ok) {
      mount(badge("none", "Для этого дома пока нет замеров", null, coords));
    } else if (reply.status === 422) {
      mount(badge("none", "Дом вне района, где есть замеры связи", null, null));
    } else {
      mount(badge("none", "Сервис оценки связи недоступен", null, null));
    }
  }

  run();
  // Одностраничные сайты меняют адрес без перезагрузки — следим за ним.
  setInterval(() => { if (location.href !== currentUrl) run(); }, 1000);
})();
