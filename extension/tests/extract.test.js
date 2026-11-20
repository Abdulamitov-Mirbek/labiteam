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
    dehydratedState: { queries: [{ state: { data: { id: 113744590, lat: 42.85625521708996, lng: 74.58782376781599 } } }] } } } });
  assert.deepEqual(
    extractCoords(html, "https://lalafo.kg/bishkek/ads/posutocnaa-kvartira-id-113744590"),
    { lat: 42.85625521708996, lon: 74.58782376781599 });
});

test("lalafo: точка «Бишкек по умолчанию» — это не адрес дома", () => {
  // Её ставит lalafo, когда адрес не указан: на странице поиска так ~10 % объявлений.
  const html = nextData({ ad: { id: 118401039, lat: 42.8746212, lng: 74.5697617 } });
  assert.equal(extractCoords(html, "https://lalafo.kg/bishkek/ads/x-id-118401039"), null);
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

test("плашка: вывод по зоне карты", () => {
  assert.equal(describe({ status: "ok", color: "green", median: -84.2 }), "Хорошая связь — зелёная зона (−84 дБм)");
  assert.equal(describe({ status: "ok", color: "yellow", median: -98.3 }), "Средняя связь — жёлтая зона (−98 дБм)");
  assert.equal(describe({ status: "ok", color: "red", median: -112 }), "Плохой интернет — дом в красной зоне (−112 дБм)");
});

test("плашка: нет данных — нет текста", () => {
  assert.equal(describe({ status: "no_data" }), null);
  assert.equal(describe({ status: "ok", color: "purple", median: -90 }), null);
  assert.equal(describe(null), null);
});
