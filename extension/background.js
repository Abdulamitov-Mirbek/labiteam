// Сервисный воркер: единственное место, которое ходит в API «Сигнал.kg».
// Content script не делает сетевых запросов к API сам: так ему не нужен CORS,
// а сайт объявления никак не может обратиться к API от имени плагина.
const API_BASE = "http://127.0.0.1:8000";

function validPoint(message) {
  return Number.isFinite(message.lat) && Number.isFinite(message.lon);
}

// Сайт объявления берётся из адреса вкладки, который сообщает сам Chrome,
// а не из данных страницы.
function listingSite(sender) {
  try {
    const host = new URL(sender.url).hostname.replace(/^www\./, "");
    return ["house.kg", "lalafo.kg"].includes(host) ? host : null;
  } catch (e) {
    return null;
  }
}

async function call(path, options) {
  try {
    const response = await fetch(API_BASE + path, options);
    const body = await response.json().catch(() => null);
    return { ok: response.ok, status: response.status, body };
  } catch (e) {
    return { ok: false, status: 0, body: null };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Сообщения принимаются только от собственных content scripts плагина.
  if (sender.id !== chrome.runtime.id || !message || !validPoint(message)) return false;
  if (message.type === "signal") {
    const query = new URLSearchParams({ lat: message.lat, lon: message.lon });
    call("/api/signal?" + query).then(sendResponse);
    return true;
  }
  if (message.type === "complaint") {
    call("/api/complaints", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat: message.lat, lon: message.lon, source: "plugin", site: listingSite(sender) }),
    }).then(sendResponse);
    return true;
  }
  return false;
});
