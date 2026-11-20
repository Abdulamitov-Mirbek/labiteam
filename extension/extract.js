// Координаты дома из HTML объявления. Чистые функции без доступа к DOM —
// тестируются в Node (extension/tests) и подключаются в content script.
(function (root) {
  "use strict";

  // Широкая рамка вокруг Бишкека: всё за её пределами — мусор или другой город.
  const AREA = { latMin: 42.6, latMax: 43.1, lonMin: 74.3, lonMax: 74.9 };

  function point(lat, lon) {
    lat = Number(lat);
    lon = Number(lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    if (lat < AREA.latMin || lat > AREA.latMax || lon < AREA.lonMin || lon > AREA.lonMax) return null;
    return { lat, lon };
  }

  function scripts(html, pattern) {
    const out = [];
    for (const m of html.matchAll(pattern)) out.push(m[1]);
    return out;
  }

  function parseJson(text) {
    try { return JSON.parse(text); } catch (e) { return undefined; }
  }

  // Обход JSON без рекурсии: на чужих страницах глубина вложенности не гарантирована.
  function find(value, test) {
    const stack = [value];
    let steps = 0;
    while (stack.length && steps++ < 200000) {
      const node = stack.pop();
      if (node && typeof node === "object") {
        const hit = test(node);
        if (hit) return hit;
        for (const key in node) stack.push(node[key]);
      }
    }
    return null;
  }

  // house.kg: JSON-LD с GeoCoordinates {latitude, longitude}.
  function fromJsonLd(html) {
    for (const text of scripts(html, /<script[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)) {
      const hit = find(parseJson(text), node =>
        (node["@type"] === "GeoCoordinates" || "latitude" in node) ? point(node.latitude, node.longitude) : null);
      if (hit) return hit;
    }
    return null;
  }

  // lalafo.kg: __NEXT_DATA__ содержит и похожие объявления, поэтому берём объект
  // с тем же id, что в адресе страницы (…-id-113744590).
  function fromNextData(html, url) {
    const id = /-id-(\d+)/.exec(url || "");
    if (!id) return null;
    const adId = Number(id[1]);
    for (const text of scripts(html, /<script[^>]*id=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)<\/script>/gi)) {
      const hit = find(parseJson(text), node =>
        node.id === adId && "lat" in node ? point(node.lat, node.lng ?? node.lon) : null);
      if (hit) return hit;
    }
    return null;
  }

  function extractCoords(html, url) {
    if (typeof html !== "string" || !html) return null;
    return fromJsonLd(html) || fromNextData(html, url);
  }

  // Текст плашки по ответу GET /api/signal.
  function describe(signal) {
    if (!signal || signal.status !== "ok") return null;
    const parts = [`Связь в этом доме: ${signal.quality} (${dbm(signal.median)})`];
    const { day_median: day, night_median: night } = signal;
    if (day !== null && night !== null && day !== undefined && night !== undefined) {
      if (night <= day - 3) parts.push(`ночью хуже (${dbm(night)})`);
      else if (night >= day + 3) parts.push(`ночью лучше (${dbm(night)})`);
      else parts.push("днём и ночью одинаково");
    }
    const weak = signal.nearest_weak_m;
    if (signal.color !== "red" && weak !== null && weak !== undefined && weak <= 300) {
      parts.push(`⚠️ в ${Math.max(50, Math.round(weak / 50) * 50)} м слабая зона`);
    }
    return parts.join(" · ");
  }

  function dbm(value) {
    return String(Math.round(value)).replace("-", "−");
  }

  const api = { extractCoords, describe, point };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SignalKg = api;
})(this);
