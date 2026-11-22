import json
import time

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="F1 Las Vegas Overtakes", page_icon="🏎️", layout="wide")

API = "https://api.openf1.org/v1"


# ---------------------------------------------------------------- OpenF1 data
def get(endpoint, **params):
    """GET from OpenF1 with retry on rate limit (429). Returns [] when nothing found."""
    for attempt in range(6):
        r = requests.get(f"{API}/{endpoint}", params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(2 + attempt * 2)
            continue
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()
    raise RuntimeError("OpenF1 rate limit hit - wait a minute and reload.")


@st.cache_data(show_spinner="Loading Las Vegas GP from OpenF1...")
def load_race(year):
    sessions = get("sessions", location="Las Vegas", session_name="Race", year=year)
    if not sessions:
        raise ValueError(f"No Las Vegas race found for {year}")
    sk = sessions[-1]["session_key"]

    # drivers -> cards
    drivers = pd.DataFrame(get("drivers", session_key=sk)).drop_duplicates("driver_number")

    # start / finish position from the position feed (first and last record per driver)
    pos = pd.DataFrame(get("position", session_key=sk))
    pos["date"] = pd.to_datetime(pos["date"], utc=True, format="ISO8601")
    pos = pos.sort_values("date")
    start = pos.groupby("driver_number")["position"].first()
    finish = pos.groupby("driver_number")["position"].last()

    res = pd.DataFrame({
        "DriverNumber": drivers["driver_number"].values,
        "Abbreviation": drivers["name_acronym"].values,
        "FullName": drivers["full_name"].str.title().values,
        "TeamName": drivers["team_name"].values,
        "TeamColor": drivers["team_colour"].values,
        "HeadshotUrl": drivers["headshot_url"].values,
    })
    res["GridPosition"] = res["DriverNumber"].map(start).fillna(0).astype(int)
    res["Position"] = res["DriverNumber"].map(finish).fillna(99).astype(int)

    # overtakes (beta endpoint)
    raw = get("overtakes", session_key=sk)
    if not raw:
        return res, pd.DataFrame(), str(sessions[-1].get("circuit_short_name", "Las Vegas"))
    o = pd.DataFrame(raw)
    o["date"] = pd.to_datetime(o["date"], utc=True, format="ISO8601")
    o = o.sort_values("date").reset_index(drop=True)

    # tolerate both possible column namings
    passer = "overtaking_driver_number" if "overtaking_driver_number" in o else "driver_number"
    passed = "overtaken_driver_number"
    abbr = res.set_index("DriverNumber")["Abbreviation"].to_dict()

    # lap number from the laps feed (last lap that had started at overtake time)
    laps = pd.DataFrame(get("laps", session_key=sk))
    laps["date_start"] = pd.to_datetime(laps["date_start"], utc=True, format="ISO8601")
    laps = laps.dropna(subset=["date_start"]).sort_values("date_start")
    lap_no = pd.merge_asof(o[["date"]], laps[["date_start", "lap_number"]],
                           left_on="date", right_on="date_start")["lap_number"]

    ot = pd.DataFrame({
        "Lap": lap_no.fillna(1).astype(int),
        "Overtaker": o[passer].map(abbr).fillna(o[passer].astype(str)),
        "Overtaken": o[passed].map(abbr).fillna(o[passed].astype(str)),
        "NewPos": o["position"].astype(int) if "position" in o else 0,
    })
    return res, ot, str(sessions[-1].get("circuit_short_name", "Las Vegas"))


# ---------------------------------------------------------------- animated race strip
TEMPLATE = """
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, Segoe UI, Roboto, sans-serif; color:#fff; background:transparent; }
  #wrap { background:#0f0f14; border-radius:14px; padding:14px; }
  #top { display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  button { background:#e10600; color:#fff; border:0; border-radius:8px; padding:10px 20px;
           font-size:16px; font-weight:700; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .pill { background:#1d1d27; border-radius:20px; padding:6px 14px; font-size:14px; }
  .pill b { font-size:18px; }
  #banner { margin-top:12px; min-height:46px; padding:10px 14px; border-radius:10px;
            background:linear-gradient(90deg,#e10600,#1d1d27); font-size:18px; }
  #bar { height:6px; background:#2a2a35; border-radius:3px; margin:10px 0 14px; overflow:hidden; }
  #fill { height:100%; width:0; background:#e10600; }
  #track { padding-top:4px; }
  #row { position:relative; }
  .item { position:absolute; left:0; right:0; display:flex; align-items:center; gap:12px;
          border-bottom:1px solid #2a2a35; border-left:4px solid #555; padding:0 10px 0 10px;
          transition: top var(--t,.1s) ease-in-out, background .15s; }
  .item.up   { background:rgba(46,204,113,.25); z-index:5; }
  .item.down { background:rgba(231,76,60,.25); }
  .item .pos  { width:34px; font-weight:800; font-size:17px; text-align:right; }
  .item img   { height:32px; width:32px; border-radius:50%; object-fit:cover; background:#222; flex:none; }
  .item .name { font-weight:700; font-size:16px; white-space:nowrap; }
  .item .team { font-size:12px; margin-left:8px; }
  .item .meta { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .item .cnt  { font-weight:800; font-size:17px; width:40px; text-align:right; }
</style>
<div id="wrap">
  <div id="top">
    <button id="play">&#9654; Play all overtakes</button>
    <label class="pill">Duration <input id="dur" type="range" min="10" max="90" value="30"> <b id="durv">30</b>s</label>
    <span class="pill">Overtake <b id="done">0</b> of <b id="total"></b></span>
  </div>
  <div id="banner">Press play to watch every overtake of the race.</div>
  <div id="bar"><div id="fill"></div></div>
  <div id="track"><div id="row"></div></div>
</div>
<script>
const D = __DATA__;
const drivers = D.drivers, ot = D.overtakes, total = ot.length;
const initial = drivers.slice().sort((a,b)=>a.start-b.start).map(d=>d.abbr);
const totals = {}; ot.forEach(o => totals[o.a] = (totals[o.a]||0)+1);
const $ = id => document.getElementById(id);
const row = $('row'), track = $('track');
let order = initial.slice(), counts = {}, running = false, showTotals = true;
$('total').textContent = total;
$('dur').oninput = () => $('durv').textContent = $('dur').value;

const els = {};
const RH = 44;
drivers.forEach(d => {
  const c = document.createElement('div');
  c.className = 'item'; c.style.borderLeftColor = d.color;
  c.innerHTML = '<div class="pos"></div>' +
    (d.img ? '<img src="'+d.img+'" onerror="this.style.opacity=0">' : '<img style="opacity:0">') +
    '<div class="meta"><span class="name">'+d.name+'</span>' +
    '<span class="team" style="color:'+d.color+'">'+d.team+'</span></div>' +
    '<div class="cnt"><span class="n">0</span></div>';
  row.appendChild(c); els[d.abbr] = c;
});

function render(up, down) {
  row.style.height = (RH * drivers.length) + 'px';
  order.forEach((abbr, i) => {
    const c = els[abbr];
    c.style.height = (RH - 2) + 'px';
    c.style.top = (i * RH + 1) + 'px';
    c.querySelector('.pos').textContent = (i + 1);
    c.querySelector('.n').textContent = showTotals ? (totals[abbr]||0) : (counts[abbr]||0);
    c.classList.toggle('up', abbr === up);
    c.classList.toggle('down', abbr === down);
  });
}
window.addEventListener('resize', () => render());
render();

function play() {
  if (running || !total) return;
  running = true; showTotals = false; counts = {}; order = initial.slice();
  $('play').disabled = true; $('done').textContent = 0; $('fill').style.width = '0';
  render();
  const dur = +$('dur').value * 1000, step = dur / total, t0 = performance.now(), i0 = 0;
  row.style.setProperty('--t', Math.min(0.4, step * 0.0009) + 's');
  let i = i0;
  (function tick() {
    if (i >= total) { finish(); return; }
    const o = ot[i];
    counts[o.a] = (counts[o.a]||0) + 1;
    const ia = order.indexOf(o.a), ib = order.indexOf(o.b);
    if (ia > ib) { order.splice(ia, 1); order.splice(ib, 0, o.a); }
    i++;
    $('banner').innerHTML = '<b>Lap ' + o.lap + '</b> &nbsp;&#128994; <b>' + o.a + '</b> passes &#128308; ' + o.b +
                            ' <span style="float:right;opacity:.8">P' + (order.indexOf(o.a)+1) + '</span>';
    $('done').textContent = i;
    $('fill').style.width = (100 * i / total) + '%';
    render(o.a, o.b);
    setTimeout(tick, Math.max(0, t0 + i * step - performance.now()));
  })();
}
function finish() {
  render();
  $('banner').innerHTML = 'Done &mdash; <b>' + total + '</b> overtakes replayed.';
  $('play').disabled = false; $('play').innerHTML = '&#8635; Replay'; running = false;
}
$('play').onclick = play;
</script>
"""


def build_component(res, ot):
    drivers = []
    for r in res.itertuples():
        color = r.TeamColor if isinstance(r.TeamColor, str) and r.TeamColor else "888888"
        drivers.append({
            "abbr": str(r.Abbreviation),
            "name": str(r.FullName),
            "team": str(r.TeamName),
            "color": "#" + color.lstrip("#"),
            "num": str(r.DriverNumber),
            "img": r.HeadshotUrl if isinstance(r.HeadshotUrl, str) else "",
            "start": int(r.GridPosition) if r.GridPosition else 99,
        })
    overtakes = [{"lap": int(o.Lap), "a": str(o.Overtaker), "b": str(o.Overtaken)}
                 for o in ot.itertuples()]
    data = json.dumps({"drivers": drivers, "overtakes": overtakes}).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", data)


# ---------------------------------------------------------------- app
st.title("🏎️ F1 Las Vegas GP 2026 – Overtakes")

year = 2026  # Las Vegas GP, 21 Nov 2026

try:
    res, ot, event_name = load_race(year)
except Exception as e:
    st.error(f"Could not load data: {e}")
    st.stop()

n = len(ot)
if n == 0:
    st.warning("OpenF1 has no overtake data for this race (the endpoint is beta and can be incomplete). Try again later.")
    st.stop()

st.caption(f"[v5 · list] {year} {event_name} · {n} overtakes (OpenF1) · the list starts in starting order and drivers move up as they overtake")
page = build_component(res, ot).strip()
if hasattr(st, "iframe"):          # newer Streamlit
    st.iframe(page, height=210 + 44 * len(res))
else:                              # older Streamlit
    components.html(page, height=210 + 44 * len(res), scrolling=False)

with st.expander("Full overtake list"):
    try:
        st.dataframe(ot, width="stretch")
    except Exception:
        st.dataframe(ot, use_container_width=True)
