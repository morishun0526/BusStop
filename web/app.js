/* バス接近 — お気に入りのバス停に、あと何分でバスが来るかを表示する PWA */
(() => {
"use strict";

const CFG = Object.assign({ RT_PROXY: "", REFRESH_SEC: 30 }, window.BUS_CONFIG || {});
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const ICON = {
  back: '<svg viewBox="0 0 13 21"><path d="M11 2 2.5 10.5 11 19"/></svg>',
  chev: '<svg class="chev" viewBox="0 0 8 13"><path d="m1.5 1.5 5 5-5 5"/></svg>',
  star: '<svg viewBox="0 0 24 24"><path d="m12 2.8 2.8 5.8 6.3.9-4.6 4.4 1.1 6.3L12 17.2l-5.6 3 1.1-6.3L2.9 9.5l6.3-.9z"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg>',
  bus: '<svg viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="15" rx="3"/><path d="M4 11h16M8 18v2M16 18v2"/><circle cx="8" cy="14.5" r=".8"/><circle cx="16" cy="14.5" r=".8"/></svg>',
  grip: '<svg viewBox="0 0 20 14"><path d="M2 2h16M2 7h16M2 12h16"/></svg>',
  ext: '<svg class="chev" viewBox="0 0 14 14" style="width:13px;height:13px"><path d="M5 2h7v7M12 2 3 11"/></svg>',
};

/* ================================================================ 保存（お気に入り） */
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
let favs = store.get("favs", []); // [{cid, k, name, co}]
const saveFavs = () => store.set("favs", favs);
const isFav = (cid, k) => favs.some(f => f.cid === cid && f.k === k);

/* ================================================================ データ取得 */
const cache = new Map();
async function getJSON(path) {
  if (cache.has(path)) return cache.get(path);
  const p = fetch("data/" + path, { cache: "no-cache" }).then(r => {
    if (!r.ok) throw new Error(r.status);
    return r.json();
  });
  cache.set(path, p);
  p.catch(() => cache.delete(path));
  return p;
}
const getIndex = () => getJSON("index.json");
async function getCompany(cid) {
  const idx = await getIndex();
  for (const p of idx.prefs) for (const c of p.companies) if (c.id === cid) return { ...c, pref: p.name };
  return null;
}

/* ================================================================ 日本時間 */
const fmt = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", weekday: "short",
});
const WD = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
function jstParts(ms) {
  const o = {};
  for (const p of fmt.formatToParts(new Date(ms))) o[p.type] = p.value;
  return { ymd: o.year + o.month + o.day, wd: WD[o.weekday], sec: +o.hour * 3600 + +o.minute * 60 + +o.second };
}
function serviceOn(cal, sid, d) {
  const ex = cal.x[sid] && cal.x[sid][d.ymd];
  if (ex === 1) return true;
  if (ex === 2) return false;
  const w = cal.w[sid];
  return !!w && w[0][d.wd] === "1" && (!w[1] || d.ymd >= w[1]) && (!w[2] || d.ymd <= w[2]);
}
const hhmm = s => { s = ((s % 86400) + 86400) % 86400; return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}`; };

/* ================================================================ リアルタイム（GTFS-RT） */
// 依存ライブラリなしの最小 protobuf デコーダ
function pbRead(buf) {
  let pos = 0;
  const varint = () => {
    let r = 0n, sh = 0n, b;
    do { b = buf[pos++]; r |= BigInt(b & 0x7f) << sh; sh += 7n; } while (b & 0x80 && pos < buf.length);
    return r;
  };
  const fields = function* (end) {
    while (pos < end) {
      const key = Number(varint()), no = key >> 3, wt = key & 7;
      if (wt === 0) yield [no, varint()];
      else if (wt === 2) { const len = Number(varint()); const s = pos; pos += len; yield [no, buf.subarray(s, s + len)]; }
      else if (wt === 1) pos += 8;
      else if (wt === 5) pos += 4;
      else throw new Error("bad wire type");
    }
  };
  return { fields: () => fields(buf.length) };
}
const td = new TextDecoder();
const msg = b => pbRead(b).fields();
const int = v => Number(BigInt.asIntN(64, v));

function parseFeed(u8) {
  const trips = new Map(), vehicles = new Map();
  for (const [no, ent] of msg(u8)) {
    if (no !== 2) continue;
    for (const [eno, tu] of msg(ent)) {
      if (eno === 4) { // VehiclePosition（車両位置）
        const v = { trip: "", seq: null, stop: "", status: 2, ts: null };
        for (const [vno, vv] of msg(tu)) {
          if (vno === 1) for (const [dno, dv] of msg(vv)) { if (dno === 1) v.trip = td.decode(dv); }
          else if (vno === 3) v.seq = int(vv);
          else if (vno === 4) v.status = int(vv);
          else if (vno === 5) v.ts = int(vv);
          else if (vno === 7) v.stop = td.decode(vv);
        }
        if (v.trip) vehicles.set(v.trip, v);
        continue;
      }
      if (eno !== 3) continue;
      const t = { id: "", delay: null, canceled: false, stu: [] };
      for (const [tno, v] of msg(tu)) {
        if (tno === 1) for (const [dno, dv] of msg(v)) {
          if (dno === 1) t.id = td.decode(dv);
          if (dno === 4 && int(dv) === 3) t.canceled = true;
        }
        else if (tno === 5) t.delay = int(v);
        else if (tno === 2) {
          const s = { seq: null, stop: "", delay: null, time: null, skip: false };
          for (const [sno, sv] of msg(v)) {
            if (sno === 1) s.seq = int(sv);
            else if (sno === 4) s.stop = td.decode(sv);
            else if (sno === 5) s.skip = int(sv) === 1;
            else if (sno === 2 || sno === 3) {
              for (const [xno, xv] of msg(sv)) {
                if (xno === 1 && (sno === 2 || s.delay === null)) s.delay = int(xv);
                if (xno === 2 && (sno === 2 || s.time === null)) s.time = int(xv);
              }
            }
          }
          t.stu.push(s);
        }
      }
      if (t.id) trips.set(t.id, t);
    }
  }
  return { trips, vehicles };
}

const rtCache = new Map(); // url -> {at, trips, ok}
async function getRT(url) {
  if (!url) return null;
  const c = rtCache.get(url);
  if (c && Date.now() - c.at < 20000) return c;
  const target = CFG.RT_PROXY ? CFG.RT_PROXY + encodeURIComponent(url) : url;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 8000);
  let res = { at: Date.now(), trips: new Map(), vehicles: new Map(), ok: false };
  try {
    const r = await fetch(target, { signal: ctl.signal, cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    res = { at: Date.now(), ...parseFeed(new Uint8Array(await r.arrayBuffer())), ok: true };
  } catch (e) { /* CORS やネットワーク不可 → 時刻表で表示 */ }
  clearTimeout(timer);
  rtCache.set(url, res);
  return res;
}

// 便のリアルタイム補正: {eta, live, delay, canceled}
function applyRT(trip, dep, schedEtaSec, nowMs) {
  const [, , , , , seq, stopId] = dep;
  if (!trip) return null;
  if (trip.canceled) return { canceled: true };
  let best = null;
  for (const s of trip.stu) {
    const same = (s.stop && s.stop === stopId) || (s.seq !== null && s.seq === seq);
    if (same) { best = s; break; }
    if (s.seq !== null && s.seq < seq && (s.delay !== null) && (!best || s.seq > best.seq)) best = s;
  }
  if (best && best.skip && ((best.stop && best.stop === stopId) || best.seq === seq)) return { canceled: true };
  if (best && best.time && ((best.stop && best.stop === stopId) || best.seq === seq)) {
    const eta = best.time - nowMs / 1000;
    return { eta, delay: eta - schedEtaSec, live: true };
  }
  const delay = best && best.delay !== null ? best.delay : trip.delay;
  if (delay === null || delay === undefined) return { eta: schedEtaSec, delay: 0, live: true };
  return { eta: schedEtaSec + delay, delay, live: true };
}

// 車両位置からの遅れ推定: 「バスが今いる停留所」の時刻表時刻と現在時刻の差を遅れとみなす
function applyVP(v, dep, schedEtaSec, nowMs) {
  const [, , , , , seq, stopId, prev] = dep;
  if (!v) return null;
  let vseq = v.seq;
  if (vseq === null && v.stop === stopId) vseq = seq;
  if (vseq === null) return null;
  if (vseq > seq) return { passed: true };
  let off = null; // 時刻表上「バスのいる停留所」→「このバス停」の所要秒
  if (vseq === seq) off = 0;
  else if (prev) for (let i = 0; i < prev.length; i += 2) if (prev[i] === vseq) { off = prev[i + 1]; break; }
  if (off === null) return null; // まだ遠い（手前12停留所より前）→ 時刻表どおり
  const ago = v.ts ? Math.max(0, nowMs / 1000 - v.ts) : 0;
  const schedAtVeh = schedEtaSec - off;                    // 今から見た「バスのいる停留所」の予定時刻
  const delay = Math.max(0, -ago - schedAtVeh);            // 予定より遅れている秒数（早着は0扱い）
  const eta = v.status === 1 && vseq === seq ? 0 : schedEtaSec + delay;
  return { eta, delay, live: true, near: seq - vseq };
}

/* ================================================================ 次のバスを計算 */
async function upcoming(cid, k, { limit = 20 } = {}) {
  const [stop, cal, co] = await Promise.all([getJSON(`c/${cid}/s/${k}.json`), getJSON(`c/${cid}/cal.json`), getCompany(cid)]);
  const now = Date.now();
  const today = jstParts(now), yest = jstParts(now - 86400000), tom = jstParts(now + 86400000);
  const nowSec = today.sec;
  const list = [];
  const consider = (d, offset) => {
    const act = {};
    for (const dep of stop.d) {
      const sid = dep[3];
      if (!(sid in act)) act[sid] = serviceOn(cal, sid, d);
      if (!act[sid]) continue;
      const eta = dep[0] + offset - nowSec;
      if (eta >= -1200 && eta < 26 * 3600) list.push({ dep, eta, sched: eta, day: offset > 0 ? "明日" : "" });
    }
  };
  consider(yest, -86400);
  consider(today, 0);
  const [rt, vp] = await Promise.all([co && co.rt ? getRT(co.rt) : null, co && co.vp ? getRT(co.vp) : null]);
  for (const it of list) {
    const a = (rt && rt.ok && applyRT(rt.trips.get(it.dep[4]), it.dep, it.sched, now)) ||
              (vp && vp.ok && applyVP(vp.vehicles.get(it.dep[4]), it.dep, it.sched, now));
    if (a) Object.assign(it, a);
  }
  let res = list.filter(x => !x.passed && (x.canceled || (x.live ? x.eta >= -30 : x.sched >= -30)))
    .sort((a, b) => a.eta - b.eta);
  if (res.filter(x => !x.canceled).length === 0) { // 本日の運行終了 → 明日の始発
    list.length = 0;
    consider(tom, 86400);
    res = list.sort((a, b) => a.eta - b.eta);
  }
  const live = (rt && rt.ok) ? "tu" : (vp && vp.ok) ? "vp" : "";
  return { stop, co, rt: { ok: !!live, kind: live }, items: res.slice(0, limit), at: now };
}

/* ================================================================ 画面共通 */
let lastBar = {};
function setBar({ title = "", back = null, right = "" }) {
  lastBar = { title, back };
  $("#barTitle").textContent = title;
  $("#barLeft").innerHTML = back ? `<button class="back" id="backBtn">${ICON.back}<span>${esc(back.label)}</span></button>` : "";
  if (back) $("#backBtn").onclick = () => (history.state && history.state.from !== undefined) ? history.back() : go(back.to, true);
  $("#barRight").innerHTML = right;
}
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function go(hash, replace) {
  if (replace) history.replaceState({}, "", hash);
  else history.pushState({ from: location.hash }, "", hash);
  route();
}
function toast(t) {
  let el = $(".toast");
  if (!el) { el = document.createElement("div"); el.className = "toast"; document.body.appendChild(el); }
  el.textContent = t; el.classList.add("show");
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), 1600);
}
const minHTML = (it, cls = "min") => {
  if (it.canceled) return `<div class="${cls}">運休</div>`;
  const m = Math.floor(Math.max(0, it.eta) / 60);
  return it.eta < 60
    ? `<div class="${cls} soon">まもなく</div>`
    : `<div class="${cls}${m <= 3 ? " soon" : ""}">${m}<small>分</small></div>`;
};
const PREF_KANA = "ほっかいどう,あおもりけん,いわてけん,みやぎけん,あきたけん,やまがたけん,ふくしまけん,いばらきけん,とちぎけん,ぐんまけん,さいたまけん,ちばけん,とうきょうと,かながわけん,にいがたけん,とやまけん,いしかわけん,ふくいけん,やまなしけん,ながのけん,ぎふけん,しずおかけん,あいちけん,みえけん,しがけん,きょうとふ,おおさかふ,ひょうごけん,ならけん,わかやまけん,とっとりけん,しまねけん,おかやまけん,ひろしまけん,やまぐちけん,とくしまけん,かがわけん,えひめけん,こうちけん,ふくおかけん,さがけん,ながさきけん,くまもとけん,おおいたけん,みやざきけん,かごしまけん,おきなわけん".split(",");
const normKana = s => (s || "").normalize("NFKC").toLowerCase().replace(/[ァ-ヶ]/g, c => String.fromCharCode(c.charCodeAt(0) - 0x60));

let timers = [];
const clearTimers = () => { timers.forEach(clearInterval); timers = []; };
let editing = false;

/* ================================================================ ホーム（お気に入り） */
function renderHome() {
  setBar({ title: editing ? "お気に入りを編集" : "" });
  $("#toolbar").hidden = false;
  $("#btnEdit").classList.toggle("active", editing);
  $("#btnEdit").disabled = !favs.length && !editing;
  $("#btnSearch").disabled = editing;

  if (!favs.length) {
    editing = false;
    $("#btnEdit").classList.remove("active");
    view.innerHTML = `<h1 class="large-title">お気に入り</h1>
      <div class="empty">${ICON.star.replace("<svg", '<svg style="stroke-width:1.4"')}<br>お気に入りのバス停はまだありません。<br>
      下の <b>検索</b> からバス停を探し、<br>右上の ☆ をタップして追加してください。</div>`;
    return;
  }
  view.innerHTML = `<h1 class="large-title">お気に入り</h1>
    <div class="group ${editing ? "editing" : ""}" id="favList">
      ${favs.map((f, i) => `
        <div class="row fav" data-i="${i}" role="${editing ? "listitem" : "button"}">
          <div class="main"><div class="t">${esc(f.name)}</div><div class="s">${esc(f.co)}</div></div>
          ${editing
            ? `<button class="minus" data-del="${i}" aria-label="${esc(f.name)}を削除"><i></i></button>
               <span class="handle" aria-hidden="true">${ICON.grip}</span>`
            : f.ext ? `<div class="fav-eta"><div class="sub">公式の接近情報</div></div>${ICON.ext}`
            : `<div class="fav-eta" id="eta${i}"><div class="sub">…</div></div>${ICON.chev}`}
        </div>`).join("")}
    </div>
    <p class="note">${editing ? "＝ をドラッグして並べ替え、⊖ で削除できます。" : "表示は30秒ごとに自動更新されます。"}</p>`;

  const list = $("#favList");
  if (editing) {
    list.querySelectorAll("[data-del]").forEach(b => b.onclick = e => {
      e.stopPropagation();
      const i = +b.dataset.del, row = b.closest(".row");
      row.classList.add("removing");
      setTimeout(() => { favs.splice(i, 1); saveFavs(); renderHome(); }, 220);
    });
    enableDrag(list);
  } else {
    list.querySelectorAll(".row").forEach(r => r.onclick = () => {
      const f = favs[+r.dataset.i];
      if (f.ext) window.open(f.ext, "_blank", "noopener");
      else go(`#/c/${encodeURIComponent(f.cid)}/${f.k}`);
    });
    const refresh = () => favs.forEach(async (f, i) => {
      const box = $("#eta" + i);
      if (!box || f.ext) return;
      try {
        const u = await upcoming(f.cid, f.k, { limit: 3 });
        const next = u.items.find(x => !x.canceled);
        if (!next) { box.innerHTML = `<div class="sub">本日終了</div>`; return; }
        box.innerHTML = (next.day ? `<div class="sub">明日 ${hhmm(next.dep[0])}</div>` : minHTML(next)) +
          `<div class="sub">${esc(next.dep[2] || next.dep[1])}${next.live ? " ●" : ""}</div>`;
      } catch { box.innerHTML = `<div class="sub">取得できません</div>`; }
    });
    refresh();
    timers.push(setInterval(refresh, CFG.REFRESH_SEC * 1000));
  }
}

