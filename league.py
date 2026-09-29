#!/usr/bin/env python3
"""Liga de 100 bots: 10 subligas x 10 bots, decisiones diarias sobre cierres reales.

Cada subliga tiene 1 benchmark (compra y aguanta), 1 bot aleatorio de control,
1 bot IA (Claude, via ia_orders.json) y 7 estrategias de la literatura.

  python league.py              # ejecucion diaria (GitHub Actions)
  python league.py --mock 120   # simula 120 dias con precios inventados
"""
import hashlib, json, math, os, random, sys, time, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CFG_P, STATE_P, DATA_P = ROOT / "config.json", ROOT / "state.json", ROOT / "data.json"
IA_P, BOARD_P = ROOT / "ia_orders.json", ROOT / "LEADERBOARD.md"


def load(p, d):
    return json.loads(p.read_text()) if p.exists() else d


def save(p, o, indent=None):
    p.write_text(json.dumps(o, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":")))


def pc(x):
    return f"{x:+.1%}".replace(".", ",")


# =============================================================== datos
def http_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "liga-bots/2.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def fetch_coingecko(ids, hist, today):
    """Historial diario en USD. Primera vez: 365 dias; despues solo el precio del dia."""
    key = os.environ.get("COINGECKO_API_KEY")
    h = {"x-cg-demo-api-key": key} if key else {}
    base = "https://api.coingecko.com/api/v3"
    for cid in ids:
        if len(hist.get(cid, [])) < 250:
            try:
                d = http_json(f"{base}/coins/{cid}/market_chart?vs_currency=usd&days=365&interval=daily", h)
                ser = {}
                for ts, p in d.get("prices", []):
                    ser[datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()] = p
                hist[cid] = sorted(ser.items())
            except Exception as e:
                print("coingecko historial", cid, e)
            time.sleep(2.5)
    try:
        px = http_json(f"{base}/simple/price?vs_currencies=usd&ids=" + ",".join(ids), h)
        for cid in ids:
            p = px.get(cid, {}).get("usd")
            if p:
                s = [x for x in hist.get(cid, []) if x[0] != today]
                s.append((today, p))
                hist[cid] = s[-400:]
    except Exception as e:
        print("coingecko precio", e)
    return {c: hist[c] for c in ids if hist.get(c)}


def fetch_yahoo(tickers):
    out = {}
    try:
        import yfinance as yf
        df = yf.download(list(tickers), period="2y", interval="1d", auto_adjust=True,
                         progress=False, group_by="column", threads=True)
        close = df["Close"]
        for t in tickers:
            if t in close:
                s = close[t].dropna()
                if len(s) > 30:
                    out[t] = [(d.date().isoformat(), float(v)) for d, v in s.items()]
    except Exception as e:
        print("yahoo", e)
    for t in tickers:  # reserva: Stooq (acciones/ETF de EE. UU. y divisas)
        if t in out:
            continue
        sym = t.replace("=X", "").lower() if t.endswith("=X") else (t.lower() + ".us" if "." not in t else None)
        if not sym:
            continue
        try:
            req = urllib.request.Request(f"https://stooq.com/q/d/l/?s={sym}&i=d", headers={"User-Agent": "liga-bots/2.0"})
            rows = urllib.request.urlopen(req, timeout=30).read().decode().strip().splitlines()[1:]
            ser = [(r.split(",")[0], float(r.split(",")[4])) for r in rows if r.count(",") >= 4]
            if len(ser) > 30:
                out[t] = ser[-520:]
        except Exception as e:
            print("stooq", t, e)
    return out


def fetch_all(cfg, state, today):
    data = {}
    for sid, sl in cfg["subligas"].items():
        ids = list(sl["activos"])
        if sl["fuente"] == "coingecko":
            data[sid] = fetch_coingecko(ids, state.setdefault("hist_cg", {}), today)
        else:
            data[sid] = fetch_yahoo(ids)
    return data


