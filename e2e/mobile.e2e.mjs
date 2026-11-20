// Сквозной автотест мобильной версии (PWA /app/) в настоящем Chrome.
//
//   node --test e2e/
//
// Тест сам поднимает сервер на свободном порту с временными данными (жалобы,
// сигналы, проверенные квартиры) и Chrome без окна с экраном телефона 390×844.
// Chrome управляется через протокол DevTools — без сторонних библиотек.
// Нужны: Python из .venv с зависимостями проекта и Google Chrome / Chromium.
// Путь к браузеру можно задать переменной CHROME, к Python — PYTHON.
// Скриншоты экранов: E2E_SHOTS=<папка>.
import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const TMP = mkdtempSync(join(tmpdir(), "signalkg-e2e-"));
const SHOTS = process.env.E2E_SHOTS;
const sleep = ms => new Promise(r => setTimeout(r, ms));

// Точки района замеров (данные data/07_coverage).
const IN_AREA = { lat: 42.8687, lon: 74.604 };     // X017_Y015, средняя связь
const WEAK = { lat: 42.8618, lon: 74.5728 };       // X005_Y012, слабая связь
const OUTSIDE = { lat: 42.83, lon: 74.62 };
const FLATS = [
  { url: "https://www.house.kg/details/e2e-good-1", site: "house.kg", title: "2-комн. кв., Мед. академия (тест)",
    lat: 42.8416, lon: 74.6041, cell_id: "X017_Y000", color: "green", quality: "хорошая", median: -89.35, checked: "2026-11-21T00:00:00+00:00" },
  { url: "https://www.house.kg/details/e2e-mid-2", site: "house.kg", title: "3-комн. кв., Раззакова (тест)",
    lat: 42.8687, lon: 74.604, cell_id: "X017_Y015", color: "yellow", quality: "средняя", median: -98.3, checked: "2026-11-21T00:00:00+00:00" },
  { url: "https://lalafo.kg/bishkek/ads/e2e-weak-id-3", site: "lalafo.kg", title: "1-комн. кв., Ахунбаева (тест)",
    lat: 42.8618, lon: 74.5728, cell_id: "X005_Y012", color: "red", quality: "слабая", median: -116.8, checked: "2026-11-21T00:00:00+00:00" },
];

function freePort() {
  return new Promise(resolve => {
    const srv = createServer().listen(0, "127.0.0.1", () => { const { port } = srv.address(); srv.close(() => resolve(port)); });
  });
}

function findChrome() {
  const candidates = [process.env.CHROME, "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"].filter(Boolean);
  for (const c of candidates) {
    if (c.includes("/") || c.includes("\\")) { if (existsSync(c)) return c; continue; }
    try { return execFileSync("which", [c]).toString().trim(); } catch (e) { /* нет такого */ }
  }
  throw new Error("Не найден Chrome: задайте путь в переменной CHROME");
}

function findPython() {
  if (process.env.PYTHON) return process.env.PYTHON;
  for (const p of [join(ROOT, ".venv", "bin", "python"), join(ROOT, ".venv", "Scripts", "python.exe")]) if (existsSync(p)) return p;
  return "python3";
}

async function waitFor(check, what, timeout = 15000) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try { last = await check(); if (last) return last; } catch (e) { last = e; }
    await sleep(150);
  }
  throw new Error(`Не дождались: ${what} (последнее значение: ${last})`);
}

