// LizPilot: what the phone panel decides, apart from the page so it can be tested without a phone
// (system/hexaweb/tests/test_panel_logic.py runs it in node). Nothing here touches the page or the network.
// Texts come back in Portuguese, marked with T() for the catalogs; the page translates them with LP.t / LP.tf.
(function (root) {
  "use strict";
  const T = (s) => s;

  const STALE_MS = 3500;  // live data is asked every second: older than this is not "now"
  const BUMP_MIN = 5, BUMP_MAX = 60;  // radars.BUMP_SPEEDS
  const ACC_MIN = 30;  // [log] in 12 routes the Haval ACC was never set below 30 km/h
  const LAT_MS = 2000;  // "steering only" has to last this long, moving, before the band says so

  // The band at the top while driving. Never a claim the data can't back:
  //   none  gray   no data, data too old, car off
  //   fail  red    critical alert
  //   warn  amber  alert asking for the driver
  //   op    green  openpilot steering
  //   lat   green  openpilot steering, the Haval ACC not in control for 2 s while moving: the speed is the driver's
  //   you   blue   the driver steering
  // l: /api/live · link: { online, age, now } in ms · latSince: when "steering only" started, from the last call
  // [log] 3 routes: with openpilot engaged and acc_active false the car is mostly stopped behind another one, held by
  // the ACC (state 2); moving, mostly ≤ 3 s of foot on the gas. Stopped, nothing is said about the ACC.
  function band(l, link, latSince) {
    const fresh = !!(link && link.online && link.age <= STALE_MS);
    if (!l || !fresh) {
      return { cls: "none", who: T("sem dados"), sub: link && link.online ? T("dado atrasado: não dá para dizer quem dirige") : T("sem conexão com o comma"),
               why: null, acc: null, stale: true, latSince: null };
    }
    if (!l.driving) return { cls: "none", who: T("carro desligado"), sub: "", why: null, acc: null, stale: false, latSince: null };
    const steer = !!l.steering, moving = l.v > 0;
    const paused = steer && moving && l.acc_active === false;
    const since = paused ? (latSince == null ? link.now : latSince) : null;
    const lat = paused && link.now - since >= LAT_MS;
    const acc = l.acc_active === true && l.acc_set > 0 ? l.acc_set : null;  // the set speed means something only with the ACC in control
    const status = l.alert_status;  // normal, userPrompt, critical; missing on an older comma
    if (status === "critical") return { cls: "fail", who: T("assuma o volante"), sub: "", why: null, acc, stale: false, latSince: since };  // the alert says why
    let who;
    if (!steer) who = T("você dirigindo");
    else if (lat) who = T("só direção · a velocidade é sua");
    else if (l.driver) who = T("você e openpilot na direção");
    else if (moving && l.acc_active === true) who = T("openpilot dirigindo");
    else who = T("openpilot na direção");  // stopped, or the ACC state not known or too short to say
    let cls = steer ? (lat ? "lat" : "op") : "you";
    if (status === "userPrompt" || (status == null && l.alert)) cls = "warn";
    const why = !l.engaged && l.why && l.why.length ? l.why : null;
    const took = !steer ? null : l.brake ? T("você no freio") : l.gas ? T("você no acelerador") : l.driver ? T("você no volante") : null;
    const sub = why ? T("não engata: {motivos}") : took || (lat ? T("ACC desligado") : steer ? T("volante ativo") : T("openpilot desligado"));
    return { cls, who, sub, why, acc, stale: false, latSince: since };
  }

  // With the car off the comma can't see the panda, the cameras nor the calibration: it only says what it checked.
  // ok: true checked and fine · false checked and not fine · null information, not a check
  function commaChecks(online, h) {
    if (!online) return { cls: "att", title: T("Sem dados do comma"), sub: "", items: [] };
    if (!h || !h.device) return { cls: "", title: T("Conferindo o comma…"), sub: "", items: [] };
    const d = h.device, n = h.learned || {}, items = [];
    if (h.down) items.push(h.down.length ? { ok: false, text: T("Processo parado: {nomes}"), vars: { nomes: h.down.join(", ") } } : { ok: true, text: T("Processos rodando") });
    if (d.temp !== undefined) {
      const ok = !["overheated", "critical"].includes(d.thermal);  // deviceState.thermalStatus
      items.push({ ok, text: ok ? T("Comma {graus} °C") : T("Comma quente: {graus} °C"), vars: { graus: d.temp } });
    }
    if (d.free !== undefined) items.push({ ok: d.free >= 10, text: d.free >= 10 ? T("Disco {livre}% livre") : T("Disco quase cheio: {livre}% livre"), vars: { livre: d.free } });
    if (h.commit) items.push({ ok: null, text: T("Versão {versao}"), vars: { versao: h.commit } });
    // learned only shows up after a trip since the comma started: missing is not a failure
    items.push(n.torque ? { ok: null, text: T("Aprendizado {pct}%"), vars: { pct: n.torque.percent } } : { ok: null, text: T("Aprendizado aparece depois de dirigir") });
    const bad = items.some((i) => i.ok === false);
    return { cls: bad ? "att" : "ok", title: bad ? T("Atenção no comma") : T("Comma ok"),
             sub: T("Conferido agora: processos, temperatura e disco. Câmeras, panda e calibração só com o carro ligado."), items };
  }

  // Centimeters only with both lines seen: one weak line makes the middle a guess.
  function laneText(ln) {
    if (!ln) return null;
    if (!ln.left_ok && !ln.right_ok) return { text: T("Linhas da faixa pouco visíveis") };
    if (!ln.left_ok || !ln.right_ok) return { text: ln.left_ok ? T("Só a linha da esquerda está visível") : T("Só a linha da direita está visível") };
    const left = Math.abs(ln.left), right = Math.abs(ln.right), off = (left - right) / 2;  // > 0: right of the middle
    const cm = Math.round(Math.abs(off) * 100);
    if (cm < 5) return { text: T("No centro da faixa") };
    return { text: off > 0 ? T("Na faixa · {cm} cm à direita") : T("Na faixa · {cm} cm à esquerda"), vars: { cm } };
  }

  // When a change takes effect, per option.
  function applies(o) { return o.restart_needed ? T("Vale na próxima partida do carro.") : T("Vale no próximo engate."); }

  // Speeds offered in the point editor. A bump keeps the speed it was passed at and asks for the one to use.
  function speedChoices(kind, passed, limit) {
    const out = [{ v: null, label: T("Não sei") }];
    if (kind !== "lombada") {
      for (let v = 20; v <= 120; v += 10) out.push({ v, label: String(v) });
      return out;
    }
    const keep = passed >= BUMP_MIN && passed <= BUMP_MAX ? passed : null;
    if (keep) out.push({ v: keep, label: T("Manter {n}"), vars: { n: keep } });
    const speeds = [...new Set([10, 15, 20, 25, 30, 40, 50, limit].filter((v) => v >= BUMP_MIN && v <= BUMP_MAX))].sort((a, b) => a - b);
    for (const v of speeds) if (v !== keep) out.push({ v, label: String(v) });
    return out;
  }

  function keptSpeed(kind, passed) { return kind === "lombada" && passed >= BUMP_MIN && passed <= BUMP_MAX ? passed : null; }

  // What the comma will do at a bump, by the chosen speed. Information only: it never blocks Salvar, and it never
  // says the car will slow down by itself where nothing does.
  // ctx: { alert: RadarAlert on, slowdown: BumpSlowdown on, long: AlphaLongitudinalEnabled, calibrated: LONG_CALIBRATED }
  function bumpMessage(speed, ctx) {
    if (!speed) return "";
    if (!ctx.alert) return T("Com o aviso de radar e lombada desligado, o comma não avisa nem reduz. Ligue em Ajustes.");
    if (speed >= ACC_MIN) return ctx.slowdown ? T("Em teste: o comma toca o − do ACC até essa velocidade.") : T("Hoje só avisa. Ligue reduzir na lombada em Ajustes.");
    if (!ctx.long) return T("O ACC da Haval não vai abaixo de 30 km/h. Aqui o comma só avisa. Para reduzir, é preciso o longitudinal do comma.");
    if (!ctx.calibrated) return T("O longitudinal do comma ainda não foi testado nem calibrado neste carro. Aqui o comma só avisa.");
    return T("Aqui o comma só avisa.");  // the text for a calibrated longitudinal comes with the calibration
  }

  // lizpilot.local: one address for the panel, whatever network the comma is on (mdns.py on the comma).
  // A phone keeps the panel (and its pairing) by address, so the fixed one is offered once, and taken before pairing.
  //   fixed  already there · move  not paired yet: go there before typing the code · offer  paired here: suggest it
  //   ip     the name doesn't answer from this phone (some Android, some networks): stay by address
  const FIXED_HOST = "lizpilot.local";
  function addressPlan(host, reachable, paired) {
    if (String(host || "").toLowerCase() === FIXED_HOST) return "fixed";
    if (!reachable) return "ip";
    return paired ? "offer" : "move";
  }

  // Who had the wheel in a drive (timeline.trip_summary): openpilot alone, openpilot with your hand, you alone, all
  // while moving; stopped is apart. Light, never a score: helping when needed is right, so it's never "less is better".
  const MIN_S = 60;
  function minutes(x) { return Math.round((x || 0) / MIN_S); }
  function duration(min) { return min >= 60 ? { text: T("{h} h {m} min"), vars: { h: Math.floor(min / 60), m: min % 60 } } : { text: T("{m} min"), vars: { m: min } }; }
  function tripSplit(s) {
    if (!s || !(s.moving_s > 0)) return null;
    const pct = (x) => 100 * (x || 0) / s.moving_s;
    const parts = [
      { key: "op", label: T("openpilot sozinho"), min: minutes(s.op_s), km: s.op_km || 0, pct: pct(s.op_s) },
      { key: "help", label: T("openpilot com a sua mão"), min: minutes(s.help_s), km: s.help_km || 0, pct: pct(s.help_s) },
      { key: "you", label: T("você sozinho"), min: minutes(s.you_s), km: s.you_km || 0, pct: pct(s.you_s) },
    ];
    const op = (s.op_s || 0) >= 30;
    return {
      parts, movingMin: minutes(s.moving_s), stopMin: minutes(s.stop_s), km: s.km || 0,
      big: duration(op ? parts[0].min : parts[2].min),
      line: op ? T("de volante por conta do openpilot") : T("você dirigiu nesta viagem"),
      sub: { text: s.stop_s >= 30 ? T("Você ajudou em {ajudou} min e dirigiu {dirigiu} min. Parado no trânsito: {parado} min.") : T("Você ajudou em {ajudou} min e dirigiu {dirigiu} min."),
             vars: { ajudou: parts[1].min, dirigiu: parts[2].min, parado: minutes(s.stop_s) } },
      short: { text: T("{op} de {andando} min com o openpilot"), vars: { op: parts[0].min, andando: minutes(s.moving_s) } },
      accPct: s.acc_s > 0 ? Math.round(pct(s.acc_s)) : null,  // older drives don't have it
    };
  }

  // What the lists and the sums count: a hidden drive and one that didn't go anywhere are out of both.
  function listed(trips) { return (trips || []).filter((t) => !t.hidden && !idle(t)); }
  // The same drive in the list as it is now: each refresh replaces the list, and a row on the screen may hold the old one.
  function currentTrip(trips, tr) { return (trips || []).find((t) => t.id === tr.id) || tr; }
  // No path from the comma means no GPS only once the comma has read the drive: until then it means not yet.
  function pathSettled(tr, pending) { return !!tr.summary && !pending; }
  // The screen's newest marks and every mark from the list of drives: the newest are the fresher copy.
  function mergeMarks(recent, all) {
    const seen = new Set((recent || []).map((m) => m.id));
    return [...(recent || []), ...(all || []).filter((m) => !seen.has(m.id))];
  }

  // The last 7 days added up: a sum, never a streak (a streak would reward not putting the hand on the wheel).
  function weekTotal(trips, nowS) {
    const recent = listed(trips).filter((t) => t.summary && t.mtime >= nowS - 7 * 86400);
    if (!recent.length) return null;
    const op = recent.reduce((a, t) => a + (t.summary.op_s || 0), 0), km = recent.reduce((a, t) => a + (t.summary.km || 0), 0);
    return { op: duration(minutes(op)), km, trips: recent.length };
  }

  // Every drive with a summary since sinceS (0: all of them). Sums and rates, never a score or a streak.
  // Disengagements and alerts per 100 km only from 1 km on, so one short drive doesn't look like a trend.
  function totals(trips, sinceS) {
    const ts = listed(trips).filter((t) => t.summary && t.mtime >= (sinceS || 0));
    if (!ts.length) return null;
    const sum = (k) => ts.reduce((a, t) => a + (t.summary[k] || 0), 0);
    const km = sum("km"), moving = sum("moving_s");
    return { trips: ts.length, km, op: duration(minutes(sum("op_s"))), opPct: moving > 0 ? Math.round(100 * sum("op_s") / moving) : null,
             off: sum("off"), alerts: sum("alerts"), per100: km >= 1 ? { off: 100 * sum("off") / km, alerts: 100 * sum("alerts") / km } : null };
  }
  function monthStart(nowS) { const d = new Date(nowS * 1000); return new Date(d.getFullYear(), d.getMonth(), 1).getTime() / 1000; }
  // The installed version of each drive (its git commit), newest first. Drives summarized before this have none.
  function versionOf(t) { return (t && t.summary && t.summary.commit) || ""; }
  function versions(trips) {
    const seen = [];
    for (const t of trips || []) { const v = versionOf(t); if (v && !seen.includes(v)) seen.push(v); }
    return seen;
  }

  // ----- the whole history on Início (owner, 02/11 01:53): search, filters, days, hidden drives and the map
  // Turned on in the garage and off again is not a drive: out of the lists unless asked for. [hipótese] the limits
  const IDLE_KM = 0.3, IDLE_MOVING_S = 60;
  function idle(t) { const s = t && t.summary; return !!s && (s.km || 0) < IDLE_KM && (s.moving_s || 0) < IDLE_MOVING_S; }
  function fold(s) { return String(s == null ? "" : s).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); }
  function tripStart(t) { return t.mtime - t.minutes * 60; }
  // marks within the drive, a minute of slack on each side (the clock of a mark vs the log's files)
  function tripMarks(t, marks) { const a = tripStart(t) - 60, b = t.mtime + 60; return (marks || []).filter((m) => m.t >= a && m.t <= b); }
  function midnight(s) { const d = new Date(s * 1000); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }
  function daysAgo(s, nowS) { return Math.round((midnight(nowS) - midnight(s)) / 86400000); }
  function dayKey(s) { const d = new Date(s * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
  // What a drive is found by: its day and times in the panel's language, the version, its marks' reasons and notes.
  // marks: [{t, text}] already translated; words: {today, yesterday} likewise
  function tripWords(t, marks, locale, nowS, words) {
    const d = new Date(tripStart(t) * 1000), end = new Date(t.mtime * 1000);
    const hm = { hour: "2-digit", minute: "2-digit" };
    const out = [d.toLocaleDateString(locale, { weekday: "long" }), d.toLocaleDateString(locale, { month: "long" }),
                 d.toLocaleDateString(locale, { day: "2-digit", month: "2-digit" }), d.toLocaleDateString(locale, { day: "numeric", month: "numeric" }),
                 d.toLocaleDateString(locale, { day: "2-digit", month: "2-digit", year: "numeric" }), d.toLocaleTimeString(locale, hm), end.toLocaleTimeString(locale, hm)];
    const ago = daysAgo(tripStart(t), nowS);
    if (ago === 0 && words && words.today) out.push(words.today);
    if (ago === 1 && words && words.yesterday) out.push(words.yesterday);
    const s = t.summary || {};
    if (s.commit) out.push(s.commit);
    if (s.version) out.push(s.version);
    for (const m of tripMarks(t, marks)) out.push(m.text || "");
    return fold(out.join(" "));
  }
  const HISTORY_FILTERS = [["todas", T("Todas")], ["marcas", T("Com marcas")], ["desengates", T("Com desengates")], ["alertas", T("Com alertas")]];
  // The drives to list, newest first. Every word of the search has to be found; a filter narrows further.
  // o: { query, filter, marks: [{t, text}], showHidden, locale, nowS, words }
  // Hidden by the owner and idle drives stay out unless showHidden; the counts say how many each filter would show.
  function history(trips, o) {
    o = o || {};
    const terms = fold(o.query).split(/\s+/).filter(Boolean);
    const all = trips || [];
    const out_ = all.filter((t) => t.hidden || idle(t));
    const pool = o.showHidden ? all : all.filter((t) => !t.hidden && !idle(t));
    const found = terms.length ? pool.filter((t) => { const w = tripWords(t, o.marks, o.locale, o.nowS, o.words); return terms.every((x) => w.includes(x)); }) : pool;
    const test = { todas: () => true, marcas: (t) => tripMarks(t, o.marks).length > 0,
                   desengates: (t) => !!(t.summary && t.summary.off), alertas: (t) => !!(t.summary && t.summary.alerts) };
    const counts = {};
    for (const [k] of HISTORY_FILTERS) counts[k] = found.filter(test[k]).length;
    const f = test[o.filter] ? o.filter : "todas";
    return { list: found.filter(test[f]), counts, filter: f, hidden: out_.filter((t) => t.hidden).length, idle: out_.filter((t) => !t.hidden && idle(t)).length };
  }
  // Drives grouped by the day they started, with the day's km and minutes from the drives that have a summary.
  function byDay(list) {
    const days = [];
    for (const t of list || []) {
      const k = dayKey(tripStart(t));
      let d = days[days.length - 1];
      if (!d || d.key !== k) { d = { key: k, start: tripStart(t), trips: [], km: 0, minutes: 0 }; days.push(d); }
      d.trips.push(t); d.km += (t.summary && t.summary.km) || 0; d.minutes += t.minutes;
    }
    return days;
  }

  // A drive's numbers, as a trip computer shows them. Averages only from 1 km and 1 min on; older summaries lack v_max and run_*.
  // run: the longest stretch without an intervention: a pedal or a push of half a second ends it, a hand resting on the wheel
  // doesn't. A measure of openpilot in that drive, never a record to beat (no streaks: weekTotal).
  function tripNumbers(s) {
    if (!s || !(s.moving_s > 0)) return null;
    const km = s.km || 0;
    return { km, movingMin: minutes(s.moving_s), stopMin: minutes(s.stop_s),
             avg: km >= 1 && s.moving_s >= 60 ? Math.round(km / (s.moving_s / 3600)) : null,
             vmax: s.v_max > 0 ? Math.round(s.v_max) : null,
             runKm: s.run_km > 0 ? s.run_km : null, runMin: s.run_km > 0 ? minutes(s.run_s) : null };
  }
  // Week by week, oldest first, for the trend: km, openpilot's share of the time moving, and the rates per 100 km.
  // Weeks start on Monday; a week without drives shows as empty, so a gap reads as a gap.
  function weekly(trips, nowS, n) {
    const monday = (s) => { const d = new Date(midnight(s)); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d.getTime() / 1000; };
    const weeks = [];
    let start = monday(nowS);
    for (let i = 0; i < (n || 8); i++) {
      weeks.unshift({ start, trips: 0, km: 0, moving_s: 0, op_s: 0, off: 0, alerts: 0 });
      const d = new Date(start * 1000); d.setDate(d.getDate() - 7); start = d.getTime() / 1000;
    }
    for (const t of trips || []) {
      if (!t.summary || t.hidden || idle(t)) continue;
      const w = weeks.find((x, i) => tripStart(t) >= x.start && (i === weeks.length - 1 || tripStart(t) < weeks[i + 1].start));
      if (!w) continue;
      w.trips += 1;
      for (const k of ["km", "moving_s", "op_s", "off", "alerts"]) w[k] += t.summary[k] || 0;
    }
    return weeks.map((w) => ({ start: w.start, trips: w.trips, km: w.km, opPct: w.moving_s > 0 ? Math.round(100 * w.op_s / w.moving_s) : null,
                               off100: w.km >= 1 ? 100 * w.off / w.km : null, alerts100: w.km >= 1 ? 100 * w.alerts / w.km : null }));
  }

  // The map: Web Mercator, 256 px tiles (OpenStreetMap). points: [[lat, lon, who, s], ...] from /api/trip/path
  const TILE = 256, MAP_ZMIN = 3, MAP_ZMAX = 17;
  function mercator(lat, lon, z) {
    const n = TILE * Math.pow(2, z), s = Math.sin(lat * Math.PI / 180);
    return [(lon + 180) / 360 * n, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n];
  }
  // The zoom where the whole path fills w × h with pad on every side: z is the tiles' whole zoom, size their size in pixels
  // (256 to 512, the rest of the zoom), xy the path in the box's pixels.
  function mapFit(points, w, h, pad) {
    if (!points || !points.length || w <= 2 * pad || h <= 2 * pad) return null;
    const w0 = points.map((p) => mercator(p[0], p[1], 0));
    const xs = w0.map((p) => p[0]), ys = w0.map((p) => p[1]);
    const bw = Math.max(...xs) - Math.min(...xs), bh = Math.max(...ys) - Math.min(...ys);
    const room = Math.min(bw > 0 ? (w - 2 * pad) / bw : Infinity, bh > 0 ? (h - 2 * pad) / bh : Infinity);
    const zf = Math.max(MAP_ZMIN, Math.min(MAP_ZMAX, Math.log2(room))), z = Math.floor(zf), k = Math.pow(2, zf);
    const ox = (Math.min(...xs) + bw / 2) * k - w / 2, oy = (Math.min(...ys) + bh / 2) * k - h / 2;
    return { z, size: TILE * Math.pow(2, zf - z), ox, oy, xy: w0.map(([x, y]) => [x * k - ox, y * k - oy]) };
  }
  // The tiles under the box: z/x/y and where each goes, in the box's pixels
  function mapTiles(fit, w, h) {
    if (!fit) return [];
    const n = Math.pow(2, fit.z), sz = fit.size, out = [];
    for (let ty = Math.floor(fit.oy / sz); ty * sz < fit.oy + h; ty++) {
      if (ty < 0 || ty >= n) continue;
      for (let tx = Math.floor(fit.ox / sz); tx * sz < fit.ox + w; tx++) {
        out.push({ z: fit.z, x: ((tx % n) + n) % n, y: ty, size: sz, left: tx * sz - fit.ox, top: ty * sz - fit.oy });
      }
    }
    return out;
  }
  // The copilot's answers per kind, for good (the 48 h list forgets): ok and bad only; "Não aconteceu"/"Não lembro" say nothing
  function tallyAdd(tally, kind, r) {
    const out = { ...(tally || {}) };
    if (!EV_KINDS.includes(kind) || (r !== "ok" && r !== "bad")) return out;
    const k = out[kind] || { ok: 0, bad: 0 };
    out[kind] = { ok: k.ok + (r === "ok" ? 1 : 0), bad: k.bad + (r === "bad" ? 1 : 0) };
    return out;
  }
  // "pode silenciar": 20 answers or more, all "deu certo" (owner, 05/11)
  const SILENCE_MIN = 20;
  function canSilence(k) { return !!k && k.ok >= SILENCE_MIN && !k.bad; }
  // Usar | Melhorar (owner, 05/11): the phone's last choice; before any, LizPilot for every car opens on Usar, the Haval on Melhorar
  function appMode(stored, edition) {
    if (stored === "usar" || stored === "melhorar") return stored;
    return edition === "generic" ? "usar" : "melhorar";
  }
  // what the switch does to "enviar trechos aos desenvolvedores": it follows the mode, only where that option exists and the phone can write it
  function shareFor(mode, opt) {
    if (!opt || !opt.writable) return null;
    const want = mode === "melhorar";
    return !!opt.value === want ? null : want;
  }
  // ----- the Mapa (owner, 05/11: "ao colocar o endereço, ele mostrar no mapa [...] ver se é aquele endereço certinho") -----
  // A view centred on (lat, lon) at the zoom zf, in a w × h box: the same shape as mapFit, to reuse mapTiles
  function mapCenter(lat, lon, zf, w, h) {
    zf = Math.max(MAP_ZMIN, Math.min(18, zf));
    const z = Math.floor(zf), k = Math.pow(2, zf), c = mercator(lat, lon, 0);
    return { z, zf, k, size: TILE * Math.pow(2, zf - z), ox: c[0] * k - w / 2, oy: c[1] * k - h / 2 };
  }
  function mapXY(v, lat, lon) { const c = mercator(lat, lon, 0); return [c[0] * v.k - v.ox, c[1] * v.k - v.oy]; }
  // the place under a pixel of the box: the pin in the middle, after the map was dragged
  function mapLatLon(v, x, y) {
    const n = TILE * v.k, mx = (x + v.ox) / n, my = (y + v.oy) / n;
    const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * my))) * 180 / Math.PI;
    return [Math.round(lat * 1e6) / 1e6, Math.round((mx * 360 - 180) * 1e6) / 1e6];
  }
  function distanceM(a, b) {
    if (!a || !b) return null;
    const r = Math.PI / 180, dl = (b[0] - a[0]) * r, dn = (b[1] - a[1]) * r;
    const h = Math.sin(dl / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dn / 2) ** 2;
    return Math.round(2 * 6371000 * Math.asin(Math.sqrt(h)));
  }
  // The search's long name, short: the first two parts, then the city. Nominatim gives names like "Jardim Botânico de Curitiba,
  // Rua Engenheiro Ostoja Roguski, Região Geográfica Imediata de Curitiba, Curitiba, Paraná, Região Sul, 00000-000, Brasil".
  const PLACE_DROP = /^(brasil|brazil|regi[ãa]o\b.*|\d{5}-?\d{3}|\d{8}|região metropolitana.*|microrregião.*|mesorregião.*)$/i;
  const STATES = new Set(["acre", "alagoas", "amapá", "amazonas", "bahia", "ceará", "distrito federal", "espírito santo", "goiás", "maranhão", "mato grosso",
    "mato grosso do sul", "minas gerais", "pará", "paraíba", "paraná", "pernambuco", "piauí", "rio de janeiro", "rio grande do norte", "rio grande do sul",
    "rondônia", "roraima", "santa catarina", "são paulo", "sergipe", "tocantins"]);
  function shortPlace(name) {
    if (/^\s*-?\d+\.\d+\s*,\s*-?\d+\.\d+\s*$/.test(String(name || ""))) return { title: String(name).trim(), sub: "" };  // a point, not an address
    const parts = String(name || "").split(",").map((x) => x.trim()).filter(Boolean);
    const keep = parts.filter((x, i) => !PLACE_DROP.test(x) && !(i > 0 && STATES.has(x.toLowerCase())));
    if (keep.length <= 3) return { title: keep[0] || "", sub: keep.slice(1).join(", ") };
    return { title: keep[0], sub: [keep[1], keep[keep.length - 1]].join(", ") };
  }
  // "Ir para cá" (owner, 05/11): Waze or Google Maps navigate (voice, traffic, CarPlay, free, no key); the comma's own route
  // only when its navigation is on. It always asks: NAV_REMEMBER is the switch for a day the choice is remembered.
  const NAV_REMEMBER = false;
  function navTargets(commaOn) { return ["waze", "google"].concat(commaOn ? ["comma"] : []); }
  function navChoice(stored, commaOn) { return NAV_REMEMBER && navTargets(commaOn).includes(stored) ? stored : null; }
  function navLink(app, lat, lon) {
    const ll = `${Number(lat).toFixed(6)},${Number(lon).toFixed(6)}`;
    if (app === "waze") return `https://waze.com/ul?ll=${encodeURIComponent(ll)}&navigate=yes`;
    if (app === "google") return `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(ll)}&travelmode=driving`;
    return null;
  }
  // the last places gone to, newest first, no repeats (same point within ~20 m), at most 5
  function navRecent(list, p, keep) {
    return [{ name: p.name, latitude: p.latitude, longitude: p.longitude }, ...(list || []).filter((a) => a && !samePoint(a, p))].slice(0, keep || 5);
  }
  // The same place: within ~20 m (a saved place, a recent one and the route's destination are compared like this)
  function samePoint(a, b) {
    return !!(a && b) && Math.abs(a.latitude - b.latitude) < 2e-4 && Math.abs(a.longitude - b.longitude) < 2e-4;
  }
  function savedPlace(places, p) { return (places || []).find((x) => samePoint(x, p)) || null; }
  // "Quem mostra o caminho?" (MAP-UX-01, owner's D4 and D5, 06/11): asked every time, the last choice first and the only green
  // one; the comma's route always listed, off (with why) when its navigation is off
  function navOptions(last, commaOn) {
    const all = ["waze", "google", "comma"], on = (app) => app !== "comma" || !!commaOn;
    const first = all.includes(last) && on(last) ? last : null;
    return (first ? [first, ...all.filter((a) => a !== first)] : all).map((app, i) => ({ app, on: on(app), last: app === first, main: i === 0 }));
  }
  // What the Mapa shows with no route (MAP-UX-01, step 1): the last places gone to (the saved ones are already the chips on top),
  // the way to edit the saved ones, or, with nothing yet, what can be done
  function mapCard(recent, places, keep) {
    const rec = (recent || []).filter((p) => p && !savedPlace(places, p)).slice(0, keep || 3);
    return { recent: rec, edit: (places || []).length > 0, empty: !rec.length && !(places || []).length };
  }
  // Where the pin's tip goes on a map of height h: the middle of what the bar on top (top) and the card below (cover) leave
  // free, so the place is never under the card (MAP-UX-01, step 4); the pin is 52 px tall
  function pinSpot(h, top, cover) {
    const free = h - top - cover;
    if (free <= 0) return Math.round(h / 2);  // nothing of the map shows: as before
    return Math.round(Math.min(h - cover, top + Math.max(free / 2 + 26, Math.min(52, free))));  // a small phone: the tip on the card's edge
  }
  // The map's centre that puts (lat, lon) at the box's pixel (x, y), zoom zf
  function centerAt(lat, lon, zf, w, h, x, y) {
    const v = mapCenter(lat, lon, zf, w, h), c = mapLatLon(v, w - x, h - y);
    return { lat: c[0], lon: c[1], zf: v.zf };
  }
  // The whole path inside what the bar on top, the card below and the buttons on the right leave free
  function fitBand(points, w, h, band, pad) {
    const bw = w - (band.right || 0), bh = h - (band.top || 0) - (band.bottom || 0);
    const fit = mapFit(points, bw, bh, pad);
    if (!fit) return null;
    const zf = fit.z + Math.log2(fit.size / TILE), c = mapLatLon({ k: Math.pow(2, zf), ox: fit.ox, oy: fit.oy - (band.top || 0) }, w / 2, h / 2);
    return { lat: c[0], lon: c[1], zf };
  }
  // navd's maneuver as one of the arrows the page draws
  function maneuverKind(type, modifier) {
    const t = String(type || ""), m = String(modifier || "");
    if (t === "arrive") return "arrive";
    if (m.includes("uturn")) return "uturn";
    if (m.includes("left")) return m.includes("slight") ? "slight-left" : "left";
    if (m.includes("right")) return m.includes("slight") ? "slight-right" : "right";
    return "straight";
  }

  // ----- Navegar (NAV-03): the screen to glance at with the phone on its mount, a route on. It only reads: no text box,
  // no button that changes anything; the passenger changes the route on the Mapa.
  // Início's pane. Navegar exists only with a destination. The car starting to move goes to Navegar with a route (owner,
  // 06/11) and, without one, leaves the Mapa (the search, the passenger's) for the Painel, as before.
  // o: { hasRoute, driving, wasDriving } — hasRoute null before the comma answered (a reload on Navegar stays there);
  // wasDriving null on the first look, which is not a start
  function homeFor(pane, o) {
    if (pane === "navegar" && o.hasRoute === false) return "painel";
    if (o.driving && o.wasDriving === false) return o.hasRoute ? "navegar" : pane === "mapa" ? "painel" : pane;
    return pane;
  }
  // A distance to show big: metres to the 10 under 1 km, km with one decimal under 10 km, whole km after
  function navDist(m) {
    m = Math.max(0, Number(m) || 0);
    const r = Math.round(m / 10) * 10;
    if (r < 1000) return { n: r, unit: "m", dec: 0 };
    const k = Math.round(m / 100) / 10;
    return k < 10 ? { n: k, unit: "km", dec: 1 } : { n: Math.round(m / 1000), unit: "km", dec: 0 };
  }
  // What the screen shows of navd's route. rt: /api/nav route; null without a route.
  //   turn:   the next maneuver (kind for the arrow, how far, the street and the one after), only while navd talks
  //   arrive: the clock time (ms) and what is left; null until navd has the route
  //   wait:   with a destination, the route isn't there yet (navd computing, no internet, the car off)
  function navView(rt, hasDest, nowMs) {
    if (!hasDest) return null;
    const active = !!(rt && rt.active);
    if (!active) return { wait: true, turn: null, arrive: null };
    const left = Math.max(0, Number(rt.left_s) || 0);
    return {
      wait: false,
      turn: { kind: maneuverKind(rt.type, rt.modifier), dist: navDist(rt.in_m), street: rt.next || "", then: rt.then || "" },
      arrive: { at: nowMs + left * 1000, min: Math.round(left / 60), dist: navDist(rt.left_m) },
    };
  }
  // The car ahead: the time to it (what the ACC keeps), its speed and how much slower (negative) or faster it goes.
  // Under 1 km/h it is the same speed. Without our own speed (the gap needs it) only the distance.
  function leadView(l) {
    const ld = l && l.lead;
    if (!ld) return null;
    const dv = Number.isFinite(l.v) && Number.isFinite(ld.v) ? Math.round(ld.v - l.v) : null;
    return { gap: ld.gap == null ? null : Number(ld.gap), d: ld.d, v: ld.v, dv,
             how: dv == null ? "" : dv <= -1 ? T("{n} km/h mais devagar") : dv >= 1 ? T("{n} km/h mais rápido") : T("mesma velocidade") };
  }
  // The minimap's zoom: closer in town, farther on the road, so the next few hundred metres show at any speed
  // (at zoom 16.5 a 240 px tall map is about 350 m; each step down doubles it)
  function navZoom(kmh) { return kmh >= 90 ? 14.5 : kmh >= 50 ? 15.5 : 16.5; }

  // A wall clock time as the path counts it, from minute 0 of the route: the deleter may have taken the first minutes of the list
  function pathSecond(t, at) { return at - tripStart(t) + ((t.segments && t.segments[0]) || 0) * 60; }
  // ...and back: the wall clock (seconds) of a path second, for Rever viagem's bar and list (MAP-UX-01, D6)
  function rideClock(t, s) { return tripStart(t) + s - ((t.segments && t.segments[0]) || 0) * 60; }
  // Rever viagem draws who drove as Início does: openpilot, openpilot with your hand or foot, you (MAP-UX-01, D6)
  function rideWho(p) {
    const w = p && p.who;
    if (w === "wheel" || w === "gas" || w === "brake" || w === "pedal") return "help";
    if (w === "op" || (!w && p && p.on)) return "op";
    return "you";
  }
  // The path point closest in time to s seconds from the start of the drive
  function nearestAt(points, s) {
    let best = -1, gap = Infinity;
    (points || []).forEach((p, i) => { const g = Math.abs(p[3] - s); if (g < gap) { gap = g; best = i; } });
    return best;
  }

  // ----- the panel's KPIs (owner, 02/11 14:54): every number with its context, never a score or a streak
  // A set of drives in four numbers. Intervention = openpilot let go, or the hand on the wheel for 0.5 s (timeline.py).
  // Each is null without enough under it: 1 km for a rate, 30 s of steering for the wobble, 20 samples for the lane.
  function kpiOf(trips) {
    const ts = (trips || []).filter((t) => t.summary && !t.hidden && !idle(t));
    const sum = (k) => ts.reduce((a, t) => a + (t.summary[k] || 0), 0);
    // the wobble only from summaries that measure it on straights (wob_s, CTO review of 05): the older ones measured curves
    const straight = ts.filter((t) => t.summary.wob_s != null), wobS = straight.reduce((a, t) => a + t.summary.wob_s, 0);
    const wobN = straight.reduce((a, t) => a + (t.summary.wob_n || 0), 0), wobSS = straight.reduce((a, t) => a + (t.summary.wob_ss || 0), 0);
    const km = sum("km"), moving = sum("moving_s"), laneN = sum("lane_n");
    // no "km per intervention": a hand on the wheel isn't an intervention, and the owner hasn't said yet what is (CTO review of 05)
    return { trips: ts.length, km,
             opPct: moving >= 60 ? 100 * sum("op_s") / moving : null,
             wobble: wobS >= WOBBLE_MIN_S && wobN ? Math.sqrt(wobSS / wobN) : null,
             lanePct: laneN >= 20 ? 100 * sum("lane_ok") / laneN : null,
             alerts100: km >= 1 ? 100 * sum("alerts") / km : null,
             off100: km >= 1 ? 100 * sum("off") / km : null };
  }
  const WOBBLE_MIN_S = 30;  // s on straights: a short drive has no wobble rather than a wrong one
  const KPI_MIN_KM = 30;  // a window or an average with less road says nothing: no number, no arrow
  function enough(k) { if (k.km >= KPI_MIN_KM) return k; const out = { trips: k.trips, km: k.km, few: true }; for (const x of Object.keys(KPI_BETTER)) out[x] = null; return out; }
  // Which way is better for each KPI, and how big a change counts (relative, or points for a percent)
  const KPI_BETTER = { opPct: 1, lanePct: 1, wobble: -1, alerts100: -1, off100: -1 };
  function kpiDelta(key, now, before) {
    if (now == null || before == null) return null;
    const pts = key === "opPct" || key === "lanePct";
    const d = pts ? now - before : (before > 0 ? (now - before) / before : null);
    if (d == null) return null;
    const small = pts ? Math.abs(d) < 2 : Math.abs(d) < 0.1;
    return { d, pts, trend: small ? 0 : Math.sign(d) * KPI_BETTER[key] };  // trend: 1 better, -1 worse, 0 about the same
  }
  // The last `days` against the `days` before: the four numbers of Início, with their trend
  function kpis(trips, nowS, days) {
    const span = (days || 28) * 86400;
    const now = enough(kpiOf((trips || []).filter((t) => tripStart(t) >= nowS - span)));
    const before = enough(kpiOf((trips || []).filter((t) => tripStart(t) >= nowS - 2 * span && tripStart(t) < nowS - span)));
    const out = { now, before };
    for (const k of Object.keys(KPI_BETTER)) out[k] = kpiDelta(k, now[k], before[k]);
    return out;
  }
  // One drive against the average of the last 4 weeks: "acima da média", "na média", "abaixo da média"
  function tripVsAverage(t, trips, nowS) {
    const mine = kpiOf([t]), avg = enough(kpiOf((trips || []).filter((x) => x.id !== t.id && tripStart(x) >= nowS - 28 * 86400)));
    const out = {};
    for (const k of ["opPct", "wobble"]) {
      const d = kpiDelta(k, mine[k], avg[k]);
      out[k] = { value: mine[k], word: d == null ? null : d.trend > 0 ? T("melhor que a sua média") : d.trend < 0 ? T("pior que a sua média") : T("na sua média") };
    }
    return out;
  }
  // The version now against the last one tested (owner, 03/11: several updates went out untested). Tested: drives on it with
  // KPI_MIN_KM of road; if none has that much, the last with any drive. The channel versions in between come along in the
  // comparison, from the release notes: `between` those with no drive at all, `short` those with too little road to be the
  // one tested, `earlier` those tested but driven before it (a rollback). Their km are on neither side. Under 200 km a side:
  // no verdict
  const VERSION_MIN_KM = 200;
  function versionCompare(trips, releases) {
    const ts = (trips || []).filter((t) => t.summary && !t.hidden && !idle(t) && versionOf(t)).sort((a, b) => b.mtime - a.mtime);
    if (!ts.length) return null;
    const now = versionOf(ts[0]), mine = (v) => ts.filter((t) => versionOf(t) === v);
    const others = versions(ts).filter((v) => v !== now);
    const before = others.find((v) => kpiOf(mine(v)).km >= KPI_MIN_KM) || others[0];
    if (!before) return null;
    const a = kpiOf(mine(now)), b = kpiOf(mine(before));
    const few = a.km < VERSION_MIN_KM || b.km < VERSION_MIN_KM;
    const rows = ["opPct", "wobble", "alerts100"].map((k) => ({ key: k, now: a[k], before: b[k], delta: few ? null : kpiDelta(k, a[k], b[k]) }));
    const judged = rows.filter((r) => r.delta);
    const rel = (releases || []).map((r) => r.commit), i = rel.indexOf(now), j = rel.indexOf(before);
    const mid = i >= 0 && j > i ? rel.slice(i + 1, j) : null;  // null: the notes don't know one of the two
    const km = (x) => kpiOf(mine(x)).km;
    const between = mid && mid.filter((x) => !mine(x).length), short = mid && mid.filter((x) => mine(x).length && km(x) < KPI_MIN_KM);
    const earlier = mid && mid.filter((x) => km(x) >= KPI_MIN_KM);
    return { now, before, a, b, rows, better: few ? null : judged.filter((r) => r.delta.trend > 0).length, judged: few ? null : judged.length, few,
             between, short, earlier };
  }
  // Week by week of one KPI, oldest first, for a sparkline (null weeks stay null: a gap reads as a gap)
  function kpiWeeks(trips, nowS, key, n) {
    const out = [];
    for (let i = (n || 8) - 1; i >= 0; i--) {
      const end = nowS - i * 7 * 86400, start = end - 7 * 86400, wk = kpiOf((trips || []).filter((t) => tripStart(t) >= start && tripStart(t) < end));
      out.push(wk.km >= KPI_MIN_KM / 4 ? wk[key] : null);  // a week needs a quarter of the window's road
    }
    return out;
  }

  // Shadow mode (selfdrive/hexa/shadow.py), added up over the last 7 days: what openpilot's longitudinal would have done
  // while the Haval ACC drove. Null until a drive has it (openpilot engaged with the ACC driving).
  function weekShadow(trips, nowS) {
    const recent = listed(trips).filter((t) => t.summary && t.mtime >= nowS - 7 * 86400 && (t.summary.sh_on_s || 0) > 0);
    if (!recent.length) return null;
    const sum = (k) => recent.reduce((a, t) => a + (t.summary[k] || 0), 0);
    const gapT = sum("sh_gap_t");
    return { hard: sum("sh_hard"), earlier: sum("sh_earlier"), weaker: sum("sh_weaker"), fcw: sum("sh_fcw"),
             gap: gapT > 0 ? sum("sh_gap_s") / gapT : null, minutes: Math.round(sum("sh_on_s") / 60), trips: recent.length };
  }
  // What the car went through in the last 7 days (selfdrive/hexa/sensors.py): maxima, minima and rates, never a score.
  function weekCar(trips, nowS) {
    const recent = listed(trips).filter((t) => t.summary && t.mtime >= nowS - 7 * 86400 && (t.summary.se_grade_up || t.summary.se_v12_min));
    if (!recent.length) return null;
    const S = (k) => recent.reduce((a, t) => a + (t.summary[k] || 0), 0);
    const M = (k) => { const v = recent.map((t) => t.summary[k] || 0).filter((x) => x > 0); return v.length ? Math.max(...v) : null; };
    const mins = recent.map((t) => t.summary.se_v12_min || 0).filter((x) => x > 0);
    const wheelT = S("se_wheel_t");
    return { up: M("se_grade_up"), down: M("se_grade_down"), roll: M("se_roll"), decel: M("se_decel"), lat: M("se_lat"),
             v12: mins.length ? Math.min(...mins) : null, wheels: wheelT > 30 ? S("se_wheel_s") / wheelT : null };
  }

  // Missions (selfdrive/hexa/missions.py): the tests the car still needs. What counts as done comes from the CTO (done),
  // from the owner's answers, or from the drive summaries the comma already keeps, read here with the car off.
  // A "Fiz agora" mark is a clue for the CTO, never done by itself. The level is the system's, never the driver's.
  const SLOW_MISSION_S = 600, NIGHT_MIN_S = 1800, HIGHWAY_KMH = 90, HIGHWAY_KM = 15, MISSION_VERSION_KM = 200, MARKS_IN_A_TRIP = 10;
  // the comma approval checks (MISS-01): the car's signals the drive summary saw change (timeline sg_*, signals_seen.py).
  // Three starts within IGNITION_WINDOW_S are the "turn it off and on 3 times"; the four gears are P, R, N and D
  const IGNITION_WINDOW_S = 900, GEARS_PRND = 15;
  // a new car's approval (MISS-01 B): a minute engaged, and half an hour of openpilot steering in one drive with no fault
  const ENGAGED_MIN_S = 60, LAT_NO_FAULT_S = 1800;
  // the calibration trail (MISS-01 C): what the comma learned (timeline cb_*). The steer ratio counts after 20 km with it
  // valid; the wheels match the GPS within 3% over a minute above 8 m/s (10 readings a second)
  const RATIO_KM = 20, WHEELS_GPS_N = 600, WHEELS_GPS_TOL = .03;
  // The week of a time (Monday 00:00 local, as a day number): 200 km in one week, whatever versions it had
  function weekOf(s) { const d = new Date(s * 1000); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return Math.round(d.getTime() / 86400000); }
  function missionAuto(m, trips) {
    const ts = (trips || []).filter((t) => t.summary && !t.hidden), S = (t) => t.summary;
    const night = (t) => { const h = new Date(tripStart(t) * 1000).getHours(); return h >= 19 || h < 5; };
    switch (m.detect) {
      case "slow10": return ts.some((t) => (S(t).slow_s || 0) >= SLOW_MISSION_S) ? 1 : 0;
      case "night30": return ts.some((t) => (S(t).moving_s || 0) >= NIGHT_MIN_S && night(t)) ? 1 : 0;
      case "highway": return ts.some((t) => (S(t).v_max || 0) >= HIGHWAY_KMH && (S(t).km || 0) >= HIGHWAY_KM) ? 1 : 0;
      case "marks10": return ts.some((t) => (S(t).marks || 0) >= MARKS_IN_A_TRIP) ? 1 : 0;
      case "installed": return m.arg && ts.some((t) => versionOf(t).startsWith(m.arg)) ? 1 : 0;  // m.arg: the newest version (missions.public)
      case "belt": return ts.some((t) => (S(t).sg_belt || 0) > 0) ? 1 : 0;
      case "doors": return Math.min(m.total, Math.max(0, ...ts.map((t) => S(t).sg_door || 0)));  // one stop: each door, then the trunk
      case "gears": return ts.some((t) => ((S(t).sg_gears || 0) & GEARS_PRND) === GEARS_PRND) ? 1 : 0;
      case "pedals": return ["sg_brake", "sg_gas"].filter((k) => ts.some((t) => (S(t)[k] || 0) > 0)).length;
      case "blinkers": return ["sg_left", "sg_right", "sg_hazard"].filter((k) => ts.some((t) => (S(t)[k] || 0) > 0)).length;
      case "bsm": return ["sg_bsm_l", "sg_bsm_r"].filter((k) => ts.some((t) => (S(t)[k] || 0) > 0)).length;
      case "ign3": { const at = ts.map(tripStart).sort((a, b) => a - b);  // the most starts within the window
        let best = 0; at.forEach((s, i) => { best = Math.max(best, at.slice(i).filter((x) => x - s <= IGNITION_WINDOW_S).length); });
        return best >= 2 ? Math.min(m.total, best) : 0; }  // one start is any drive: the check is turning it off and on
      case "engaged": return ts.some((t) => (S(t).on_s || 0) >= ENGAGED_MIN_S) ? 1 : 0;
      case "brake_off": return ts.some((t) => (S(t).brake || 0) > 0) ? 1 : 0;  // the driver's brake with openpilot engaged
      // a summary from before the faults were counted (SUMMARY_VERSION 7) knows nothing of them: it proves nothing
      case "lat30": return ts.some((t) => (S(t).on_s || 0) >= LAT_NO_FAULT_S && S(t).sg_steer_fault === 0) ? 1 : 0;
      case "calibrated": return ts.some((t) => (S(t).cb_calibrated || 0) >= 1) ? 1 : 0;
      case "ratio": return ts.some((t) => (S(t).cb_sr_km || 0) >= RATIO_KM && (S(t).cb_sr || 0) > 0) ? 1 : 0;
      case "torque": return ts.some((t) => (S(t).cb_tq_perc || 0) >= 100) ? 1 : 0;
      case "wheels_gps": return ts.some((t) => { const n = S(t).cb_v_n || 0, g = S(t).cb_v_gps || 0;
        return n >= WHEELS_GPS_N && g > 0 && Math.abs((S(t).cb_v_wheel || 0) / g - 1) <= WHEELS_GPS_TOL; }) ? 1 : 0;
      case "km200": { const km = {}; ts.forEach((t) => { const w = weekOf(tripStart(t)); km[w] = (km[w] || 0) + (S(t).km || 0); });  // a week: versions change daily
        return Object.values(km).some((x) => x >= MISSION_VERSION_KM) ? 1 : 0; }
      default: return 0;
    }
  }
  // The approval report (MISS-01 B): comma's porting checklist, filled from the approval trails, in English for the pull
  // request. Only what the phone knows: no car name, VIN, place or date beyond the day a drive proved it
  const REPORT_LINES = {
    "ap-reconhece": "Identifies the car on repeated cold starts without a forced fingerprint",
    "ap-ligar": "Ignition off and on: recognized and engages after each start, no dash warnings",
    "ap-dormir": "Car asleep: the device shuts down and doesn't keep the car awake",
    "ap-cinto": "Seatbelt", "ap-portas": "Doors", "ap-cambio": "Gears (P, R, N, D and others)", "ap-pedais": "Brake and gas pedals",
    "ap-piscas": "Turn signals and hazard", "ap-cego": "Blind spot, each side", "ap-freio-mao": "Parking brake",
    "ap-engatar": "Engages on the car's cruise button and drives", "ap-freio-desliga": "Brake press disengages",
    "ap-cancelar": "The cancel button disengages", "ap-botoes": "Cruise + and − buttons reach openpilot",
    "ap-curvas": "Steering direction and torque, both ways", "ap-sem-falha": "30 min of openpilot steering with no steering fault",
    "ap-perto-zero": "Steering near 0 km/h", "ap-rota": "Public route for the PR", "ap-relatorio": "This report",
  };
  const REPORT_GROUPS = { "ap-carro": "Vehicle and identification", "ap-sinais": "Vehicle state", "ap-engate": "Buttons and engagement",
                          "ap-direcao": "Lateral control", "ap-envio": "Submission" };
  function approvalReport(cat, prog, trips) {
    const ms = ((cat && cat.missions) || []).filter((m) => String(m.trail).startsWith("ap-"));
    if (!ms.length) return "";
    const out = ["# comma porting checklist: what this car has shown", "",
                 "Filled by LizPilot from the drive logs and the driver's checks. [x] done · [~] partly · [ ] to do.", ""];
    for (const [g, title] of Object.entries(REPORT_GROUPS)) {
      const gm = ms.filter((m) => m.trail === g);
      if (!gm.length) continue;
      out.push(`## ${title}`);
      for (const m of gm) {
        const p = (prog && prog[m.key]) || { d: 0, total: m.total }, line = REPORT_LINES[m.key] || m.title;
        const byLog = m.detect && missionAuto(m, trips) >= m.total;
        const how = byLog ? "seen in the drive logs" : p.checked ? "confirmed by the driver" : p.full ? "marked while driving, to check in the logs" : "";
        const box = p.full ? "[x]" : p.d > 0 ? "[~]" : "[ ]";
        out.push(`- ${box} ${line}${m.total > 1 ? ` (${Math.min(p.d, m.total)}/${m.total})` : ""}${how ? `: ${how}` : ""}`);
      }
      out.push("");
    }
    return out.join("\n");
  }
  // A mark's result (missions.py RESULTS): "Fiz agora" and "Deu certo" wait for the CTO as a mission advanced; "Não deu" and
  // "Parei o teste" wait too, but never as progress (CTO review of app-08)
  const ADVANCED = ["done", "ok"];
  function advanced(p) { return !p.result || ADVANCED.includes(p.result); }
  // A mission's history (owner, 03/11: "saber o quanto performou"): the tries checked in the log, newest first, and the
  // owner's marks, each one "conferida" once a try covers its day. A mark is never a try by itself.
  // a try checks the marks up to its time, or its whole day; a reference ("base", before the change) checks none
  function dayEnd(iso) { const s = String(iso || ""), t = Date.parse(s.includes("T") ? s : s + "T23:59:59"); return isNaN(t) ? 0 : t / 1000; }
  function checkedUntil(m) { return ((m && m.tries) || []).filter((x) => x.result !== "base").reduce((a, x) => Math.max(a, dayEnd(x.date)), 0); }
  function markChecked(m, p, until) { return !!until && (p.t || 0) <= until; }
  function missionHistory(m, pending) {
    const until = checkedUntil(m);
    const marks = (pending || []).filter((p) => p.key === m.key).map((p) => ({ t: p.t || 0, result: p.result || "done", checked: markChecked(m, p, until) }));
    return { tries: ((m && m.tries) || []).slice().reverse(), marks: marks.sort((a, b) => b.t - a.t) };
  }
  // What was already checked or is complete, the latest try first: the "Feitas" screen. A reference alone ("base") is the
  // number before the change, not a check
  function missionsChecked(cat, prog) {
    const last = (m) => (m.tries && m.tries.length ? m.tries[m.tries.length - 1].date : "");
    const checked = (m) => (m.tries || []).some((x) => x.result !== "base");
    return ((cat && cat.missions) || []).filter((m) => checked(m) || (prog && prog[m.key] && prog[m.key].full))
      .map((m, i) => ({ m, i })).sort((a, b) => (last(b.m) > last(a.m) ? 1 : last(b.m) < last(a.m) ? -1 : a.i - b.i)).map((x) => x.m);
  }
  // Two layers (owner, 05/11): what the owner did counts at once ("Fiz agora", "Deu certo"), waiting for the check; the CTO
  // then accepts it (a try "ok"/"partial", and `done` raised) or returns it (a try "back" or "bad": it counts no more, and
  // says why, until the owner does it again). d is both layers; accepted is the CTO's alone.
  function missionProgress(cat, state, trips) {
    const st = state || {}, answers = st.answers || {}, out = {};
    for (const m of (cat && cat.missions) || []) {
      // a detector counts whatever the mission's kind: the approval checks keep their Marcar, and the log ticks them too
      const accepted = Math.min(m.total, Math.max(m.done || 0, answers[m.key] || 0, m.detect ? missionAuto(m, trips) : 0));
      const until = checkedUntil(m);  // a mark a try already checked waits for nothing
      const mine = (st.pending || []).filter((p) => p.key === m.key && !markChecked(m, p, until)), marks = mine.filter(advanced).length;
      const byYou = Math.min(marks, m.total - accepted), d = accepted + byYou;
      out[m.key] = { d, accepted, byYou, total: m.total, full: d >= m.total, checked: accepted >= m.total, locked: !!m.lock && d < m.total,
                     toCheck: byYou, fails: mine.length - marks, active: (st.active || []).includes(m.key), returned: missionReturned(m, mine) };
    }
    return out;
  }
  // The CTO's last word, when it sent the mission back and the owner hasn't done it again since: {date, reason}, or null
  const RETURNED = ["back", "bad"];
  function missionReturned(m, open) {
    const tr = ((m && m.tries) || []).filter((x) => x.result !== "base"), last = tr[tr.length - 1];
    if (!last || !RETURNED.includes(last.result) || open.some(advanced)) return null;
    return { date: last.date, reason: last.measured || "" };
  }
  function missionLevel(cat, prog) {
    const keys = Object.keys(prog), n = keys.filter((k) => prog[k].full).length, total = keys.length || 1, levels = (cat && cat.levels) || [[0, ""]];
    let i = 0;
    levels.forEach((l, k) => { if (n >= Math.ceil(l[0] * total)) i = k; });
    const nx = levels[i + 1];
    return { n, total, i, name: levels[i][1], next: nx ? { name: nx[1], left: Math.ceil(nx[0] * total) - n } : null };
  }
  // A trail's missions, what is left first: half done, open, the CTO's, locked, done
  // trails: the catalog's trail keys in order (missions.py TRAILS). Given, the trail comes first and the rank within it: the
  // approval of the car by comma leads, whatever else is under way (owner, 06/11)
  function missionOrder(list, prog, trails) {
    const rank = (m) => { const p = prog[m.key]; return p.full ? 4 : p.locked ? 3 : m.how === "cto" ? 2 : p.d || p.toCheck ? 0 : 1; };  // the CTO's open ones after the owner's
    const at = (m) => { const i = trails ? trails.indexOf(m.trail) : -1; return i < 0 ? 99 : i; };
    return list.slice().sort((a, b) => at(a) - at(b) || rank(a) - rank(b));
  }
  function missionTrails(cat, prog) {
    return ((cat && cat.trails) || []).map((tr) => {
      const ms = cat.missions.filter((m) => m.trail === tr.key), d = ms.filter((m) => prog[m.key].full).length;
      const next = missionOrder(ms, prog).find((m) => !prog[m.key].full && !prog[m.key].locked);
      return { key: tr.key, title: tr.title, d, total: ms.length, seal: d === ms.length, next: next ? next.title : null };
    });
  }
  // What moved between two looks (before the drive, after it): for the result screen. moved: it advanced; a "não deu"
  // alone is listed (fails) but never counted as a mission that advanced
  function missionDiff(before, after) {
    return Object.keys(after).filter((k) => before[k] && (after[k].d > before[k].d || after[k].toCheck > before[k].toCheck || (after[k].fails || 0) > (before[k].fails || 0)))
      .map((k) => {
        const checks = Math.max(0, after[k].toCheck - before[k].toCheck), fails = Math.max(0, (after[k].fails || 0) - (before[k].fails || 0));
        return { key: k, from: before[k].d, to: after[k].d, total: after[k].total, full: after[k].full, checks, fails, moved: after[k].d > before[k].d || checks > 0 };
      });
  }
  // The missions that go on a drive (missions.py can_take)
  function canTake(m) { return !!m && !m.lock && (m.how === "auto" || m.how === "marca" || (m.how === "parado" && !!m.drive)); }
  // The route's missions still open, in the CTO's order (missions.py route_keys): what the comma takes with "pack". The
  // route goes along with what the owner picked, never in its place (owner, 03/11)
  function isRoute(cat, key) { const r = cat && cat.route; return !!r && (key === r.key || (r.old || []).includes(key)); }
  function routeKeys(cat, answers) {
    const a = answers || {}, open = (m) => Math.max(m.done || 0, a[m.key] || 0) < m.total;
    return ((cat && cat.missions) || []).filter((m) => m.route && canTake(m) && open(m)).sort((x, y) => x.route - y.route).map((m) => m.key);
  }
  // Missions chosen away from the comma (owner, 03/11: D3): take, drop and the route wait on the phone, in order, and
  // go by the same POST /api/missions when the comma answers. Only these: nothing else waits for the comma.
  const QUEUE_ACTIONS = ["take", "drop", "pack"];
  const MAX_PACK = 12;  // missions.py MAX_PACK
  function queueAdd(queue, item) {
    const q = (queue || []).slice();
    if (!item || !QUEUE_ACTIONS.includes(item.action) || typeof item.key !== "string") return q;
    if (item.action === "pack") return q.filter((x) => x.action !== "pack").concat([item]);  // once, after what was picked
    const after = q.map((x) => x.action).lastIndexOf("pack");
    const i = q.findIndex((x, k) => k > after && x.key === item.key);
    if (i < 0) return q.concat([item]);
    if (q[i].action !== item.action) q.splice(i, 1);  // taken and dropped again before the comma saw it: nothing to send
    return q;
  }
  // What the phone shows as going along: the comma's state with what still waits on top
  function applyQueue(state, queue, cat) {
    let active = ((state && state.active) || []).slice();
    for (const x of queue || []) {
      if (x.action === "take" && !active.includes(x.key) && active.length < MAX_PACK) active.push(x.key);
      else if (x.action === "drop") active = active.filter((k) => k !== x.key);
      else if (x.action === "pack" && isRoute(cat, x.key)) active = [...new Set(active.concat(routeKeys(cat, state && state.answers)))].slice(0, MAX_PACK);
    }
    return Object.assign({}, state || {}, { active });
  }
  // What still waits to reach the comma, as it will land: the comma's state with the queue on top, minus what it already has
  function queuedKeys(queue, cat, state) {
    const had = new Set(((state && state.active) || [])), now = applyQueue(state, queue, cat).active;
    return now.filter((k) => !had.has(k) && (queue || []).some((x) => x.key === k || x.action === "pack"));
  }

  // The passenger's copilot (owner, 03/11): what the live data says fits now, so the right test comes up at the right moment.
  // Only reads /api/live, which the drive screen already asks every second: nothing more for the comma.
  const COP_STOP_KMH = 1, COP_LONG_S = 60, COP_MOVING_KMH = 5, COP_CITY_KMH = 60;
  function copilotMoment(l, stoppedS) {
    if (!l || !l.driving) return [];
    const v = l.v || 0, lead = !!l.lead, out = [];
    if (v < COP_STOP_KMH) {
      out.push(lead ? "stop_queue" : "stop_free");
      if ((stoppedS || 0) >= COP_LONG_S) out.push("stop_long");
    } else {
      if (v >= COP_MOVING_KMH) out.push("moving");
      if (v >= COP_MOVING_KMH && v <= COP_CITY_KMH) out.push("city");
      if (lead && v >= 40 && v <= 60) out.push("follow_mid");
      if (lead && v < 30) out.push("slow_follow");
    }
    return out;
  }
  // What the copilot may say about now: live only with fresh data from a car that is on (CTO review of app-08: old data
  // never passes as "now"). state: live, stale (the data stopped), off (the car is off), none (no live data at all)
  function copilotNow(l, link, stoppedS) {
    if (!l || !link || !link.online) return { state: "none", moment: [] };
    if (!(link.age <= STALE_MS)) return { state: "stale", moment: [], age: Math.round(link.age / 1000) };
    if (!l.driving) return { state: "off", moment: [] };
    return { state: "live", moment: copilotMoment(l, stoppedS) };
  }
  // Since when the car is stopped: one old tick says nothing, so it keeps counting
  function stopClock(prev, l, stale, now) {
    if (stale) return prev == null ? null : prev;
    if (!l || !l.driving) return null;
    return (l.v || 0) < COP_STOP_KMH ? (prev == null ? now : prev) : null;
  }
  // The copilot is the passenger's: opening it always asks, parked too, once per drive. It closes with the car off, and when
  // the car moves with nobody confirmed (owner, 03/11)
  function copilotGate(gate, car) {
    const ok = !!(gate && gate.ok) && !!(car && car.on);
    return { ok, ask: !ok, close: !(car && car.on) || (!!car.moving && !ok) };
  }
  // What was answered on this drive (the comma's pending marks since it started): reopening the copilot keeps it
  function tripAnswers(pending, since) {
    const out = {};
    for (const p of pending || []) if ((p.t || 0) >= since) out[p.key] = p.result || "done";
    return out;
  }
  // The next drive's missions still open, in their order, the ones that fit now first
  function copilotOrder(cat, active, prog, moment) {
    const ms = (active || []).map((k) => ((cat && cat.missions) || []).find((m) => m.key === k)).filter((m) => m && prog[m.key] && !prog[m.key].full && !m.lock);
    const fits = (m) => !!m.when && (moment || []).includes(m.when);
    return ms.map((m, i) => ({ key: m.key, fits: fits(m), i })).sort((a, b) => (b.fits - a.fits) || (a.i - b.i)).map(({ key, fits: f }) => ({ key, fits: f }));
  }

  // The copilot's events (owner, 03/11: "não sei se leu de agora ou trouxe de antes"): what just happened, read from /api/live
  // the moment it happened, never a mission that only fits. Each one keeps the time it was read; the phone shows its age and
  // colours it by that: live up to 30 s, recent up to 2 min, then past. Pure: the page keeps st between calls, nothing more
  // goes to the comma.
  const EV_LIVE_S = 30, EV_RECENT_S = 120, EV_KEEP = 40;
  const EV_COOLDOWN_S = { lead_brake: 15, cut_in: 10, disengage: 10 };
  const EV_MISSIONS = { lead_brake: ["frear-antes"], pull_away: ["sair-sozinho", "fila"], stop_lead: ["parar-perto", "fila"],
                        stop_free: ["parar-de-vez", "freio-alavanca"], go_free: ["sinal-abrindo"], stop_long: ["parada-longa"],
                        bump: ["lombada", "lombada-faixa"] };
  // with no open mission, "não" still becomes a mark with this reason (marks.py DEFAULT_REASONS)
  const EV_REASON = { lead_brake: "freada", pull_away: "outro", stop_lead: "freada", stop_free: "freada", go_free: "outro", stop_long: "outro",
                      cut_in: "freada", disengage: "soltou", bump: "outro" };
  // The car ahead braking, on route 32 (CTO, 03/11): at 35–60 m the comma's own lead speed swings 12 km/h in 3 s (p50), so a
  // speed drop alone fired 26–34 times in 79 min, a third of them real. With the comma's filtered deceleration (lead.a, aLeadK)
  // twice in a row, the same car, within 40 m and 8 km/h down: about 18 times, two thirds real, 12 of the 14 real ones caught.
  // [hipótese] from one route: check it on the next ones.
  const LEAD_BRAKE_KMH = 8, LEAD_BRAKE_S = 3, LEAD_BRAKE_MIN_KMH = 20, LEAD_BRAKE_A = -0.4, LEAD_BRAKE_NEAR_M = 40, SAME_LEAD_M = 6,
        CUT_IN_KMH = 15, CUT_IN_M = 8, CUT_IN_NEAR_M = 30, CUT_IN_GAP_S = 2,
        BLINKER_S = 6, PULL_NEAR_M = 20, PULL_ARMED_S = 2, PULL_MOVE_KMH = 4, PULL_GAP_M = 3, GO_KMH = 3, MOVED_KMH = 10, DISENGAGE_KMH = 8,
        EV_GAP_S = 3.5;  // readings further apart than this (page hidden, Wi-Fi gone): what was being watched starts over
  function eventAge(ev, now) { return Math.max(0, (now - ev.t) / 1000); }
  function eventStage(ev, now) { const a = eventAge(ev, now); return a < EV_LIVE_S ? "live" : a < EV_RECENT_S ? "recent" : "past"; }
  function eventStep(st, l, now) {
    const s = Object.assign({ hist: [], stopAt: null, stopLead: null, longDone: false, moved: false, steer: null, lead: null, leadAt: 0,
                              blinkAt: -1e9, last: {}, armed: null, waitGo: null, cut: null, readAt: null, n: 0 }, st || {});
    const events = [], updates = [];
    const fresh = { hist: [], stopAt: null, armed: null, waitGo: null, steer: null, lead: null, prev: null, moved: false, cut: null };
    if (!l || !l.driving) {  // car off or no data: nothing is "now", every pending detection starts over
      Object.assign(s, fresh, { readAt: null });
      return { st: s, events, updates };
    }
    const v = l.v || 0, lead = l.lead || null, sec = now / 1000;
    // after a gap the first reading can't tell what happened meanwhile, nor when: start over rather than fire late as "now"
    if (s.readAt != null && (sec - s.readAt > EV_GAP_S || sec < s.readAt)) Object.assign(s, fresh);
    s.readAt = sec;
    const fire = (kind, data, at) => {
      if (s.last[kind] != null && sec - s.last[kind] < (EV_COOLDOWN_S[kind] || 0)) return null;
      s.last[kind] = sec; s.n += 1;
      const ev = { id: `${Math.round(now)}-${s.n}`, kind, t: at || now, v, data: data || {} };
      events.push(ev); return ev;
    };
    if (l.blinker) s.blinkAt = sec;
    // a car cut in: a lead appears close where the last reading had none, or one much closer than you closing in on it explains,
    // with you moving and not changing lanes yourself. It has to be there in the next reading too: then it counts from the
    // first. A lead that blinked out for a reading and came back at the same place is the same car.
    const prev = s.prev && sec - s.prev.t <= CUT_IN_GAP_S ? s.prev : null;
    const closing = (from) => Math.max(0, (v - from.v) / 3.6) * (sec - prev.t);  // m you got closer to it since the last reading
    const sameLead = !!(lead && prev && prev.lead && Math.abs(prev.lead.d - closing(prev.lead) - lead.d) <= SAME_LEAD_M);
    if (s.cut) {
      if (lead && prev && Math.abs(s.cut.d - closing(s.cut) - lead.d) <= SAME_LEAD_M) { fire("cut_in", { d: s.cut.d }, s.cut.t); s.hist = []; }
      s.cut = null;
    } else if (lead && prev && v >= CUT_IN_KMH && lead.d <= CUT_IN_NEAR_M && sec - s.blinkAt > BLINKER_S) {
      const blip = !prev.lead && s.lead && sec - s.leadAt <= 3 && Math.abs(s.lead.d - lead.d) < 5;
      if ((!prev.lead && !blip) || (prev.lead && prev.lead.d - closing(prev.lead) - lead.d >= CUT_IN_M)) s.cut = { t: now, d: lead.d, v: lead.v };
    }
    s.prev = { t: sec, lead: lead ? { d: lead.d, v: lead.v } : null };
    // the car ahead braked: the comma's filtered deceleration of the same car, twice in a row, close, and its speed down 8 km/h
    // in 3 s, with you following above 20 km/h (route 32 above)
    s.hist = s.hist.filter((h) => now - h.t <= LEAD_BRAKE_S * 1000);
    if (lead && v >= LEAD_BRAKE_MIN_KMH) {
      if (!sameLead) s.hist = [];
      s.hist.push({ t: now, v: lead.v, a: lead.a });
      const top = Math.max(...s.hist.map((h) => h.v)), n = s.hist.length;
      const decel = n >= 2 && [s.hist[n - 1].a, s.hist[n - 2].a].every((a) => typeof a === "number" && a <= LEAD_BRAKE_A);
      if (decel && lead.d <= LEAD_BRAKE_NEAR_M && top - lead.v >= LEAD_BRAKE_KMH && fire("lead_brake", { from: top, to: lead.v, d: lead.d })) s.hist = [];
    } else s.hist = [];
    // stops: arriving (behind a car or not), a long one, the car ahead leaving, and you leaving
    if (v < COP_STOP_KMH || (s.stopAt != null && v < GO_KMH)) {  // stopped, and still so at 1–2 km/h of creeping
      if (s.stopAt == null) {
        s.stopAt = sec; s.stopLead = lead ? lead.d : null; s.longDone = false;
        if (s.moved) fire(lead ? "stop_lead" : "stop_free", lead ? { d: lead.d } : {});
        s.moved = false;
      }
      if (!s.longDone && sec - s.stopAt >= COP_LONG_S) { s.longDone = true; fire("stop_long", { s: Math.round(sec - s.stopAt) }); }
      if (lead && lead.d <= PULL_NEAR_M && !s.waitGo) s.armed = s.armed ? { ...s.armed, d: Math.min(s.armed.d, lead.d), miss: 0 } : { at: sec, d: lead.d, miss: 0 };
      else if (s.armed && !lead) s.armed.miss += 1;
      if (s.armed && sec - s.armed.at >= PULL_ARMED_S && (s.armed.miss >= 2 || (lead && (lead.v >= PULL_MOVE_KMH || lead.d >= s.armed.d + PULL_GAP_M)))) {
        const ev = fire("pull_away", { d: s.armed.d });
        s.armed = null;
        if (ev) s.waitGo = { id: ev.id, at: sec };
      }
    } else if (s.stopAt != null) {
      // the car ahead and you left in the same reading: still its leaving, and you went right along
      if (s.armed && !s.waitGo && sec - s.armed.at >= PULL_ARMED_S && (!lead || lead.v >= PULL_MOVE_KMH || lead.d >= s.armed.d + PULL_GAP_M)) {
        fire("pull_away", { d: s.armed.d, go_s: 0 });
      } else if (s.waitGo) updates.push({ id: s.waitGo.id, data: { go_s: Math.round((sec - s.waitGo.at) * 10) / 10 } });  // how long you took
      else if (!lead && s.stopLead == null) fire("go_free", {});
      Object.assign(s, { stopAt: null, armed: null, waitGo: null });
    }
    if (v >= MOVED_KMH) s.moved = true;
    // openpilot let go of the wheel with the car moving. Not `engaged`: with always-on steering it stays on through a brake
    // (route 32: 11 brakes at speed, enabled throughout); the steering itself (latActive) is what dropped
    if (typeof l.steering === "boolean") {
      if (s.steer === true && !l.steering && v >= DISENGAGE_KMH) fire("disengage", { by: l.brake ? "freio" : l.gas ? "acelerador" : l.driver ? "volante" : null });
      s.steer = l.steering;
    }
    if (lead) { s.lead = { d: lead.d, v: lead.v }; s.leadAt = sec; }
    return { st: s, events, updates };
  }
  // A point of the list you just went past (Pontos, /api/radars/near): seen ahead and really close, then behind or gone, with
  // the car moving (stopped, the GPS heading wanders; and a point only seen far off may be on a street you didn't take)
  const PASS_WATCH_M = 300, PASS_NEAR_M = 60, PASS_FORGET_S = 120;
  function pointPassed(st, near, now, moving) {
    const s = Object.assign({ watch: {} }, st || {}), events = [], sec = now / 1000, seen = new Set();
    if (moving === false) return { st: s, events };
    for (const p of near || []) {
      if (!p.key) continue;
      seen.add(p.key);
      const w = s.watch[p.key];
      if (p.ahead && p.distance <= PASS_WATCH_M) s.watch[p.key] = { p, at: sec, min: Math.min(p.distance, w ? w.min : Infinity) };
      else if (!p.ahead && w && w.min > PASS_NEAR_M) delete s.watch[p.key];
      else if (!p.ahead && w) {
        delete s.watch[p.key];
        events.push({ id: `${Math.round(now)}-${p.key}`, kind: p.kind === "lombada" ? "bump" : "radar", t: now, v: null,
                      data: { key: p.key, kind: p.kind, limit: p.limit ?? null, side: p.side || null, confirmed: !!p.confirmed, mine: !!p.mine } });
      }
    }
    for (const [k, w] of Object.entries(s.watch)) {
      if (sec - w.at > PASS_FORGET_S) delete s.watch[k];
      else if (!seen.has(k) && w.min <= PASS_NEAR_M) {  // out of the list right after being close: gone past
        delete s.watch[k];
        events.push({ id: `${Math.round(now)}-${k}`, kind: w.p.kind === "lombada" ? "bump" : "radar", t: now, v: null,
                      data: { key: k, kind: w.p.kind, limit: w.p.limit ?? null, side: w.p.side || null, confirmed: !!w.p.confirmed, mine: !!w.p.mine } });
      }
    }
    return { st: s, events };
  }
  // When a drive started, in seconds: the comma's clock for it (t0: its last read minute's file, minus where that minute
  // ended), else its end minus where its last minute ended, else minus how long it ran (CTO review of app-12: a cut or
  // unread minute moved every event of the drive about a minute)
  function driveStart(tr) {
    const s = (tr && tr.summary) || {};
    return Number.isFinite(s.t0) ? s.t0 : tr.mtime - (s.end_s || s.s || tr.minutes * 60);
  }
  // The copilot's events the comma read from the drives' logs after them (timeline.py "cop", owner 03/11): what the phone
  // missed with its screen off. Each one the phone already has (the same kind within 20 s; the nearest, one for one) is that
  // one, and its answer stays. Two of the log's own close together are two (CTO review of app-12).
  const EV_SAME_MS = 20000;
  function logEvents(trips, list, since) {
    const have = list || [], ids = new Set(have.map((x) => x.id)), cand = [];
    for (const tr of trips || []) {
      const cop = tr && tr.summary && tr.summary.cop;
      if (!Array.isArray(cop) || tr.hidden) continue;
      const t0 = driveStart(tr) * 1000;
      if (!(t0 >= (since || 0))) continue;
      for (const e of cop) {
        const t = t0 + e.s * 1000, id = `${tr.id}-${e.s}-${e.kind}`;
        if (!ids.has(id)) cand.push({ id, kind: e.kind, t: Math.round(t), v: e.v ?? null, data: e.data || {}, src: "log" });
      }
    }
    // the closest pairs first, each event at most once on each side
    const pairs = [];
    cand.forEach((e, i) => have.forEach((x, j) => { if (x.kind === e.kind && Math.abs(x.t - e.t) <= EV_SAME_MS) pairs.push([Math.abs(x.t - e.t), i, j]); }));
    const gone = new Set(), taken = new Set();
    for (const [, i, j] of pairs.sort((a, b) => a[0] - b[0])) if (!gone.has(i) && !taken.has(j)) { gone.add(i); taken.add(j); }
    return cand.filter((_, i) => !gone.has(i)).sort((a, b) => a.t - b.t);
  }
  // What "A conferir" lists (CTO review of app-12): the phone's own events and the log's, never stored together. The phone
  // keeps only EV_KEEP events and EV_KEEP_H hours; merging up to 15 a kind per drive into that list pushed events out on
  // every render, they came back from the log, and an answer went with them. The answers live apart, with the kind and the
  // time, so a log copy of an event answered on the phone (and since dropped there) never comes back.
  const EV_ANSWERS_KEEP = 400;
  function reviewList(list, trips, answers, since) {
    const ans = answers || {}, known = (list || []).concat(Object.values(ans));
    const withAns = (e) => (!e.answer && ans[e.id] ? { ...e, answer: { r: ans[e.id].r, at: ans[e.id].at } } : e);
    const out = (list || []).filter((e) => e.t >= since).map(withAns);
    for (const e of logEvents(trips, known, since)) if (e.t >= since) out.push(e);
    return out.sort((a, b) => a.t - b.t);
  }
  // An answer kept apart: the event's kind and time with it; only the last `since` and EV_ANSWERS_KEEP of them
  function answerKeep(answers, ev, r, at, since) {
    const out = { ...(answers || {}), [ev.id]: { id: ev.id, kind: ev.kind, t: ev.t, r, at } };
    const ids = Object.keys(out).filter((id) => out[id] && out[id].t >= since).sort((a, b) => out[a].t - out[b].t).slice(-EV_ANSWERS_KEEP);
    return Object.fromEntries(ids.map((id) => [id, out[id]]));
  }
  // The drive's event log: new ones in, updates merged, never more than EV_KEEP (the oldest answered ones go first)
  function eventsMerge(list, events, updates) {
    let out = (list || []).slice();
    for (const u of updates || []) out = out.map((e) => (e.id === u.id ? { ...e, data: { ...e.data, ...u.data } } : e));
    for (const e of events || []) if (!out.some((x) => x.id === e.id)) out.push(e);
    while (out.length > EV_KEEP) {
      const i = out.findIndex((e) => e.answer);
      out.splice(i < 0 ? 0 : i, 1);
    }
    return out;
  }
  // The mission an event answers: the first of its missions still open and taken on this drive. Not one left at home: its
  // option may be off, and a mark would count for a test nobody ran (CTO review of app-11)
  function eventMission(ev, cat, active, prog) {
    const keys = EV_MISSIONS[ev && ev.kind] || [], act = active || [];
    const open = keys.filter((k) => ((cat && cat.missions) || []).some((m) => m.key === k && !m.lock) && !(prog && prog[k] && prog[k].full));
    return open.find((k) => act.includes(k)) || null;
  }
  // What the copilot shows: the newest open event in the big card while it is recent; otherwise the one picked for review,
  // or nothing (waiting). An event newer than the pick always takes the big card back. The rest, newest first, is "A conferir".
  // An event picked from the list for review keeps the card until a newer event arrives.
  // Quiet (owner, 04/11): a kind the owner turned off, and a point of the list he already confirmed. Still detected and kept
  // (history, "A conferir" under "silenciados"), never in the big card unless he picks it himself.
  function quietEvent(e, muted) { return (muted || []).includes(e.kind) || (e.kind === "radar" && !!(e.data && e.data.confirmed)); }
  function copilotView(list, now, pick, pickAt, muted) {
    const open = (list || []).filter((e) => !e.answer).sort((a, b) => b.t - a.t);
    let main = open.find((e) => eventAge(e, now) < EV_RECENT_S && !quietEvent(e, muted)) || null;
    const picked = pick ? open.find((e) => e.id === pick) || null : null;
    if (picked && (!main || main.t <= (pickAt || 0))) main = picked;
    const rest = open.filter((e) => e !== main).map((e) => ({ ...e, stage: eventStage(e, now) }));
    return { main, stage: main ? eventStage(main, now) : null, review: !!main && eventAge(main, now) >= EV_RECENT_S,
             pending: rest.filter((e) => !quietEvent(e, muted)), quiet: rest.filter((e) => quietEvent(e, muted)),
             answered: (list || []).filter((e) => e.answer).length };
  }
  // "A conferir" (owner, 04/11: "tem que ser só 3 e o outro um 3 pontinhos que você abre o resto"): the newest three, and
  // the rest behind one tap, newest first, in the same rows
  const PENDING_SHOWN = 3;
  function groupPending(pending, keep) {
    const k = keep ?? PENDING_SHOWN, list = (pending || []).slice().sort((a, b) => b.t - a.t);
    return { shown: list.slice(0, k), rest: list.slice(k) };
  }
  // The kinds the owner turned off, kept on the phone (a preference, not data): only kinds the copilot knows, once each
  function mutedKinds(raw) { return Array.isArray(raw) ? [...new Set(raw.filter((k) => EV_KINDS.includes(k)))] : []; }
  const EV_KINDS = ["lead_brake", "pull_away", "stop_lead", "stop_free", "go_free", "stop_long", "cut_in", "disengage", "bump", "radar"];
  // The age as the card shows it: "há 0:04" up to 10 min, then the clock time
  function eventClock(ev, now) {
    const a = Math.floor(eventAge(ev, now));
    return a < 600 ? { short: true, m: Math.floor(a / 60), s: a % 60 } : { short: false, min: Math.floor(a / 60) };
  }

  // Versions (selfdrive/hexa/versions.py, owner 03/11): every version, its release notes when there are any, and what
  // your drives on it add up to. Newest first: the channel's order, then any version only the drives know, by its last drive.
  const VERSION_CHIPS = 3;
  function versionList(trips, releases) {
    const ts = (trips || []).filter((t) => t.summary && !t.hidden && !idle(t) && versionOf(t));
    const of = (v) => ts.filter((t) => versionOf(t) === v);
    const known = (releases || []).map((r, i) => ({ ...r, i }));
    const extra = versions(ts).filter((v) => !known.some((r) => r.commit === v))
      .map((v) => ({ commit: v, title: null, changes: [], i: null, last: Math.max(...of(v).map((t) => t.mtime)) }));
    const out = known.concat(extra.sort((a, b) => b.last - a.last)).map((r) => {
      const mine = of(r.commit), k = kpiOf(mine);
      return { ...r, trips: mine.length, km: k.km, last: mine.length ? Math.max(...mine.map((t) => t.mtime)) : null };
    });
    out.forEach((r, i) => { r.before = r.i != null ? (known[r.i + 1] || {}).commit || null : null; });
    return out;
  }
  // The chips on Viagens: all, the newest few the drives have, the one picked if it isn't among them, and how many more
  function versionChips(seen, picked, n) {
    const top = (seen || []).slice(0, n || VERSION_CHIPS);
    if (picked && !top.includes(picked)) top.push(picked);
    return { chips: [null, ...top], more: (seen || []).filter((v) => !top.includes(v)).length };
  }

  // openpilot's three following distances (longitudinal_mpc_lib/long_mpc.py:71-79), in seconds and in meters at a speed
  const PERSONALITIES = [[T("perto"), 1.25], [T("padrão"), 1.45], [T("folgada"), 1.75]];
  function gapMeters(gapS, kmh) { return Math.round(gapS * kmh / 3.6); }

  // With the car moving, the phone is for the passenger. Asked once per trip, before Marcar or a point; never parked,
  // never stored on the phone or the comma. Stopping in traffic doesn't ask again; turning the car off does.
  function passenger(gate, car) {
    const ok = !!(gate && gate.ok) && !!car.on;
    return { ok, ask: !!car.on && !!car.moving && !ok };
  }

  // ----- the phone's own copy (APP-01): it opens with what it has, then asks the comma only for what changed -----
  // /api/trips?since=<v>: {same} nothing new · {delta, trips: only the changed, ids: every drive, hidden} · or the whole list.
  // missing: the comma listed a drive this copy never had (a copy cut short): ask for the whole list once.
  function applyTrips(base, ans) {
    const b = base || {};
    const kept = { trips: b.trips || [], marks: b.marks || [], v: b.v || "" };
    if (!ans) return kept;
    if (ans.same) return Object.assign(kept, { v: ans.v || kept.v });
    const marks = Array.isArray(ans.marks) ? ans.marks : kept.marks;
    if (!ans.delta) return { trips: ans.trips || [], marks, v: ans.v || "" };
    const byId = new Map(kept.trips.map((t) => [t.id, t]));
    for (const t of ans.trips || []) byId.set(t.id, t);
    const ids = ans.ids || [], hidden = new Set(ans.hidden || []);
    const missing = ids.filter((id) => !byId.has(id)).length;
    return { trips: ids.filter((id) => byId.has(id)).map((id) => Object.assign({}, byId.get(id), { hidden: hidden.has(id) })), marks, v: ans.v || "", missing };
  }
  // marks, missions: small, so the whole when it changed; {same} keeps the copy
  function applyWhole(base, ans) {
    if (!ans) return base || null;
    if (ans.same) return base ? Object.assign({}, base, { v: ans.v || base.v }) : null;
    return ans;
  }
  function since(v) { return v ? "since=" + encodeURIComponent(v) : ""; }
  // what the copy keeps of a drive's path: nothing unless "Guardar os mapas neste celular" is on (off by default, owner 03/11: D2)
  function keepPath(got, keepMap) { return keepMap && got && Array.isArray(got.points) && got.points.length ? { points: got.points, v: got.v || "" } : null; }
  // "Rever viagem" without the comma (APP-02): viagens.html keeps the drive it read to the end with the comma near.
  //   ride:<id>      the numbers, the marks and the disengagements: never a position
  //   path:<id>#min  the minutes for the map, only with "Guardar os mapas neste celular" on (D2). The "path:" prefix makes
  //                  index.html drop them with the other maps when the option goes off; "Apagar dados" and unpairing clear all.
  const RIDE_KEEP = 30;  // drives kept for "Rever viagem": the one opened longest ago leaves first
  const RIDE_SHOWN = ["mark", "off", "hardbrake"];  // what viagens.html lists and draws (SHOWN there)
  function rideKeys(id) { return { ride: "ride:" + id, map: "path:" + id + "#min" }; }
  function rideKeep(trip, sum, events, points, keepMap, now) {
    const v = trip && Array.isArray(trip.segments) ? trip.segments.join(",") : "";
    const ride = { v, at: now, summary: Object.assign({}, sum),
                   events: (events || []).filter((e) => e && RIDE_SHOWN.includes(e.kind)).map((e) => ({ t: e.t, kind: e.kind, text: e.text })) };
    return { ride, map: keepMap && Array.isArray(points) && points.length > 1 ? { v, points } : null };
  }
  // the drives kept, the newest opened first; past max, the oldest leave (drop: their ride and map are erased)
  function rideOrder(order, id, max) {
    const all = [id].concat((Array.isArray(order) ? order : []).filter((x) => x !== id)), n = max || RIDE_KEEP;
    return { order: all.slice(0, n), drop: all.slice(n) };
  }
  // the maps on the phone, one per drive, whichever page kept it ("path:<id>" from Início, "path:<id>#min" from Rever viagem)
  function mapCount(keys) { return new Set((keys || []).map(String).filter((k) => k.startsWith("path:")).map((k) => k.slice(5).split("#")[0])).size; }
  // Read only: without the comma, or before the first answer of a page opened from the copy. Commands wait for the comma.
  function readOnly(online, fromCopy) { return !online || !!fromCopy; }
  // a 401 says the comma at this address doesn't know this phone: it can be another comma at the same address, so it
  // erases nothing by itself (CTO review, 06/11). Pairing and "Desconectar" have their own answers
  function strangerAnswer(status, path) { return status === 401 && path !== "/api/pair" && path !== "/api/logout"; }
  // what "Apagar dados deste celular" takes from localStorage besides the copy: data, never the language or the view
  const DATA_KEYS = ["lpTrips", "lpMissionSnap", "lpCopEvents", "lpCopAnswers", "lpCopTally", "lpNavRecent"];  // the copilot's events of the drive, and the answers

  // The road's limit card (UI-19): from the tap to the end, what happened to mapd and to the map, and why it stopped.
  // s is /api/limit: job (installing mapd), map (the download, hexaweb.MapJob), saved (the maps downloaded whole, with
  // the date). Lines and buttons hold Portuguese templates; the page translates them and the strings put in them.
  const LIMIT_STOP = {
    car: T("Parou: o carro ligou, {f} de {n} partes. Com o carro desligado, baixe de novo."),
    you: T("Parou: você cancelou, {f} de {n} partes."),
    network: T("Parou: a rede caiu, {f} de {n} partes. Num Wi-Fi, baixe de novo para completar."),
    space: T("Parou: falta espaço no comma, {f} de {n} partes."),
    partial: T("Parou: {f} de {n} partes baixaram. Baixe de novo para completar."),
  };
  const LIMIT_ACTIVE = ["asked", "downloading"];
  function limitBusy(s) { return !!s && (!!(s.job && s.job.busy) || LIMIT_ACTIVE.includes((s.map || {}).state)); }
  function limitCard(s, locale) {
    const out = { lines: [], actions: [] };
    if (!s || !s.option) return out;
    const job = s.job || {}, m = s.map || {}, say = (text, vars) => out.lines.push({ text, vars: vars || {} });
    const name = (k) => ((s.areas || []).find((a) => a.key === k) || {}).name || k;
    if (job.busy) {
      say(T("Instalando o mapd…"));
      return out;
    }
    if (!s.installed) {
      if (job.error) say(T("Não instalou: {motivo}"), { motivo: job.error });
      say(T("Falta instalar o mapd, o programa que lê o mapa (uma vez, num Wi-Fi)."));
      out.actions.push({ kind: "install", label: job.error ? T("Tentar de novo") : T("Instalar o mapd") });
      return out;
    }
    say(T("mapd {v} instalado e conferido."), { v: s.version || "" });
    const counts = { f: m.done || 0, n: m.total || 0 }, area = { regiao: name(m.area) };
    if (m.state === "asked") say(T("Pedindo o mapa de {regiao} ao comma…"), area);
    else if (m.state === "downloading") say(m.total ? T("Baixando {regiao}: {f} de {n} partes.") : T("Baixando {regiao}…"), Object.assign({}, area, counts));
    else if (m.state === "silent") {
      say(m.reason === "start" ? T("O comma não respondeu: o download de {regiao} não começou.")
                               : T("O comma parou de dar notícia do download de {regiao}, em {f} de {n} partes."), Object.assign({}, area, counts));
    } else if (m.state === "stopped") say(LIMIT_STOP[m.reason] || LIMIT_STOP.partial, counts);
    else if (m.state === "refused") say(T("Não começou: {motivo}"), { motivo: m.error });
    const saved = s.saved || [];
    for (const r of saved) {
      say(T("Pronto: {regiao}, baixado em {data}."), { regiao: name(r.area), data: new Date(r.date * 1000).toLocaleDateString(locale || "pt-BR") });
    }
    if (s.fresh && s.tile) say(s.limit ? T("Aqui: {v} km/h.") : T("Mapa carregado. Esta rua não tem limite no mapa."), { v: s.limit });
    else if (!saved.length && (m.state || "idle") === "idle") say(T("Baixe o mapa da sua região. Depois, o limite aparece na tela de dirigir."));
    if (LIMIT_ACTIVE.includes(m.state)) {
      out.actions.push({ kind: "stop", label: T("Parar o download") });
      return out;
    }
    if (["silent", "stopped", "refused"].includes(m.state) && m.area) out.actions.push({ kind: "download", area: m.area, label: T("Tentar de novo") });
    for (const a of s.areas || []) {
      if (a.key !== m.area || !["silent", "stopped", "refused"].includes(m.state)) out.actions.push({ kind: "download", area: a.key, label: T("Baixar: {regiao}"), vars: { regiao: a.name } });
    }
    return out;
  }

  root.LPL = { approvalReport, groupPending, quietEvent, mutedKinds, EV_KINDS, eventStep, pointPassed, eventsMerge, logEvents, driveStart, reviewList, answerKeep, eventMission, copilotView, eventAge, eventStage, eventClock, EV_MISSIONS, EV_REASON, EV_LIVE_S, EV_RECENT_S, EV_KEEP, versionList, versionChips, VERSION_CHIPS, copilotMoment, copilotOrder, copilotNow, copilotGate, stopClock, tripAnswers, canTake, isRoute, routeKeys, missionHistory, missionsChecked, checkedUntil, queueAdd, applyQueue, queuedKeys, QUEUE_ACTIONS, ADVANCED, COP_LONG_S, COP_STOP_KMH, missionAuto, missionProgress, missionLevel, missionOrder, missionTrails, missionDiff, kpiOf, kpiDelta, kpis, tripVsAverage, versionCompare, kpiWeeks, KPI_BETTER, VERSION_MIN_KM, KPI_MIN_KM, WOBBLE_MIN_S, FIXED_HOST, addressPlan, tripSplit, listed, currentTrip, pathSettled, pathSecond, mergeMarks, rideClock, rideWho, applyTrips, applyWhole, since, keepPath, rideKeys, rideKeep, rideOrder, mapCount, RIDE_KEEP, readOnly, strangerAnswer, DATA_KEYS, weekTotal, totals, monthStart, versionOf, versions, idle, fold, tripStart, tripMarks, daysAgo, dayKey,
               tripWords, HISTORY_FILTERS, history, byDay, tripNumbers, weekly, TILE, mercator, mapFit, mapTiles, appMode, shareFor, tallyAdd, canSilence, SILENCE_MIN, NAV_REMEMBER, navTargets, navChoice, navLink, navRecent, samePoint, savedPlace, navOptions, mapCard, pinSpot, centerAt, fitBand, mapCenter, mapXY, mapLatLon, distanceM, shortPlace, maneuverKind, homeFor, navDist, navView, leadView, navZoom, nearestAt, weekShadow, weekCar, PERSONALITIES, gapMeters, duration, STALE_MS, LAT_MS, BUMP_MIN, BUMP_MAX, ACC_MIN, band, commaChecks, laneText, applies, speedChoices, keptSpeed, bumpMessage, passenger, limitCard, limitBusy, LIMIT_STOP };
  if (typeof module === "object" && module.exports) module.exports = root.LPL;
})(typeof window === "object" ? window : globalThis);