class Mock:
    def __init__(self, cfg, start, days):
        rng = random.Random(7)
        self.series = {}
        first = start - timedelta(days=520)
        for sid, sl in cfg["subligas"].items():
            vol = {"memecoins": .07, "cripto": .04, "divisas": .005, "bonos": .006}.get(sid, .015)
            drift = rng.uniform(-.0005, .001)
            for t in sl["activos"]:
                p, s, d = rng.uniform(1, 200), [], first
                while d <= start + timedelta(days=days):
                    if sl["fuente"] == "coingecko" or d.weekday() < 5:
                        p *= math.exp(rng.gauss(drift, vol * rng.uniform(.7, 1.3)))
                        s.append((d.isoformat(), p))
                    d += timedelta(days=1)
                self.series[(sid, t)] = s

    def get(self, cfg, today):
        return {sid: {t: [x for x in self.series[(sid, t)] if x[0] <= today] for t in sl["activos"]}
                for sid, sl in cfg["subligas"].items()}


# =============================================================== indicadores
def closes(ser):
    return [p for _, p in ser]


def ret(c, n):
    return c[-1] / c[-1 - n] - 1 if len(c) > n and c[-1 - n] > 0 else None


def sma(c, n):
    return sum(c[-n:]) / n if len(c) >= n else None


def std(c, n):
    if len(c) < n:
        return None
    m = sum(c[-n:]) / n
    return math.sqrt(sum((x - m) ** 2 for x in c[-n:]) / n)


