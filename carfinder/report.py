"""
Генерация автономного HTML-дашборда (без внешних зависимостей и CDN).

Отчёт рассчитан на то, что подходящих объявлений сотни: с ними невозможно
работать без отсева вручную. Поэтому карточку можно пометить звёздочкой или
скрыть, и эти пометки переживают перегенерацию отчёта — они хранятся в
localStorage браузера по ключу объявления, а не в самом файле.

Шаблон — «сырая» строка: в нём есть регулярное выражение с `\/`, и в обычной
строке Python ругался бы на нераспознанную escape-последовательность.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import verify
from .models import DRIVE_LABEL_RU, FUEL_LABEL_RU, Listing

TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Car Finder — __TITLE__</title>
<style>
:root {
  --bg:#f6f7f9; --card:#fff; --fg:#16181d; --muted:#6b7280; --line:#e3e6ea;
  --accent:#2563eb; --new:#16a34a; --drop:#dc2626; --warn:#f59e0b;
  --star:#eab308; --chip:#eef1f5;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --card:#171a21; --fg:#e8eaed; --muted:#9aa3af; --line:#252a33;
          --accent:#60a5fa; --new:#4ade80; --drop:#f87171; --warn:#fbbf24;
          --star:#facc15; --chip:#222732; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 -apple-system,
       BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
header { position:sticky; top:0; z-index:10; background:var(--bg);
         border-bottom:1px solid var(--line); padding:12px 20px 10px; }
h1 { margin:0 0 2px; font-size:19px; letter-spacing:-.01em; }
.sub { color:var(--muted); font-size:13px; }
.controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; }
input[type=search], select {
  background:var(--card); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:7px 10px; font-size:14px; }
input[type=search] { min-width:200px; flex:1 1 200px; }
.tabs { display:flex; gap:6px; flex-wrap:wrap; }
.tab { padding:7px 13px; border-radius:8px; border:1px solid var(--line);
       background:var(--card); cursor:pointer; font-size:14px; color:var(--fg); }
.tab.on { background:var(--accent); border-color:var(--accent); color:#fff; }
.tab .n { opacity:.65; margin-left:5px; font-variant-numeric:tabular-nums; }
.toggle { display:flex; align-items:center; gap:6px; font-size:13px; color:var(--muted);
          cursor:pointer; user-select:none; white-space:nowrap; }
.range { display:flex; align-items:center; gap:7px; font-size:13px; color:var(--muted); }
.range input { accent-color:var(--accent); }
main { padding:16px 20px 60px; }
.grid { display:grid; gap:14px;
        grid-template-columns:repeat(auto-fill, minmax(310px, 1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        overflow:hidden; display:flex; flex-direction:column; position:relative; }
.card.isnew { border-color:var(--new); box-shadow:0 0 0 1px var(--new) inset; }
.card.starred { border-color:var(--star); box-shadow:0 0 0 1px var(--star) inset; }
.card.dimmed { opacity:.55; }
.thumb { aspect-ratio:4/3; background:var(--chip); object-fit:cover; width:100%;
         display:block; }
.acts { position:absolute; top:8px; right:8px; display:flex; gap:6px; }
.acts button { width:30px; height:30px; border-radius:8px; border:none; cursor:pointer;
        background:rgba(12,14,18,.62); color:#fff; font-size:15px; line-height:1;
        backdrop-filter:blur(3px); }
.acts button.on { background:var(--star); color:#1a1a1a; }
.body { padding:11px 13px 13px; display:flex; flex-direction:column; gap:7px; flex:1; }
.row { display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
.name { font-weight:600; font-size:15px; line-height:1.25; }
.price { font-size:18px; font-weight:700; white-space:nowrap; }
.deal { font-size:13px; font-weight:600; color:var(--new); display:flex;
        align-items:center; gap:6px; }
.deal.over { color:var(--muted); font-weight:500; }
.deal svg { display:block; }
.specs { display:flex; flex-wrap:wrap; gap:5px; }
.chip { background:var(--chip); border-radius:6px; padding:2px 7px; font-size:12px;
        color:var(--muted); white-space:nowrap; }
.chip.hi { background:color-mix(in srgb, var(--new) 22%, transparent); color:var(--fg); }
.chip.warn { background:color-mix(in srgb, var(--warn) 25%, transparent); color:var(--fg); }
.chip.stale { background:color-mix(in srgb, var(--warn) 14%, transparent); }
.badges { display:flex; gap:6px; flex-wrap:wrap; }
.badge { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
         padding:2px 7px; border-radius:5px; color:#fff; }
.badge.new { background:var(--new); }
.badge.drop { background:var(--drop); }
.badge.warn { background:var(--warn); color:#1a1a1a; }
.badge.src { background:transparent; color:var(--muted); border:1px solid var(--line); }
.foot { margin-top:auto; display:flex; justify-content:space-between; align-items:center;
        gap:8px; padding-top:6px; }
a.open { color:#fff; background:var(--accent); text-decoration:none; padding:7px 12px;
         border-radius:8px; font-size:13px; font-weight:600; }
.score { display:flex; align-items:center; gap:7px; font-size:12px; color:var(--muted); }
.bar { width:56px; height:5px; border-radius:3px; background:var(--chip); overflow:hidden; }
.bar i { display:block; height:100%; background:var(--accent); }
.empty { color:var(--muted); padding:48px 0; text-align:center; }
details.why { font-size:12px; color:var(--muted); }
details.why summary { cursor:pointer; }
details.why div { padding-top:4px; }
</style>
</head>
<body>
<header>
  <h1>Car Finder — кроссовер 4x4, автомат</h1>
  <div class="sub">__SUBTITLE__</div>
  <div class="controls">
    <div class="tabs">
      <button class="tab on" data-tab="match">Подходящие<span class="n" id="n-match"></span></button>
      <button class="tab" data-tab="near">Почти<span class="n" id="n-near"></span></button>
      <button class="tab" data-tab="star">★ Избранное<span class="n" id="n-star"></span></button>
      <button class="tab" data-tab="hidden">Скрытые<span class="n" id="n-hidden"></span></button>
    </div>
  </div>
  <div class="controls">
    <input type="search" id="q" placeholder="Поиск: марка, модель, город…">
    <select id="sort">
      <option value="score">По рейтингу</option>
      <option value="deal">Выгоднее к похожим</option>
      <option value="new">Сначала новые</option>
      <option value="price">Дешевле сначала</option>
      <option value="price-desc">Дороже сначала</option>
      <option value="mileage">Меньше пробег</option>
      <option value="year">Новее год</option>
      <option value="dist">Ближе ко мне</option>
    </select>
    <label class="range">до <input type="range" id="dist" min="20" max="__MAXDIST__"
      step="10" value="__MAXDIST__"><span id="dist-v">—</span></label>
    <label class="range">на сайте <input type="range" id="days" min="10" max="__MAXDAYS__"
      step="10" value="__DAYSDEFAULT__"><span id="days-v">—</span></label>
    <label class="toggle"><input type="checkbox" id="only-new"> только новые</label>
    <label class="toggle"><input type="checkbox" id="only-eco"> гибрид / ГБО</label>
    <label class="toggle"><input type="checkbox" id="only-deal"> дешевле похожих</label>
  </div>
</header>
<main><div class="grid" id="grid"></div><div class="empty" id="empty" hidden></div></main>

<script id="data" type="application/json">__DATA__</script>
<script>
const ALL = JSON.parse(document.getElementById('data').textContent);
const el = id => document.getElementById(id);
const fmt = n => n == null ? '—' : n.toLocaleString('pl-PL').replace(/,/g, ' ');

// Заголовок, город и ссылки пишет продавец — это текст из интернета, и в
// разметку он попадает только через esc(). Иначе заголовок вида
// `<img src=x onerror=...>` выполнит свой код на странице отчёта: сотрёт
// пометки ★ в localStorage или отправит содержимое отчёта наружу.
const esc = s => String(s ?? '').replace(/[&<>"']/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[ch]));

// В href и src пускаем только http(s). `javascript:`-ссылка из объявления
// сработала бы по клику на «Открыть», а экранирование от этого не спасает.
const safeUrl = u => /^https?:\/\//i.test(u || '') ? esc(u) : '';

// Пометки живут в браузере и переживают перегенерацию отчёта.
const LS = 'carfinder.marks.v1';
const marks = JSON.parse(localStorage.getItem(LS) || '{}');
const saveMarks = () => localStorage.setItem(LS, JSON.stringify(marks));
const markOf = k => marks[k] || '';
const setMark = (k, v) => { if (v) marks[k] = v; else delete marks[k]; saveMarks(); };

const PREFS = 'carfinder.prefs.v1';
const prefs = JSON.parse(localStorage.getItem(PREFS) || '{}');
let tab = prefs.tab || 'match';

function savePrefs() {
  Object.assign(prefs, {
    tab, q: el('q').value, sort: el('sort').value, dist: el('dist').value,
    days: el('days').value,
    onlyNew: el('only-new').checked, onlyEco: el('only-eco').checked,
    onlyDeal: el('only-deal').checked,
  });
  localStorage.setItem(PREFS, JSON.stringify(prefs));
}

function sparkline(history) {
  if (!history || history.length < 2) return '';
  const values = history.map(p => p[1]);
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) =>
    `${(i / (values.length - 1) * 46).toFixed(1)},${(12 - (v - lo) / span * 10).toFixed(1)}`
  ).join(' ');
  return `<svg width="48" height="14" viewBox="0 0 48 14" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;
}

function card(c) {
  const mark = markOf(c.key);
  const badges = [];
  if (c.is_new) badges.push('<span class="badge new">новое</span>');
  if (c.price_drop > 0) badges.push(`<span class="badge drop">−${fmt(c.price_drop)} zł</span>`);
  if (c.suspicious_price)
    badges.push(`<span class="badge warn" title="${esc(c.suspicious_price)}">⚠ цена занижена</span>`);
  badges.push(`<span class="badge src">${esc(c.source)}</span>`);

  const specs = [];
  if (c.year) specs.push(`<span class="chip">${esc(c.year)}</span>`);
  if (c.mileage != null) specs.push(`<span class="chip">${fmt(c.mileage)} км</span>`);
  if (c.fuel_label) specs.push(`<span class="chip${c.eco ? ' hi' : ''}">${esc(c.fuel_label)}</span>`);
  if (c.drive_label) specs.push(`<span class="chip">${esc(c.drive_label)}</span>`);
  if (c.power) specs.push(`<span class="chip">${esc(c.power)} л.с.</span>`);
  const place = [esc(c.city), c.distance_km != null ? `${c.distance_km} км` : '']
    .filter(Boolean).join(' · ');
  if (place) specs.push(`<span class="chip">${place}</span>`);
  if (c.is_business === false) specs.push('<span class="chip">частник</span>');
  if (c.days_listed != null)
    specs.push(`<span class="chip${c.days_listed >= 45 ? ' stale' : ''}">${
      c.days_listed === 0 ? 'сегодня' : c.days_listed + ' дн. на сайте'}</span>`);
  if (c.near_miss_reasons?.length)
    specs.push(`<span class="chip">${esc(c.near_miss_reasons.join(', '))}</span>`);
  (c.warnings || []).forEach(w => specs.push(`<span class="chip warn">⚠ ${esc(w)}</span>`));

  let deal = '';
  if (c.market_delta != null && Math.abs(c.market_delta) >= 1500) {
    const cheaper = c.market_delta > 0;
    deal = `<div class="deal${cheaper ? '' : ' over'}" title="Сравнение: ${esc(c.market_basis)}">
      ${cheaper ? '▼' : '▲'} ${fmt(Math.abs(c.market_delta))} zł ${
      cheaper ? 'дешевле' : 'дороже'} похожих ${sparkline(c.history)}</div>`;
  } else if (c.history?.length > 1) {
    deal = `<div class="deal over">${sparkline(c.history)} цена менялась</div>`;
  }

  const why = Object.entries(c.score_parts || {})
    .map(([k, v]) => `${esc(k)}: ${v > 0 ? '+' : ''}${v}`).join(' · ');

  const cls = ['card', c.is_new && 'isnew', mark === 'star' && 'starred',
               mark === 'hide' && 'dimmed'].filter(Boolean).join(' ');

  const thumb = safeUrl(c.image);
  const link = safeUrl(c.url);

  return `<article class="${cls}" data-key="${esc(c.key)}">
    ${thumb ? `<img class="thumb" loading="lazy" src="${thumb}" alt="">`
            : '<div class="thumb"></div>'}
    <div class="acts">
      <button data-act="star" class="${mark === 'star' ? 'on' : ''}" title="В избранное">★</button>
      <button data-act="hide" title="${mark === 'hide' ? 'Вернуть' : 'Скрыть'}">${
        mark === 'hide' ? '↩' : '✕'}</button>
    </div>
    <div class="body">
      <div class="badges">${badges.join('')}</div>
      <div class="row"><div class="name">${esc(c.title)}</div>
        <div class="price">${fmt(c.price)} zł</div></div>
      ${deal}
      <div class="specs">${specs.join('')}</div>
      <details class="why"><summary>рейтинг ${c.score}</summary><div>${why}</div></details>
      <div class="foot">
        <div class="score"><div class="bar"><i style="width:${c.score}%"></i></div>${c.score}</div>
        ${link ? `<a class="open" href="${link}" target="_blank" rel="noopener">Открыть</a>`
               : '<span class="open" title="Ссылка отброшена: не http(s)">—</span>'}
      </div>
    </div>
  </article>`;
}

function inTab(c, name) {
  const mark = markOf(c.key);
  if (name === 'hidden') return mark === 'hide';
  if (mark === 'hide') return false;              // скрытое не мешается в остальных
  if (name === 'star') return mark === 'star';
  return (name === 'near') === !!c.near_miss;
}

function counts() {
  ['match', 'near', 'star', 'hidden'].forEach(name =>
    el('n-' + name).textContent = ALL.filter(c => inTab(c, name)).length);
}

function render() {
  const q = el('q').value.trim().toLowerCase();
  const maxDist = +el('dist').value;
  const distMax = +el('dist').max;
  el('dist-v').textContent = maxDist >= distMax ? 'все' : maxDist + ' км';

  const maxDays = +el('days').value;
  const daysMax = +el('days').max;
  el('days-v').textContent = maxDays >= daysMax ? 'любой срок' : 'до ' + maxDays + ' дн.';

  let items = ALL.filter(c => inTab(c, tab));
  if (q) items = items.filter(c => c.search.includes(q));
  if (el('only-new').checked) items = items.filter(c => c.is_new);
  if (el('only-eco').checked) items = items.filter(c => c.eco);
  if (el('only-deal').checked) items = items.filter(c => (c.market_delta || 0) > 0);
  if (maxDist < distMax)
    items = items.filter(c => c.distance_km != null && c.distance_km <= maxDist);
  // Объявления без даты публикации не прячем: отсутствие даты — не признак того,
  // что объявление залежалось.
  if (maxDays < daysMax)
    items = items.filter(c => c.days_listed == null || c.days_listed <= maxDays);

  const big = 1e12;
  items.sort({
    score: (a, b) => b.score - a.score,
    deal: (a, b) => (b.market_delta ?? -big) - (a.market_delta ?? -big),
    new: (a, b) => (b.is_new - a.is_new) || (b.created_ts - a.created_ts),
    price: (a, b) => (a.price ?? big) - (b.price ?? big),
    'price-desc': (a, b) => (b.price ?? -1) - (a.price ?? -1),
    mileage: (a, b) => (a.mileage ?? big) - (b.mileage ?? big),
    year: (a, b) => (b.year ?? 0) - (a.year ?? 0),
    dist: (a, b) => (a.distance_km ?? big) - (b.distance_km ?? big),
  }[el('sort').value]);

  el('grid').innerHTML = items.map(card).join('');
  el('empty').hidden = items.length > 0;
  el('empty').textContent = tab === 'star'
    ? 'Пока ничего не отмечено — жмите ★ на карточке'
    : 'Ничего не найдено';
  counts();
  savePrefs();
}

// Пометки: делегирование, чтобы не навешивать обработчики на сотни карточек.
el('grid').addEventListener('click', e => {
  const button = e.target.closest('button[data-act]');
  if (!button) return;
  const key = button.closest('.card').dataset.key;
  const act = button.dataset.act;
  setMark(key, markOf(key) === act ? '' : act);
  render();
});

document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); tab = b.dataset.tab; render();
}));
['q', 'sort', 'dist', 'days', 'only-new', 'only-eco', 'only-deal'].forEach(id =>
  el(id).addEventListener(id === 'q' ? 'input' : 'change', render));
el('dist').addEventListener('input', render);
el('days').addEventListener('input', render);

// Восстанавливаем прошлую сессию
if (prefs.q) el('q').value = prefs.q;
if (prefs.sort) el('sort').value = prefs.sort;
if (prefs.dist) el('dist').value = prefs.dist;
if (prefs.days) el('days').value = prefs.days;
el('only-new').checked = !!prefs.onlyNew;
el('only-eco').checked = !!prefs.onlyEco;
el('only-deal').checked = !!prefs.onlyDeal;
document.querySelectorAll('.tab').forEach(b =>
  b.classList.toggle('on', b.dataset.tab === tab));
render();
</script>
</body>
</html>
"""