// 並べ替え（タッチ／マウス共通）
function enableDrag(list) {
  const rows = [...list.querySelectorAll(".row")];
  rows.forEach(row => {
    row.addEventListener("pointerdown", e => {
      if (e.target.closest(".minus")) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      e.preventDefault();
      const from = +row.dataset.i, h = row.getBoundingClientRect().height, startY = e.clientY;
      let to = from;
      row.setPointerCapture(e.pointerId);
      row.classList.add("dragging");
      rows.forEach(r => r !== row && r.classList.add("shift"));
      const move = ev => {
        const dy = ev.clientY - startY;
        row.style.transform = `translateY(${dy}px)`;
        to = Math.max(0, Math.min(rows.length - 1, from + Math.round(dy / h)));
        rows.forEach(r => {
          const i = +r.dataset.i;
          if (r === row) return;
          let off = 0;
          if (from < to && i > from && i <= to) off = -h;
          if (from > to && i < from && i >= to) off = h;
          r.style.transform = off ? `translateY(${off}px)` : "";
        });
      };
      const up = () => {
        row.removeEventListener("pointermove", move);
        row.removeEventListener("pointerup", up);
        row.removeEventListener("pointercancel", up);
        if (to !== from) { const [m] = favs.splice(from, 1); favs.splice(to, 0, m); saveFavs(); }
        renderHome();
      };
      row.addEventListener("pointermove", move);
      row.addEventListener("pointerup", up);
      row.addEventListener("pointercancel", up);
    });
  });
}