/* --- Chrome по протоколу DevTools --- */
class Browser {
  async start(chromePath) {
    this.port = await freePort();
    this.proc = spawn(chromePath, ["--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
      `--user-data-dir=${join(TMP, "chrome")}`, `--remote-debugging-port=${this.port}`, "about:blank"], { stdio: "ignore" });
    const info = await waitFor(async () => (await fetch(`http://127.0.0.1:${this.port}/json/version`)).json(), "Chrome DevTools");
    this.ws = new WebSocket(info.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { this.ws.onopen = resolve; this.ws.onerror = reject; });
    this.id = 0; this.wait = {}; this.listeners = [];
    this.ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.id && this.wait[m.id]) { this.wait[m.id](m); delete this.wait[m.id]; }
      else if (m.method) this.listeners.forEach(l => l(m));
    };
  }
  send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      this.wait[id] = m => m.error ? reject(new Error(`${method}: ${m.error.message}`)) : resolve(m.result);
      this.ws.send(JSON.stringify({ id, method, params, sessionId }));
    });
  }
  async page(origin) {
    await this.send("Browser.grantPermissions", { origin, permissions: ["geolocation"] });
    const { targetId } = await this.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await this.send("Target.attachToTarget", { targetId, flatten: true });
    return new Page(this, sessionId, targetId);
  }
  stop() { try { this.ws?.close(); } catch (e) { /* уже закрыт */ } this.proc?.kill(); }
}