def rsi(c, n=2):
    if len(c) < n + 1:
        return None
    g = l = 0.0
    for a, b in zip(c[-n - 1:-1], c[-n:]):
        g += max(b - a, 0)
        l += max(a - b, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


# =============================================================== estrategias
# Cada estrategia devuelve {activo: motivo} con los activos que quiere tener
# (a partes iguales). None = no toca nada hoy.
FAMILIAS = ["xs_mom", "ts_mom", "sma_filtro", "sma_cruce", "donchian", "rsi2", "bollinger", "dual_mom"]
PARAMS = {
    "xs_mom": [{"L": 126, "k": 3, "R": 21}, {"L": 252, "k": 3, "R": 21}, {"L": 63, "k": 2, "R": 5}, {"L": 126, "k": 2, "R": 5}],
    "ts_mom": [{"L": 252}, {"L": 126}, {"L": 63}],
    "sma_filtro": [{"n": 200}, {"n": 150}, {"n": 100}],
    "sma_cruce": [{"f": 50, "s": 200}, {"f": 20, "s": 100}, {"f": 10, "s": 50}],
    "donchian": [{"e": 55, "x": 20}, {"e": 20, "x": 10}],
    "rsi2": [{"u": 10}, {"u": 5}],
    "bollinger": [{"n": 20, "k": 2.0}, {"n": 20, "k": 2.5}],
    "dual_mom": [{"L": 252}, {"L": 126}],
}


def nombre(fam, p):
    return {"xs_mom": f"Momentum {p.get('L')}d top{p.get('k')}", "ts_mom": f"Tendencia {p.get('L')}d",
            "sma_filtro": f"Media {p.get('n')}d", "sma_cruce": f"Cruce {p.get('f')}/{p.get('s')}",
            "donchian": f"Donchian {p.get('e')}/{p.get('x')}", "rsi2": f"RSI2 <{p.get('u')}",
            "bollinger": f"Bollinger {p.get('k')}σ", "dual_mom": f"Dual {p.get('L')}d",
            "benchmark": "Benchmark", "aleatorio": "Aleatorio", "ia": "IA (Claude)"}[fam]


def reglas(fam, p, sl):
    n = len(sl["activos"])
    R = {
        "benchmark": [f"Compro los {n} activos de la subliga a partes iguales el primer día y no vuelvo a operar.",
                      "Soy el listón: una estrategia que no me gana no aporta nada frente a comprar el mercado."],
        "aleatorio": ["Cada día tengo un 15 % de probabilidad de cambiar de cartera: efectivo o de 1 a 3 activos al azar.",
                      "Soy el grupo de control: si una estrategia no me gana, su resultado se explica por suerte."],
        "ia": ["Soy Claude. Decido la cartera cuando Albert me pide actualizar, leyendo mercado y noticias.",
               "Cada orden fija qué porcentaje de la cartera va a cada activo y explica el motivo.",
               "Juego con las mismas reglas: si pierdo la mitad o quedo por debajo del aleatorio y del benchmark, me eliminan."],
        "xs_mom": [f"Cada {p.get('R')} sesiones ordeno los activos por su rentabilidad de {p.get('L')} sesiones.",
                   f"Compro los {p.get('k')} mejores a partes iguales (momentum transversal, Jegadeesh y Titman, 1993).",
                   "Si ninguno está en positivo, me quedo en efectivo."],
        "ts_mom": [f"Tengo cada activo cuya rentabilidad de {p.get('L')} sesiones es positiva.",
                   "El resto de la cartera queda en efectivo (momentum de serie temporal, Moskowitz, Ooi y Pedersen, 2012)."],
        "sma_filtro": [f"Tengo cada activo que cotiza por encima de su media de {p.get('n')} sesiones.",
                       "Si cae por debajo, lo vendo (filtro de tendencia, Faber, 2007)."],
        "sma_cruce": [f"Tengo cada activo cuya media de {p.get('f')} sesiones está por encima de la de {p.get('s')}.",
                      "Vendo cuando la media rápida cruza por debajo de la lenta."],
        "donchian": [f"Compro un activo cuando cierra por encima de su máximo de {p.get('e')} sesiones.",
                     f"Lo vendo cuando cierra por debajo de su mínimo de {p.get('x')} sesiones (sistema de las Tortugas)."],
        "rsi2": ["Solo compro activos por encima de su media de 200 sesiones (tendencia alcista).",
                 f"Entro cuando el RSI de 2 sesiones baja de {p.get('u')} (sobreventa extrema) y salgo cuando el precio supera su media de 5 sesiones (Connors)."],
        "bollinger": [f"Compro cuando el precio cae por debajo de la banda inferior ({p.get('k')} desviaciones bajo la media de {p.get('n')}).",
                      f"Vendo cuando vuelve a su media de {p.get('n')} sesiones (reversión a la media)."],
        "dual_mom": [f"Cada 21 sesiones elijo el activo con mejor rentabilidad de {p.get('L')} sesiones.",
                     "Solo lo compro si esa rentabilidad es positiva; si no, efectivo (momentum dual, Antonacci)."],
    }
    return R[fam]


def s_benchmark(b, H, sl, ctx):
    if b["meta"].get("hecho"):
        return None
    b["meta"]["hecho"] = True
    return {t: "Compra inicial del benchmark: todo el mercado a partes iguales." for t in H}


def s_aleatorio(b, H, sl, ctx):
    seed = int(hashlib.md5(f"{b['id']}{ctx['hoy']}".encode()).hexdigest(), 16)
    r = random.Random(seed)
    if b["meta"].get("iniciado") and r.random() > 0.15:
        return None
    b["meta"]["iniciado"] = True
    if r.random() < 0.3:
        return {}
    pick = r.sample(sorted(H), min(len(H), r.randint(1, 3)))
    return {t: "Elegido al azar: soy el grupo de control." for t in pick}


def s_xs_mom(b, H, sl, ctx):
    m = b["meta"]
    if ctx["sesion"] - m.get("ult", -999) < b["params"]["R"]:
        return None
    L, k = b["params"]["L"], b["params"]["k"]
    rs = {t: ret(closes(s), L) for t, s in H.items()}
    rs = {t: r for t, r in rs.items() if r is not None}
    if not rs:
        return None
    m["ult"] = ctx["sesion"]
    top = sorted(rs, key=rs.get, reverse=True)[:k]
    orden = sorted(rs, key=rs.get, reverse=True)
    for t in b["pos"]:
        if t not in top and t in rs:
            ctx["salidas"][t] = f"{sl['activos'][t]} baja al puesto {orden.index(t) + 1} de {len(rs)} ({pc(rs[t])} en {L} sesiones): fuera del top {k}."
    return {t: f"{sl['activos'][t]} es el nº {i + 1} de {len(rs)} por rentabilidad de {L} sesiones ({pc(rs[t])})."
            for i, t in enumerate(top) if rs[t] > 0}


def s_ts_mom(b, H, sl, ctx):
    L = b["params"]["L"]
    out = {}
    for t, s in H.items():
        r = ret(closes(s), L)
        if r is not None and r > 0:
            out[t] = f"{sl['activos'][t]} sube {pc(r)} en {L} sesiones: tendencia positiva."
        elif t in b["pos"] and r is not None:
            ctx["salidas"][t] = f"{sl['activos'][t]} ya va {pc(r)} en {L} sesiones: la tendencia ha dejado de ser positiva."
    return out


def s_sma_filtro(b, H, sl, ctx):
    n = b["params"]["n"]
    out = {}
    for t, s in H.items():
        c = closes(s)
        m = sma(c, n)
        if m and c[-1] > m:
            out[t] = f"{sl['activos'][t]} cotiza {pc(c[-1] / m - 1)} sobre su media de {n} sesiones."
        elif m and t in b["pos"]:
            ctx["salidas"][t] = f"{sl['activos'][t]} cae {pc(c[-1] / m - 1)} por debajo de su media de {n} sesiones."
    return out


def s_sma_cruce(b, H, sl, ctx):
    f, sl_ = b["params"]["f"], b["params"]["s"]
    out = {}
    for t, s in H.items():
        c = closes(s)
        a, z = sma(c, f), sma(c, sl_)
        if a and z and a > z:
            out[t] = f"{sl['activos'][t]}: media de {f} sesiones {pc(a / z - 1)} por encima de la de {sl_}."
        elif a and z and t in b["pos"]:
            ctx["salidas"][t] = f"{sl['activos'][t]}: la media de {f} sesiones cruza por debajo de la de {sl_}."
    return out


def s_donchian(b, H, sl, ctx):
    e, x = b["params"]["e"], b["params"]["x"]
    held = set(b["pos"])
    out = {}
    for t, s in H.items():
        c = closes(s)
        if len(c) <= e:
            continue
        if t in held:
            lo = min(c[-x - 1:-1])
            if c[-1] >= lo:
                out[t] = b["motivos"].get(t, "")
            else:
                ctx["salidas"][t] = f"{sl['activos'][t]} pierde su mínimo de {x} sesiones: salgo."
        elif c[-1] > max(c[-e - 1:-1]):
            out[t] = f"{sl['activos'][t]} rompe su máximo de {e} sesiones."
    return out


def s_rsi2(b, H, sl, ctx):
    u = b["params"]["u"]
    held = set(b["pos"])
    out = {}
    for t, s in H.items():
        c = closes(s)
        m200, m5, r = sma(c, 200), sma(c, 5), rsi(c, 2)
        if not (m200 and m5 and r is not None):
            continue
        if t in held:
            if c[-1] <= m5:
                out[t] = b["motivos"].get(t, "")
            else:
                ctx["salidas"][t] = f"{sl['activos'][t]} ya supera su media de 5 sesiones: recojo el rebote."
        elif c[-1] > m200 and r < u:
            out[t] = f"{sl['activos'][t]}: RSI(2) en {r:.1f}, sobreventa dentro de tendencia alcista."
    return out


def s_bollinger(b, H, sl, ctx):
    n, k = b["params"]["n"], b["params"]["k"]
    held = set(b["pos"])
    out = {}
    for t, s in H.items():
        c = closes(s)
        m, sd = sma(c, n), std(c, n)
        if not m or not sd:
            continue
        if t in held:
            if c[-1] < m:
                out[t] = b["motivos"].get(t, "")
            else:
                ctx["salidas"][t] = f"{sl['activos'][t]} ha vuelto a su media de {n} sesiones: objetivo cumplido."
        elif c[-1] < m - k * sd:
            out[t] = f"{sl['activos'][t]} cae {(m - c[-1]) / sd:.1f} desviaciones bajo su media de {n}: apuesto al rebote."
    return out


def s_dual_mom(b, H, sl, ctx):
    m = b["meta"]
    if ctx["sesion"] - m.get("ult", -999) < 21:
        return None
    L = b["params"]["L"]
    rs = {t: ret(closes(s), L) for t, s in H.items()}
    rs = {t: r for t, r in rs.items() if r is not None}
    if not rs:
        return None
    m["ult"] = ctx["sesion"]
    best = max(rs, key=rs.get)
    for t in b["pos"]:
        if t != best:
            ctx["salidas"][t] = f"{sl['activos'][best]} pasa a ser el mejor en {L} sesiones; cambio de activo."
    if rs[best] <= 0:
        for t in b["pos"]:
            ctx["salidas"][t] = f"Ni el mejor activo ({sl['activos'][best]}, {pc(rs[best])}) supera al efectivo: salgo."
        return {}
    return {best: f"{sl['activos'][best]} es el mejor en {L} sesiones ({pc(rs[best])}) y supera al efectivo."}


def s_ia(b, H, sl, ctx):
    orders = load(IA_P, {"orders": []}).get("orders", [])
    done = set(b["meta"].setdefault("hechas", []))
    target = None
    for o in orders:
        if o.get("subliga") != ctx["sid"] or str(o.get("id")) in done:
            continue
        done.add(str(o.get("id")))
        rev = {v.lower(): k for k, v in sl["activos"].items()}
        pesos = {}
        for t, w in (o.get("pesos") or {}).items():
            t = t if t in sl["activos"] else rev.get(str(t).lower(), t)
            if t in H and float(w) > 0:
                pesos[t] = float(w) / 100
        tot = sum(pesos.values())
        if tot > 1:
            pesos = {t: w / tot for t, w in pesos.items()}
        target = {"pesos": pesos, "nota": o.get("nota") or "Orden de Claude."}
    b["meta"]["hechas"] = sorted(done)
    return target


STRATS = {"benchmark": s_benchmark, "aleatorio": s_aleatorio, "ia": s_ia, "xs_mom": s_xs_mom, "ts_mom": s_ts_mom,
          "sma_filtro": s_sma_filtro, "sma_cruce": s_sma_cruce, "donchian": s_donchian, "rsi2": s_rsi2,
          "bollinger": s_bollinger, "dual_mom": s_dual_mom}


# =============================================================== cartera
def value(b, px):
    return b["cash"] + sum(q * px.get(t, b["ult_px"].get(t, 0)) for t, q in b["pos"].items())


def log(b, ctx, acc, t, eur, nota):
    b["trades"].append({"f": ctx["hoy"], "a": acc, "t": t, "e": round(eur, 4), "n": nota})
    b["trades"] = b["trades"][-40:]


def rebalance(b, weights, notes, px, cost, ctx, sl):
    """Lleva la cartera a los pesos objetivo, con coste sobre lo negociado."""
    V = value(b, px)
    for t in list(b["pos"]):
        if t not in px:
            continue
        cur = b["pos"][t] * px[t]
        tgt = V * weights.get(t, 0)
        if cur - tgt > 0.01:
            sell = cur - tgt
            b["pos"][t] -= sell / px[t]
            b["cash"] += sell * (1 - cost)
            if b["pos"][t] * px[t] < 0.01:
                b["pos"].pop(t)
                b["motivos"].pop(t, None)
            if t in weights and weights[t] > 0:
                why = "Reajusto el peso para mantener la cartera a partes iguales con la nueva entrada."
            else:
                why = notes.get("_salida_" + t) or f"{sl['activos'][t]} ya no cumple mis reglas."
            log(b, ctx, "VENTA", sl["activos"][t], sell, why)
    for t, w in weights.items():
        if t not in px:
            continue
        cur = b["pos"].get(t, 0) * px[t]
        buy = min(V * w - cur, b["cash"])
        if buy > 0.01:
            b["pos"][t] = b["pos"].get(t, 0) + buy * (1 - cost) / px[t]
            b["cash"] -= buy
            b["motivos"][t] = notes.get(t, "")
            log(b, ctx, "COMPRA", sl["activos"][t], buy, notes.get(t, ""))


def step_bot(b, H, px, sl, ctx):
    ctx["salidas"] = {}
    res = STRATS[b["fam"]](b, H, sl, ctx)
    if res is None:
        return
    if b["fam"] == "ia":
        rebalance(b, res["pesos"], {t: res["nota"] for t in res["pesos"]} | {"_salida_" + t: res["nota"] for t in b["pos"]},
                  px, sl["coste"], ctx, sl)
        return
    if set(res) == set(b["pos"]):
        return  # misma cartera: no se rebalancea para no pagar comisiones por nada
    w = {t: 1 / len(res) for t in res} if res else {}
    notes = dict(res)
    notes.update({"_salida_" + t: r for t, r in ctx["salidas"].items()})
    if b["fam"] == "aleatorio":
        notes.update({"_salida_" + t: "Cambio de cartera al azar." for t in b["pos"]})
    rebalance(b, w, notes, px, sl["coste"], ctx, sl)


# =============================================================== bots y rondas
def new_bot(state, sid, fam, params, hoy, cap):
    state["next_id"] += 1
    return {"id": state["next_id"], "sid": sid, "fam": fam, "params": params, "nombre": nombre(fam, params),
            "cash": cap, "pos": {}, "ult_px": {}, "motivos": {}, "meta": {}, "trades": [], "curva": [],
            "vivo": True, "nacido": hoy}


def init_state(cfg, hoy):
    st = {"next_id": 0, "bots": [], "ronda": 1, "ronda_inicio": hoy, "rondas": [], "cementerio": [],
          "sesiones": {}, "ultima": None, "inicio": hoy}
    for i, (sid, sl) in enumerate(cfg["subligas"].items()):
        cap = cfg["capital_inicial"]
        st["bots"] += [new_bot(st, sid, "benchmark", {}, hoy, cap), new_bot(st, sid, "aleatorio", {}, hoy, cap),
                       new_bot(st, sid, "ia", {}, hoy, cap)]
        fams = [f for j, f in enumerate(FAMILIAS) if j != i % len(FAMILIAS)]  # 7 de 8, rotando la que falta
        for f in fams:
            st["bots"].append(new_bot(st, sid, f, PARAMS[f][0], hoy, cap))
    for b in st["bots"]:
        b["reglas"] = reglas(b["fam"], b["params"], cfg["subligas"][b["sid"]])
    return st


def curve_val(b, d):
    v = None
    for f, x in b["curva"]:
        if f <= d:
            v = x
        else:
            break
    return v


def metrics(b, bench, anual):
    c = b["curva"]
    if len(c) < 2:
        return {"ret": 0, "exceso": 0, "sharpe": None, "dd": 0, "dias": len(c)}
    r = c[-1][1] / c[0][1] - 1
    b0, b1 = curve_val(bench, c[0][0]), (bench["curva"][-1][1] if bench["curva"] else None)
    exc = r - (b1 / b0 - 1) if b0 and b1 else 0
    rets = [c[i][1] / c[i - 1][1] - 1 for i in range(1, len(c)) if c[i - 1][1] > 0]
    sh = None
    if len(rets) >= 10:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1))
        sh = mu / sd * math.sqrt(anual) if sd > 0 else None
    peak, dd = 0, 0
    for _, v in c:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"ret": r, "exceso": exc, "sharpe": sh, "dd": dd, "dias": len(c)}