/* ================================================================ ①-1 都道府県別バス会社一覧 */
let searchIdx = null;
async function getSearchIndex() {
  if (!searchIdx) searchIdx = getJSON("search.json").then(d => {
    d.s.forEach(r => { r.push(normKana(r[0])); }); // r[4] = 検索用に正規化した名前
    return d;
  });
  return searchIdx;
}

async function renderSearch() {
  setBar({ title: "検索", back: { label: "お気に入り", to: "#/" } });
  view.innerHTML = `<div class="searchwrap"><label class="search">${ICON.search}
      <input id="q" type="search" placeholder="バス停名で検索（例：渋谷駅）" autocomplete="off" enterkeyhint="search">
      <button class="clear" id="qc" hidden aria-label="クリア">✕</button></label></div>
    <div id="list"><div class="spinner"></div></div>`;
  let idx;
  try { idx = await getIndex(); } catch { $("#list").innerHTML = errorHTML(); return; }
  const status = await getJSON("status.json").catch(() => ({}));
  const q = $("#q"), qc = $("#qc");
  q.value = sessionStorage.getItem("q") || "";
  const footer = () => {
    const ng = Object.values(status).filter(x => !x.ok);
    return (ng.length ? `<p class="note warn">取り込めなかった会社：${ng.map(x => `<br>・${esc(x.name)}：${esc(x.msg)}`).join("")}</p>` : "") +
      `<p class="note">データ: GTFSデータリポジトリ（gtfs-data.jp）、公共交通オープンデータセンター／各事業者。${esc(idx.updated)} 更新</p>`;
  };
  const coRow = c => `<a class="row" href="#/c/${encodeURIComponent(c.id)}"><div class="main"><div class="t">${esc(c.name)}</div>
         ${c.ext ? '<div class="s">公式の接近情報サービスを開きます</div>'
           : (c.rt || c.vp) ? '<div class="s"><span class="dot live"></span>リアルタイム対応</div>' : ""}</div>${ICON.chev}</a>`;

  // 何も入力していないとき: 都道府県別のバス会社一覧
  const drawCompanies = () => {
    $("#list").innerHTML = idx.prefs.map(p => `<div class="section-h">${esc(p.name)}</div>
      <div class="group">${p.companies.map(coRow).join("")}</div>`).join("") + footer();
  };
  // 入力したとき: バス停を検索（全国）
  let seq = 0;
  const drawStops = async w => {
    const my = ++seq;
    if (!searchIdx) $("#list").innerHTML = `<div class="spinner"></div>`;
    let si;
    try { si = await getSearchIndex(); } catch { $("#list").innerHTML = errorHTML(); return; }
    if (my !== seq) return;
    const hits = [];
    for (const r of si.s) if (r[4].includes(w) || r[1].includes(w)) hits.push(r);
    const pre = r => (r[4].startsWith(w) || r[1].startsWith(w)) ? 0 : 1;
    hits.sort((a, b) => pre(a) - pre(b) || a[0].length - b[0].length || a[1].localeCompare(b[1], "ja"));
    const MAX = 100;
    const cos = [];
    for (const p of idx.prefs) for (const c of p.companies)
      if ((normKana(c.name).includes(w) || (c.y || "").includes(w)) && !cos.some(x => x.id === c.id)) cos.push(c);
    let html = "";
    if (hits.length) {
      html += `<div class="section-h">バス停（${hits.length > MAX ? MAX + "件以上" : hits.length + "件"}）</div><div class="group">${hits.slice(0, MAX).map(r => {
        const c = si.c[r[2]];
        return `<a class="row" href="#/c/${encodeURIComponent(c[0])}/${r[3]}"><div class="main"><div class="t">${esc(r[0])}</div>
          <div class="s">${esc(c[1])}${c[2] ? "・" + esc(c[2]) : ""}</div></div>${ICON.chev}</a>`;
      }).join("")}</div>`;
      if (hits.length > MAX) html += `<p class="note">候補が多いため先頭${MAX}件を表示しています。もう少し詳しく入力してください。</p>`;
    }
    if (cos.length) html += `<div class="section-h">バス会社</div><div class="group">${cos.map(coRow).join("")}</div>`;
    $("#list").innerHTML = (html || `<div class="empty">「${esc(q.value)}」に一致するバス停はありません。<br>ひらがなや、一部だけ（例：「しぶや」）でも探せます。</div>`) + footer();
  };
  let t = null;
  const draw = () => {
    const w = normKana(q.value.trim());
    qc.hidden = !w;
    sessionStorage.setItem("q", q.value);
    clearTimeout(t);
    if (!w) { seq++; drawCompanies(); } else t = setTimeout(() => drawStops(w), 150);
  };
  q.oninput = draw;
  qc.onclick = () => { q.value = ""; draw(); q.focus(); };
  draw();
}
const errorHTML = () => `<div class="empty">データを読み込めませんでした。<br>電波の良い場所でもう一度お試しください。</div>`;