class Page {
  constructor(browser, sessionId, targetId) {
    Object.assign(this, { browser, sessionId, targetId, errors: [] });
    browser.listeners.push(m => {
      if (m.sessionId !== sessionId) return;
      if (m.method === "Runtime.exceptionThrown") this.errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    });
  }
  send(method, params) { return this.browser.send(method, params, this.sessionId); }
  async setup({ width = 390, height = 844, geo } = {}) {
    await this.send("Runtime.enable");
    await this.send("Page.enable");
    await this.send("Network.enable");
    await this.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 2, mobile: true });
    await this.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
    await this.send("Emulation.setUserAgentOverride", {
      userAgent: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0 Mobile Safari/537.36" });
    if (geo) await this.geo(geo);
    return this;
  }
  geo({ lat, lon, accuracy = 12 }) {
    return this.send("Emulation.setGeolocationOverride", { latitude: lat, longitude: lon, accuracy });
  }
  async open(url) {
    await this.send("Page.navigate", { url });
    await waitFor(() => this.eval("document.readyState === 'complete'"), "загрузка " + url);
  }
  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
  text(selector) { return this.eval(`document.querySelector(${JSON.stringify(selector)})?.textContent?.trim() ?? null`); }
  click(selector) {
    const sel = JSON.stringify(selector);
    return this.eval(`(() => { const el = document.querySelector(${sel}); if (!el) throw new Error("нет элемента " + ${sel}); el.click(); return true; })()`);
  }
  visible(selector) { return this.eval(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden"; })()`); }
  offline(on) { return this.send("Network.emulateNetworkConditions", { offline: on, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }); }
  async shot(name) {
    if (!SHOTS) return;
    mkdirSync(SHOTS, { recursive: true });
    const { data } = await this.send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(SHOTS, name + ".png"), Buffer.from(data, "base64"));
  }
  close() { return this.browser.send("Target.closeTarget", { targetId: this.targetId }); }
}

/* --- сервер проекта на свободном порту --- */
let server, base, browser;
const api = async path => (await fetch(base + path)).json();

before(async () => {
  writeFileSync(join(TMP, "listings.json"), JSON.stringify(FLATS));
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  server = spawn(findPython(), ["-m", "server", "--port", String(port)], {
    cwd: ROOT, stdio: "ignore",
    env: { ...process.env, COMPLAINTS_FILE: join(TMP, "complaints.jsonl"), REPORTS_FILE: join(TMP, "reports.jsonl"),
      LISTINGS_FILE: join(TMP, "listings.json"), DGIS_KEY: "" },
  });
  await waitFor(async () => (await fetch(base + "/health")).ok, "сервер проекта", 30000);
  browser = new Browser();
  await browser.start(findChrome());
});

after(() => { browser?.stop(); server?.kill(); });

/* --- сценарии --- */
describe("мобильная версия /app/", () => {
  test("открывается на карте: покрытие, квартиры, список хороших", async () => {
    const page = await (await browser.page(base)).setup();
    await page.open(base + "/app/");
    assert.equal(await page.eval(`document.querySelector("nav button.on")?.dataset.s`), "map");
    const cells = (await api("/api/cells")).cells.length;
    // Квадраты рисуются на canvas или в SVG — считаем слои Leaflet.
    await waitFor(() => page.eval(`document.querySelectorAll(".flat").length === ${FLATS.length}`), "домики квартир");
    const polygons = await page.eval(`(() => { let n = 0; for (const p of document.querySelectorAll("#amap path.leaflet-interactive")) n++; return n; })()`);
    assert.ok(polygons >= cells, `на карте ${polygons} квадратов, ожидалось ${cells}`);
    assert.equal(await page.text("#sh-title"), "Квартиры с хорошей связью · 1");
    assert.match(await page.text("#sh-count"), /всего проверено: 3/);
    const first = await page.text("#flats li .t");
    assert.equal(first, FLATS[0].title, "первой в списке идёт квартира с хорошей связью");
    await page.shot("01-map");
    const overlap = await page.eval(`document.querySelector(".leaflet-control-attribution").getBoundingClientRect().bottom - document.querySelector("#sheet").getBoundingClientRect().top`);
    assert.ok(overlap <= 1, `подпись карты налезает на список на ${overlap} px`);
    assert.deepEqual(page.errors, []);
    await page.close();
  });

  test("нажатие на квартиру в списке открывает карточку со ссылкой", async () => {
    const page = await (await browser.page(base)).setup();
    await page.open(base + "/app/");
    await waitFor(() => page.eval(`document.querySelectorAll("#flats li button").length === 3`), "список квартир");
    await page.click("#flats li button");
    const popup = await waitFor(() => page.text(".leaflet-popup-content"), "карточка квартиры");
    assert.match(popup, /Мед\. академия/);
    assert.match(popup, /Связь: хорошая/);
    assert.equal(await page.eval(`document.querySelector(".leaflet-popup-content a").href`), FLATS[0].url);
    assert.equal(await page.eval(`document.querySelector(".leaflet-popup-content a").rel`), "noopener noreferrer");
    await page.shot("02-flat-popup");
    // Панель свернулась — подпись карты и кнопка «Где я» не налезают на неё.
    await waitFor(() => page.eval(`(() => {
      const sheet = document.querySelector("#sheet").getBoundingClientRect().top;
      const attr = document.querySelector(".leaflet-control-attribution").getBoundingClientRect().bottom;
      const loc = document.querySelector("#locate").getBoundingClientRect().bottom;
      return attr <= sheet + 1 && loc <= sheet + 1;
    })()`), "подпись карты и «Где я» над панелью");
    await page.close();
  });

  test("переключатели слоёв прячут и возвращают квартиры", async () => {
    const page = await (await browser.page(base)).setup();
    await page.open(base + "/app/");
    await waitFor(() => page.eval(`document.querySelectorAll(".flat").length === 3`), "домики");
    await page.click(`#mtools [data-layer="flats"]`);
    assert.equal(await page.eval(`document.querySelectorAll(".flat").length`), 0);
    assert.equal(await page.eval(`document.querySelector('#mtools [data-layer="flats"]').getAttribute("aria-pressed")`), "false");
    await page.click(`#mtools [data-layer="flats"]`);
    assert.equal(await page.eval(`document.querySelectorAll(".flat").length`), 3);
    await page.close();
  });

  test("«Связь здесь» по GPS показывает медиану своего квадрата", async () => {
    const page = await (await browser.page(base)).setup({ geo: IN_AREA });
    const expected = await api(`/api/signal?lat=${IN_AREA.lat}&lon=${IN_AREA.lon}`);
    await page.open(base + "/app/");
    await page.click(`nav button[data-s="here"]`);
    await waitFor(async () => (await page.text("#cell")) === expected.cell_id, `квадрат ${expected.cell_id}`);
    assert.equal(await page.text("#value"), String(Math.round(expected.median)).replace("-", "−"));
    assert.match(await page.text("#quality"), /Средний сигнал/);
    assert.equal(await page.text("#gps"), "±12 м");
    await page.shot("03-here");
    assert.deepEqual(page.errors, []);
    await page.close();
  });

  test("вне района замеров: замер жителя принимается и показывается на кольце", async () => {
    const page = await (await browser.page(base)).setup({ geo: OUTSIDE });
    await page.open(base + "/app/");
    await page.click(`nav button[data-s="here"]`);
    await waitFor(async () => /Замеров оператора здесь нет/.test(await page.text("#hint")), "подсказка про замеры жителей");
    assert.equal(await page.text("#value"), "—");
    assert.match(await page.text("#quality"), /нет данных/);
    assert.equal(await page.eval(`document.querySelector("#sendmeasure").disabled`), true, "без палочек и дБм отправлять нечего");
    await page.click(`#bars [data-bars="1"]`);
    await page.click("#sendmeasure");
    const m = await waitFor(async () => (await api("/api/measurements")).measurements.find(x => x.bars === 1 && !x.cell_id), "замер на сервере");
    assert.equal(m.rsrp_dbm, null);
    assert.equal(m.color, "red");
    // Тот же экран после обновления показывает замер жителей.
    await page.click("#refresh");
    await waitFor(async () => /Замеры жителей: слабый сигнал \(1\)/.test(await page.text("#quality")), "«Замеры жителей: слабый сигнал (1)»");
    await page.shot("06-outside-measure");
    assert.deepEqual(page.errors, []);
    await page.close();
  });

  test("сила связи из приложения видна на странице оператора", async () => {
    const page = await (await browser.page(base)).setup({ geo: IN_AREA });
    await page.open(base + "/app/");
    await page.click(`nav button[data-s="here"]`);
    await waitFor(async () => (await page.text("#cell")) === "X017_Y015", "квадрат X017_Y015");
    await page.eval(`(() => { const i = document.querySelector("#dbm"); i.value = "500"; i.dispatchEvent(new Event("input")); })()`);
    assert.equal(await page.eval(`document.querySelector("#sendmeasure").disabled`), true, "500 дБм не бывает");
    await page.eval(`(() => { const i = document.querySelector("#dbm"); i.value = "112"; i.dispatchEvent(new Event("input")); })()`);
    await page.click(`#bars [data-bars="1"]`);
    await page.click(`#nets [data-net="4G"]`);
    await page.click("#sendmeasure");
    await waitFor(async () => /отправлен оператору/.test(await page.text("#m-hint")), "замер отправлен");
    const m = (await api("/api/measurements")).measurements.at(-1);
    assert.deepEqual([m.cell_id, m.rsrp_dbm, m.bars, m.network, m.color], ["X017_Y015", -112, 1, "4G", "red"]);
    await page.shot("07-measure");
    assert.deepEqual(page.errors, []);
    await page.close();

    // Страница оператора: вкладка «Замеры», сравнение с картой и плашка на карте.
    const panel = await (await browser.page(base)).setup({ width: 1400, height: 900 });
    await panel.send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
    await panel.open(base + "/#speed");
    await waitFor(async () => /X017_Y015/.test(await panel.text("#s-table")), "замер в таблице оператора");
    const row = await panel.text("#s-table");
    assert.match(row, /−112 дБм/);
    assert.match(row, /Хуже, чем на карте/, "медиана квадрата −98 — житель видит хуже");
    assert.ok(await panel.eval(`[...document.querySelectorAll(".spd span")].some(e => e.textContent === "−112")`), "плашка −112 на карте оператора");
    await panel.shot("08-operator-measure");
    assert.deepEqual(panel.errors, []);
    await panel.close();
  });

  test("жалоба «Здесь нет связи» доходит до сервера", async () => {
    const page = await (await browser.page(base)).setup({ geo: WEAK });
    const before = (await api("/api/complaints")).complaints.length;
    await page.open(base + "/app/");
    await page.click(`nav button[data-s="here"]`);
    await waitFor(async () => (await page.text("#cell")) === "X005_Y012", "квадрат X005_Y012");
    await page.click("#complain");
    const list = await waitFor(async () => {
      const c = (await api("/api/complaints")).complaints;
      return c.length === before + 1 ? c : null;
    }, "жалоба на сервере");
    assert.equal(list.at(-1).source, "app");
    assert.equal(list.at(-1).cell_id, "X005_Y012");
    await page.close();
  });

  test("без сети жалоба ждёт в очереди и уходит, когда сеть появилась", async () => {
    const page = await (await browser.page(base)).setup({ geo: IN_AREA });
    await page.open(base + "/app/");
    await page.click(`nav button[data-s="here"]`);
    await waitFor(async () => (await page.text("#cell")) === "X017_Y015", "квадрат X017_Y015");
    const before = (await api("/api/complaints")).complaints.length;
    await page.offline(true);
    await waitFor(async () => /Нет сети/.test(await page.text("#net")), "значок «Нет сети»");
    await page.click("#complain");
    await page.click(`nav button[data-s="queue"]`);
    await waitFor(async () => (await page.text("#qcount")) === "1", "1 жалоба в очереди");
    assert.match(await page.text("#reports"), /в очереди/);
    await page.shot("04-queue-offline");
    assert.equal((await api("/api/complaints")).complaints.length, before, "без сети на сервер ничего не ушло");
    await page.offline(false);
    await waitFor(async () => (await page.text("#qcount")) === "0", "очередь опустела");
    await waitFor(async () => (await api("/api/complaints")).complaints.length === before + 1, "жалоба на сервере");
    assert.match(await page.text("#reports"), /отправлена/);
    await page.close();
  });

  test("?demo=1 показывает демо-дом и ничего не запоминает", async () => {
    const page = await (await browser.page(base)).setup();
    await page.open(base + "/app/?demo=1");
    await page.click(`nav button[data-s="here"]`);
    await waitFor(async () => (await page.text("#cell")) === "X017_Y015", "демо-квадрат");
    assert.equal(await page.eval(`localStorage.getItem("sg.demoPoint")`), null);
    await page.close();
  });

  test("настройки, манифест и service worker — приложение устанавливается", async () => {
    const page = await (await browser.page(base)).setup();
    await page.open(base + "/app/");
    await page.click(`nav button[data-s="settings"]`);
    assert.ok(await page.visible("#s-settings"));
    const manifest = await (await fetch(base + "/app/manifest.webmanifest")).json();
    assert.equal(manifest.display, "standalone");
    assert.ok(manifest.icons.some(i => i.sizes === "512x512"));
    await waitFor(() => page.eval(`navigator.serviceWorker.getRegistration("/app/").then(r => !!r)`), "service worker");
    await page.shot("05-settings");
    await page.close();
  });

  for (const width of [360, 390, 430]) {
    test(`ширина ${width} px: нет горизонтальной прокрутки, нижнее меню видно`, async () => {
      const page = await (await browser.page(base)).setup({ width, height: 780, geo: IN_AREA });
      await page.open(base + "/app/");
      for (const screen of ["map", "here", "queue", "settings"]) {
        await page.click(`nav button[data-s="${screen}"]`);
        await sleep(200);
        const overflow = await page.eval(`document.documentElement.scrollWidth - innerWidth`);
        assert.ok(overflow <= 0, `экран ${screen}: шире экрана на ${overflow} px`);
        assert.ok(await page.visible("nav"), `экран ${screen}: нижнее меню не видно`);
        const navBottom = await page.eval(`document.querySelector("nav").getBoundingClientRect().bottom`);
        assert.ok(navBottom <= 780 + 1, `экран ${screen}: меню уходит за низ экрана (${navBottom})`);
      }
      // Кнопки нижнего меню удобно нажимать пальцем: не меньше 44 px.
      const small = await page.eval(`[...document.querySelectorAll("nav button")].filter(b => b.getBoundingClientRect().height < 44).length`);
      assert.equal(small, 0);
      assert.deepEqual(page.errors, []);
      await page.close();
    });
  }
});