def end_round(cfg, st, hoy, rng):
    rec = {"ronda": st["ronda"], "fin": hoy, "subligas": {}}
    for sid, sl in cfg["subligas"].items():
        bots = [b for b in st["bots"] if b["sid"] == sid]
        bench = next(b for b in bots if b["fam"] == "benchmark")
        rand = next(b for b in bots if b["fam"] == "aleatorio")
        out = [b for b in bots if not b["vivo"]]
        cands = []
        for b in bots:
            if not b["vivo"] or b["fam"] in ("benchmark", "aleatorio") or len(b["curva"]) < cfg["dias_minimos_para_eliminar"]:
                continue
            m = metrics(b, bench, sl["anual"])
            r_rand = curve_val(rand, hoy) / (curve_val(rand, b["curva"][0][0]) or 1) - 1 if rand["curva"] else 0
            if m["exceso"] < 0 and m["ret"] < r_rand:
                cands.append((m["ret"] - r_rand, b))
        cands.sort(key=lambda x: x[0])
        for _, b in cands:
            if len(out) >= cfg["max_eliminados_por_subliga"]:
                break
            out.append(b)
        elim = []
        for b in out:
            V = b["curva"][-1][1] if b["curva"] else cfg["capital_inicial"]
            motivo = "muerto (perdió la mitad)" if not b["vivo"] else "por debajo del aleatorio y del benchmark"
            elim.append({"bot": b["nombre"], "fam": b["fam"], "valor": round(V, 2), "motivo": motivo})
            st["cementerio"].append({"nombre": b["nombre"], "sid": sid, "fam": b["fam"], "ronda": st["ronda"],
                                     "valor": round(V, 2), "motivo": motivo})
            st["bots"].remove(b)
            present = {(x["fam"], json.dumps(x["params"], sort_keys=True)) for x in st["bots"] if x["sid"] == sid}
            opts = [(f, p) for f in FAMILIAS for p in PARAMS[f] if (f, json.dumps(p, sort_keys=True)) not in present]
            f, p = rng.choice(opts)
            nb = new_bot(st, sid, f, p, hoy, cfg["capital_inicial"])
            nb["reglas"] = reglas(f, p, sl)
            st["bots"].append(nb)
            elim[-1]["entra"] = nb["nombre"]
        ranking = sorted((b for b in bots if b in st["bots"] or b in out),
                         key=lambda b: -(metrics(b, bench, sl["anual"])["exceso"]))
        rec["subligas"][sid] = {"ganador": ranking[0]["nombre"] if ranking else None, "eliminados": elim}
    st["rondas"].append(rec)
    st["ronda"] += 1
    st["ronda_inicio"] = hoy


