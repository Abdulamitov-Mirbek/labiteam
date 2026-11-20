// Запуск: node --test extension/tests/*.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const { extractCoords, describe } = require("../extract.js");

const ldJson = data => `<script type="application/ld+json">${JSON.stringify(data)}</script>`;
const nextData = data => `<script id="__NEXT_DATA__" type="application/json">${JSON.stringify(data)}</script>`;

test("house.kg: координаты из JSON-LD GeoCoordinates", () => {
  const html = `<html>${ldJson({
    "@type": "Product",
    offers: {},
    contentLocation: { address: { addressLocality: "Бишкек" },
      geo: { "@type": "GeoCoordinates", latitude: 42.8687, longitude: 74.604 } },
  })}</html>`;
  assert.deepEqual(extractCoords(html, "https://www.house.kg/details/abc-1"), { lat: 42.8687, lon: 74.604 });
});

test("lalafo: берётся объявление с id из адреса, а не похожее", () => {
  const html = nextData({ props: { pageProps: { similar: [{ id: 1, lat: 42.80, lng: 74.50 }],
    dehydratedState: { queries: [{ state: { data: { id: 113744590, lat: 42.8746212, lng: 74.5697617 } } }] } } } });
  assert.deepEqual(
    extractCoords(html, "https://lalafo.kg/bishkek/ads/posutocnaa-kvartira-id-113744590"),
    { lat: 42.8746212, lon: 74.5697617 });
});

test("lalafo: нулевые координаты и чужой id не принимаются", () => {
  const html = nextData({ a: { id: 5, lat: 0, lng: 0 }, b: { id: 6, lat: 42.87, lng: 74.6 } });
  assert.equal(extractCoords(html, "https://lalafo.kg/bishkek/ads/x-id-5"), null);
});

test("координаты вне Бишкека и мусор отбрасываются", () => {
  assert.equal(extractCoords(ldJson({ geo: { "@type": "GeoCoordinates", latitude: 55.75, longitude: 37.6 } }), ""), null);
  assert.equal(extractCoords(ldJson({ geo: { "@type": "GeoCoordinates", latitude: "abc", longitude: 74.6 } }), ""), null);
  assert.equal(extractCoords('<script type="application/ld+json">{oops</script>', ""), null);
  assert.equal(extractCoords("", ""), null);
  assert.equal(extractCoords(null, ""), null);
});

test("плашка: хорошая связь, ночью хуже, слабая зона рядом", () => {
  const text = describe({ status: "ok", quality: "хорошая", color: "green", median: -84.2,
    day_median: -83, night_median: -97, nearest_weak_m: 280 });
  assert.equal(text, "Связь в этом доме: хорошая (−84) · ночью хуже (−97) · ⚠️ в 300 м слабая зона");
});

test("плашка: без ночных данных и без слабой зоны рядом", () => {
  const text = describe({ status: "ok", quality: "средняя", color: "yellow", median: -98.3,
    day_median: -96.2, night_median: null, nearest_weak_m: 569 });
  assert.equal(text, "Связь в этом доме: средняя (−98)");
});

test("плашка: в красном квадрате предупреждение о соседней зоне не дублируется", () => {
  const text = describe({ status: "ok", quality: "слабая", color: "red", median: -112,
    day_median: -111, night_median: -112, nearest_weak_m: 0 });
  assert.equal(text, "Связь в этом доме: слабая (−112) · днём и ночью одинаково");
});

test("плашка: нет данных — нет текста", () => {
  assert.equal(describe({ status: "no_data" }), null);
  assert.equal(describe(null), null);
});