/* ================================================================ バス停一覧（五十音順） */
function starButton(cid, k, makeFav) {
  setBar({ ...lastBar, right: `<button class="star ${isFav(cid, k) ? "on" : ""}" id="star" aria-label="お気に入り">${ICON.star}</button>` });
  const star = $("#star");
  star.onclick = () => {
    if (isFav(cid, k)) {
      favs = favs.filter(f => !(f.cid === cid && f.k === k));
      toast("お気に入りから外しました");
    } else {
      favs.push(makeFav());
      toast("お気に入りに追加しました");
    }
    saveFavs();
    star.classList.toggle("on", isFav(cid, k));
    star.classList.add("pop"); setTimeout(() => star.classList.remove("pop"), 160);
  };
}

// 時刻表データが公開されていない会社（公式の接近情報サービスへのリンク）
function renderExt(co) {
  setBar({ title: co.name, back: { label: "検索", to: "#/search" } });
  starButton(co.id, "_ext", () => ({ cid: co.id, k: "_ext", name: co.name, co: "公式の接近情報サービス", ext: co.ext }));
  view.innerHTML = `<div class="stop-head"><div class="name">${esc(co.name)}</div></div>
    <div class="hero"><div class="meta">${esc(co.note || "公式の接近情報サービスを開きます。")}</div>
      <a class="open-ext" href="${esc(co.ext)}" target="_blank" rel="noopener">公式の接近情報を開く ${ICON.ext}</a></div>
    <p class="note">右上の ☆ でお気に入りに追加すると、ホーム画面からワンタップで開けます。<br>
    公式ページ側でよく使うバス停を登録しておくと便利です。</p>`;
}