def run(cfg, st, data, hoy, rng):
    for sid, sl in cfg["subligas"].items():
        H = {t: s for t, s in data.get(sid, {}).items() if s}
        if not H:
            print("sin datos para", sid)
            continue
        last = max(s[-1][0] for s in H.values())
        nueva = st["sesiones"].get(sid, {}).get("fecha") != last
        ses = st["sesiones"].setdefault(sid, {"n": 0, "fecha": None})
        if nueva:
            ses["n"] += 1
            ses["fecha"] = last
        px = {t: s[-1][1] for t, s in H.items()}
        ctx = {"hoy": last, "sid": sid, "sesion": ses["n"]}
        for b in [x for x in st["bots"] if x["sid"] == sid]:
            if b["vivo"] and nueva:
                try:
                    step_bot(b, H, px, sl, ctx)
                except Exception as e:
                    b["meta"]["error"] = str(e)
            b["ult_px"].update({t: px[t] for t in b["pos"] if t in px})
            v = value(b, px)
            if b["curva"] and b["curva"][-1][0] == last:
                b["curva"][-1][1] = round(v, 5)
            elif nueva or not b["curva"]:
                b["curva"].append([last, round(v, 5)])
            b["curva"] = b["curva"][-400:]
            if b["vivo"] and b["fam"] not in ("benchmark", "aleatorio") and v < cfg["capital_inicial"] * cfg["umbral_muerte"]:
                rebalance(b, {}, {"_salida_" + t: "He perdido la mitad del capital: muero y vendo todo." for t in b["pos"]},
                          px, sl["coste"], ctx, sl)
                b["vivo"] = False
                b["muerte"] = last
        st.setdefault("mercado", {})[sid] = {
            t: {"p": px[t], "d1": ret(closes(s), 1), "d5": ret(closes(s), 5), "d20": ret(closes(s), 20),
                "sma200": (px[t] / sma(closes(s), 200) - 1) if sma(closes(s), 200) else None}
            for t, s in H.items()}
    if (date.fromisoformat(hoy) - date.fromisoformat(st["ronda_inicio"])).days >= cfg["dias_por_ronda"]:
        end_round(cfg, st, hoy, rng)
    st["ultima"] = hoy