ECO_FUELS = {"hybrid", "plugin-hybrid", "petrol-lpg", "petrol-cng"}

# Правый край ползунка «на сайте» означает «любой срок».
MAX_DAYS_SLIDER = 200


def _card_data(listing: Listing, meta: dict) -> dict:
    created_ts = 0.0
    if listing.created_at:
        try:
            created_ts = datetime.fromisoformat(
                listing.created_at.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            created_ts = 0.0

    search = " ".join(
        str(p) for p in (
            listing.title, listing.make, listing.model, listing.version,
            listing.city, listing.region, listing.seller_name,
            FUEL_LABEL_RU.get(listing.fuel or "", ""), listing.fuel, listing.year,
        ) if p
    ).lower()

    return {
        "key": listing.key,
        "title": listing.title,
        "url": listing.url,
        "source": listing.source,
        "price": listing.price,
        "year": listing.year,
        "mileage": listing.mileage,
        "power": listing.engine_power,
        "city": listing.city,
        "distance_km": listing.distance_km,
        "fuel": listing.fuel,
        "fuel_label": FUEL_LABEL_RU.get(listing.fuel or ""),
        "drive_label": DRIVE_LABEL_RU.get(listing.drive or ""),
        "eco": listing.fuel in ECO_FUELS,
        "is_business": listing.is_business,
        "image": listing.image,
        "score": listing.score,
        "score_parts": listing.score_parts,
        "near_miss": listing.near_miss,
        "near_miss_reasons": listing.near_miss_reasons,
        "suspicious_price": listing.suspicious_price,
        "warnings": [verify.reason(c) for c in listing.flags
                     if c not in verify.HARD_FLAGS],
        "market_delta": listing.market_delta,
        "market_basis": listing.market_basis,
        "days_listed": listing.days_listed,
        "created_ts": created_ts,
        "search": search,
        "is_new": meta.get("is_new", False),
        "price_drop": meta.get("price_drop", 0),
        "history": meta.get("history") or [],
    }


def render(path: Path, listings: list[tuple[Listing, dict]], scanned: int, cfg: dict) -> Path:
    data = [_card_data(l, m) for l, m in listings]
    cr = cfg["criteria"]
    loc = cfg["location"]
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    days_default = min((cfg.get("report") or {}).get("max_days_default", MAX_DAYS_SLIDER),
                       MAX_DAYS_SLIDER)
    matched = sum(1 for c in data if not c["near_miss"])
    fresh = sum(1 for c in data if c["is_new"] and not c["near_miss"])
    deals = sum(1 for c in data if (c["market_delta"] or 0) > 0)
    subtitle = (
        f"{matched} подходит · {fresh} новых · {deals} дешевле похожих · "
        f"просмотрено {scanned}<br>"
        + f"до {cr['price_max']:,} zł".replace(",", " ")
        + f" · от {cr['year_from']} г. · {loc['radius_km']} км от "
        + loc["city"].capitalize()
        + f" · обновлено {now}"
    )

    html = (
        TEMPLATE.replace("__TITLE__", now)
        .replace("__SUBTITLE__", subtitle)
        .replace("__MAXDIST__", str(loc["radius_km"]))
        .replace("__MAXDAYS__", str(MAX_DAYS_SLIDER))
        .replace("__DAYSDEFAULT__", str(days_default))
        .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