async function renderCompany(cid) {
  const co = await getCompany(cid).catch(() => null);
  if (co && co.ext) return renderExt(co);
  setBar({ title: co ? co.name : "バス停", back: { label: "検索", to: "#/search" } });
  view.innerHTML = `<div class="searchwrap"><label class="search">${ICON.search}
      <input id="q" type="search" placeholder="バス停名で絞り込み" autocomplete="off"></label></div>
    <div id="list"><div class="spinner"></div></div>`;
  let stops;
  try { stops = await getJSON(`c/${cid}/stops.json`); } catch { $("#list").innerHTML = errorHTML(); return; }
  const q = $("#q");
  const ORDER = ["あ", "か", "さ", "た", "な", "は", "ま", "や", "ら", "わ", "A", "#"];
  const draw = () => {
    const w = normKana(q.value.trim());
    const hit = stops.filter(s => !w || normKana(s.n).includes(w) || s.y.includes(w));
    const groups = ORDER.map(g => [g, hit.filter(s => s.g === g)]).filter(([, a]) => a.length);
    $("#list").innerHTML = groups.length ? groups.map(([g, a]) =>
      `<div class="section-h" id="g-${g}">${g === "#" ? "その他" : g === "A" ? "英字" : g + "行"}</div><div class="group">${a.map(s =>
        `<a class="row" href="#/c/${encodeURIComponent(cid)}/${s.k}"><div class="main"><div class="t">${esc(s.n)}</div></div>${ICON.chev}</a>`).join("")}</div>`).join("")
      : `<div class="empty">該当するバス停はありません</div>`;
    let ix = $(".kana-index");
    if (ix) ix.remove();
    if (!w && groups.length > 3) {
      ix = document.createElement("div");
      ix.className = "kana-index";
      ix.innerHTML = groups.map(([g]) => `<button data-g="${g}">${g === "#" ? "他" : g}</button>`).join("");
      ix.onclick = e => {
        const b = e.target.closest("button"); if (!b) return;
        const el = document.getElementById("g-" + b.dataset.g);
        if (el) window.scrollTo({ top: el.getBoundingClientRect().top + scrollY - 110 });
      };
      view.appendChild(ix);
    }
  };
  q.oninput = draw;
  draw();
}