# =============================================================== salida
def export(cfg, st):
    bots = []
    for b in st["bots"]:
        sl = cfg["subligas"][b["sid"]]
        bench = next(x for x in st["bots"] if x["sid"] == b["sid"] and x["fam"] == "benchmark")
        m = metrics(b, bench, sl["anual"])
        v = b["curva"][-1][1] if b["curva"] else cfg["capital_inicial"]
        pos = {sl["activos"].get(t, t): round(q * b["ult_px"].get(t, 0), 3) for t, q in b["pos"].items()}
        bots.append({"id": b["id"], "sid": b["sid"], "nombre": b["nombre"], "fam": b["fam"], "vivo": b["vivo"],
                     "valor": round(v, 4), "cash": round(b["cash"], 4), "pos": pos, "m": m, "reglas": b.get("reglas", []),
                     "trades": b["trades"][-15:], "curva": [x[1] for x in b["curva"][-120:]], "nacido": b["nacido"]})
    subs = {}
    for sid, sl in cfg["subligas"].items():
        bs = [x for x in bots if x["sid"] == sid]
        bench = next((x for x in bs if x["fam"] == "benchmark"), None)
        rand = next((x for x in bs if x["fam"] == "aleatorio"), None)
        strat = [x for x in bs if x["fam"] not in ("benchmark", "aleatorio")]
        subs[sid] = {"nombre": sl["nombre"], "bench_ret": bench["m"]["ret"] if bench else 0,
                     "rand_ret": rand["m"]["ret"] if rand else 0,
                     "exceso_medio": sum(x["m"]["exceso"] for x in strat) / len(strat) if strat else 0,
                     "baten_bench": sum(1 for x in strat if x["m"]["exceso"] > 0),
                     "baten_rand": sum(1 for x in strat if rand and x["m"]["ret"] > rand["m"]["ret"]),
                     "n_estrategias": len(strat), "fecha": st["sesiones"].get(sid, {}).get("fecha"),
                     "mercado": {sl["activos"][t]: v for t, v in st.get("mercado", {}).get(sid, {}).items()}}
    save(DATA_P, {"actualizado": st["ultima"], "inicio": st["inicio"], "ronda": st["ronda"], "ronda_inicio": st["ronda_inicio"],
                  "dias_por_ronda": cfg["dias_por_ronda"], "capital": cfg["capital_inicial"], "subligas": subs,
                  "bots": bots, "rondas": st["rondas"][-12:], "cementerio": st["cementerio"][-60:]})
    board(cfg, st, bots, subs)