/* ================================================================ 到着表示 */
async function renderStop(cid, k) {
  const co = await getCompany(cid).catch(() => null);
  const from = history.state && history.state.from;
  setBar({
    title: "", back: { label: from === "#/" ? "お気に入り" : from === "#/search" ? "検索" : "バス停", to: `#/c/${encodeURIComponent(cid)}` },
    right: `<button class="star ${isFav(cid, k) ? "on" : ""}" id="star" aria-label="お気に入り">${ICON.star}</button>`,
  });
  view.innerHTML = `<div class="spinner"></div>`;
  const star = $("#star");
  let stopName = "";
  star.onclick = () => {
    if (isFav(cid, k)) {
      favs = favs.filter(f => !(f.cid === cid && f.k === k));
      toast("お気に入りから外しました");
    } else {
      favs.push({ cid, k, name: stopName, co: co ? co.name : "" });
      toast("お気に入りに追加しました");
    }
    saveFavs();
    star.classList.toggle("on", isFav(cid, k));
    star.classList.add("pop"); setTimeout(() => star.classList.remove("pop"), 160);
  };

  let data = null;
  const draw = () => {
    if (!data) return;
    const now = Date.now(), drift = (now - data.at) / 1000;
    const items = data.items.map(x => ({ ...x, eta: x.eta - drift })).filter(x => x.eta > -30 || x.canceled);
    const live = data.rt && data.rt.ok;
    const first = items.find(x => !x.canceled);
    const pole = d => { const p = data.stop.p[d[6]]; return p ? `<span class="pill">${esc(p)}番のりば</span>` : ""; };
    const nearPill = x => x.near === undefined ? "" : `<span class="pill live">${x.near === 0 ? "到着間近" : x.near + "つ前の停留所"}</span>`;
    const delayPill = x => (x.live && x.delay >= 60 ? `<span class="pill late">${Math.round(x.delay / 60)}分遅れ</span>`
      : x.live && x.near === undefined ? `<span class="pill live">運行情報</span>` : "") + nearPill(x);
    let html = `<div class="stop-head"><div class="name">${esc(data.stop.n)}</div><div class="co">${esc(co ? co.name : "")}</div></div>`;
    if (!first) {
      html += `<div class="empty">${ICON.bus}<br>このバス停から乗れる便はありません。</div>`;
    } else {
      const rest = items.filter(x => x !== first);
      const fm = Math.floor(Math.max(0, first.eta) / 60);
      html += `<div class="hero ${first.eta < 240 ? "soon" : ""}">
        <div class="lab">${first.day ? "本日の運行は終了しました ・ 明日の始発" : "次のバス"}</div>
        <div class="big">${first.day ? `<b>${hhmm(first.dep[0])}</b><span>発</span>`
          : first.eta < 60 ? `<b style="font-size:48px">まもなく到着</b>` : `<span>あと</span><b>${fm}</b><span>分</span>`}</div>
        <div class="meta">${first.dep[1] ? `<span class="pill route">${esc(first.dep[1])}</span>` : ""}${esc(first.dep[2])} 行き</div>
        <div class="meta" style="color:var(--sub);margin-top:4px">${hhmm(first.dep[0])} 発（時刻表）${pole(first.dep)}${delayPill(first)}</div>
      </div>`;
      if (rest.length) {
        html += `<div class="section-h">このあとのバス</div><div class="group">${rest.map(x => `
          <div class="row arr ${x.canceled ? "cancel" : ""}">
            <div class="main"><div class="t">${x.dep[1] ? `<span class="pill route">${esc(x.dep[1])}</span>` : ""}${esc(x.dep[2])} 行き</div>
            <div class="s">${x.day ? "明日 " : ""}${hhmm(x.dep[0])} 発 ${pole(x.dep)}${delayPill(x)}</div></div>
            ${x.day ? `<div class="min">${hhmm(x.dep[0])}</div>` : minHTML(x)}
          </div>`).join("")}</div>`;
      }
    }
    const ago = Math.round((now - data.at) / 1000);
    html += `<div class="status-line"><span><span class="dot ${live ? "live" : ""}"></span>${live
        ? (data.rt.kind === "vp" ? "バスの現在位置から推定" : "リアルタイム運行情報を反映")
        : co && (co.rt || co.vp) ? "運行情報を取得できないため時刻表で表示" : "時刻表から計算"}・${ago < 5 ? "たった今" : ago + "秒前"}更新</span>
      <button id="reload">更新</button></div>
      <p class="note">道路状況により前後することがあります。${co && co.note ? esc(co.note) + "。" : ""}${co && co.lic ? `データ: ${esc(co.src || co.name + "、GTFSデータリポジトリ")}（${esc(co.lic)}）` : ""}</p>`;
    view.innerHTML = html;
    $("#reload").onclick = load;
  };
  const load = async () => {
    try {
      data = await upcoming(cid, k);
      stopName = data.stop.n;
      // 名前の更新（データ側で事業者名などが変わった場合）
      const f = favs.find(f => f.cid === cid && f.k === k);
      if (f && (f.name !== stopName || (co && f.co !== co.name))) { f.name = stopName; if (co) f.co = co.name; saveFavs(); }
      draw();
    } catch (e) {
      view.innerHTML = `<div class="empty">このバス停のデータが見つかりません。<br>データ更新でバス停が廃止・名称変更された可能性があります。</div>`;
    }
  };
  await load();
  timers.push(setInterval(draw, 10000));
  timers.push(setInterval(load, CFG.REFRESH_SEC * 1000));
}

/* ================================================================ ルーティング */
function route(keepScroll) {
  clearTimers();
  const kx = $(".kana-index"); if (kx) kx.remove();
  const h = location.hash.replace(/^#\/?/, "");
  const parts = h.split("/").map(decodeURIComponent);
  $("#toolbar").hidden = true;
  if (!h) { renderHome(); }
  else {
    editing = false;
    if (parts[0] === "search") renderSearch();
    else if (parts[0] === "c" && parts[1] && parts[2]) renderStop(parts[1], parts[2]);
    else if (parts[0] === "c" && parts[1]) renderCompany(parts[1]);
    else go("#/", true);
  }
  if (!keepScroll) window.scrollTo(0, 0);
}
document.addEventListener("click", e => {
  const a = e.target.closest("a[href^='#']");
  if (!a) return;
  e.preventDefault();
  go(a.getAttribute("href"));
});
window.addEventListener("popstate", route);
$("#btnSearch").onclick = () => go("#/search");
$("#btnEdit").onclick = () => { editing = !editing; renderHome(); };
document.addEventListener("visibilitychange", () => { if (!document.hidden && !editing) route(true); });

if (!history.state) history.replaceState({}, "", location.hash || "#/");
route();

if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
})();