def board(cfg, st, bots, subs):
    f = lambda x: "—" if x is None else pc(x)
    L = [f"# Liga de 100 bots — Ronda {st['ronda']}", "", f"Actualizado: {st['ultima']} · Inicio: {st['inicio']}", "",
         "## Subligas (rentabilidad desde el inicio)", "",
         "| Subliga | Benchmark | Aleatorio | Exceso medio estrategias | Baten al benchmark | Baten al aleatorio |",
         "|---|---|---|---|---|---|"]
    for sid, s in sorted(subs.items(), key=lambda kv: -kv[1]["exceso_medio"]):
        L.append(f"| {s['nombre']} | {f(s['bench_ret'])} | {f(s['rand_ret'])} | {f(s['exceso_medio'])} | "
                 f"{s['baten_bench']}/{s['n_estrategias']} | {s['baten_rand']}/{s['n_estrategias']} |")
    L += ["", "## Ranking general (exceso sobre el benchmark de su subliga)", "",
          "| # | Bot | Subliga | Rent. | Exceso | Sharpe | Caída máx. | Estado |", "|---|---|---|---|---|---|---|---|"]
    for i, b in enumerate(sorted(bots, key=lambda x: -x["m"]["exceso"]), 1):
        sh = "—" if b["m"]["sharpe"] is None else f"{b['m']['sharpe']:.2f}"
        L.append(f"| {i} | {b['nombre']} | {subs[b['sid']]['nombre']} | {f(b['m']['ret'])} | {f(b['m']['exceso'])} | {sh} | "
                 f"{f(b['m']['dd'])} | {'vivo' if b['vivo'] else 'muerto'} |")
    L += ["", "## Mercado por subliga (para la IA)", ""]
    for sid, s in subs.items():
        ia = next((b for b in bots if b["sid"] == sid and b["fam"] == "ia"), None)
        cart = ", ".join(f"{k} {v:.2f}€" for k, v in ia["pos"].items()) if ia and ia["pos"] else "efectivo"
        L += [f"### {s['nombre']} (id: {sid}) · IA: {cart}", "", "| Activo | 1d | 5d | 20d | vs media 200 |", "|---|---|---|---|---|"]
        for a, m in s["mercado"].items():
            L.append(f"| {a} | {f(m['d1'])} | {f(m['d5'])} | {f(m['d20'])} | {f(m['sma200'])} |")
        L.append("")
    BOARD_P.write_text(chr(10).join(L) + chr(10))


def main():
    cfg = load(CFG_P, None)
    rng = random.Random()
    if len(sys.argv) > 2 and sys.argv[1] == "--mock":
        days = int(sys.argv[2])
        start = date(2026, 1, 5)
        mock = Mock(cfg, start, days)
        st = None
        for i in range(days):
            hoy = (start + timedelta(days=i)).isoformat()
            if st is None:
                st = init_state(cfg, hoy)
            run(cfg, st, mock.get(cfg, hoy), hoy, rng)
    else:
        hoy = datetime.now(timezone.utc).date().isoformat()
        st = load(STATE_P, None)
        if not st or "sesiones" not in st:  # estado de la liga antigua o inexistente: empezamos de cero
            st = init_state(cfg, hoy)
        run(cfg, st, fetch_all(cfg, st, hoy), hoy, rng)
    save(STATE_P, st)
    export(cfg, st)


if __name__ == "__main__":
    main()
