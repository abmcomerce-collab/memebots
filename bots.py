#!/usr/bin/env python3
"""Liga de bots de memecoins (paper trading con precios reales de CoinGecko).

Uso:
  python bots.py                 # una ejecucion (lo que hace GitHub Actions cada hora)
  python bots.py --mock 800      # simula 800 horas con precios inventados (para probar)
"""
import json, os, random, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
STATE, CONFIG = ROOT / "state.json", ROOT / "config.json"
IA_ORDERS, BOARD = ROOT / "ia_orders.json", ROOT / "LEADERBOARD.md"
HOUR, DAY = 3600, 86400
HISTORY_DAYS = 30


def load(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def save(p, obj):
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d/%m/%Y %H:%M UTC")


# ---------------------------------------------------------------- precios
def fetch_prices(ids):
    url = ("https://api.coingecko.com/api/v3/simple/price?vs_currencies=eur&ids="
           + ",".join(ids))
    headers = {"User-Agent": "memebots/1.0"}
    if os.environ.get("COINGECKO_API_KEY"):
        headers["x-cg-demo-api-key"] = os.environ["COINGECKO_API_KEY"]
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        data = json.load(r)
    return {k: v["eur"] for k, v in data.items() if v.get("eur")}


def mock_prices(ids, history, rng):
    out = {}
    for t in ids:
        last = history.get(t, [[0, rng.uniform(0.001, 1)]])[-1][1]
        out[t] = max(last * (1 + rng.gauss(0.0002, 0.03)), 1e-12)
    return out


# ---------------------------------------------------------------- cartera
class Ctx:
    def __init__(self, state, cfg, prices, now):
        self.state, self.cfg, self.prices, self.now = state, cfg, prices, now
        self.fee = cfg["comision_por_operacion"]
        self.tokens = list(cfg["tokens"])

    def sym(self, t):
        return self.cfg["tokens"].get(t, t)

    def price_ago(self, t, hours):
        target = self.now - hours * HOUR
        best = None
        for ts, p in self.state["history"].get(t, []):
            if ts <= target + 600:
                best = p
            else:
                break
        return best

    def change(self, t, hours):
        old = self.price_ago(t, hours)
        return None if not old or t not in self.prices else self.prices[t] / old - 1

    def max_over(self, t, hours):
        lo = self.now - hours * HOUR
        ps = [p for ts, p in self.state["history"].get(t, []) if lo <= ts < self.now - 60]
        return max(ps) if ps else None


def value(bot, prices):
    return bot["cash"] + sum(q * prices.get(t, bot["entry"].get(t, 0)) for t, q in bot["pos"].items())


def holding(bot):
    return next(iter(bot["pos"]), None)


def log(bot, ctx, action, t, eur, nota=""):
    bot["trades"].append({"ts": ctx.now, "accion": action, "token": ctx.sym(t),
                          "eur": round(eur, 4), "precio": ctx.prices[t], "nota": nota})
    bot["trades"] = bot["trades"][-50:]


def buy(bot, ctx, t, frac=1.0, nota=""):
    if t not in ctx.prices:
        return
    eur = bot["cash"] * frac
    if eur < 0.01:
        return
    qty = eur * (1 - ctx.fee) / ctx.prices[t]
    old = bot["pos"].get(t, 0)
    bot["entry"][t] = (old * bot["entry"][t] + qty * ctx.prices[t]) / (old + qty) if old else ctx.prices[t]
    bot["pos"][t] = old + qty
    bot["cash"] -= eur
    bot["meta"]["peak"] = ctx.prices[t]
    log(bot, ctx, "COMPRA", t, eur, nota)


def sell(bot, ctx, t, frac=1.0, nota=""):
    if t not in bot["pos"] or t not in ctx.prices:
        return
    qty = bot["pos"][t] * frac
    eur = qty * ctx.prices[t] * (1 - ctx.fee)
    bot["cash"] += eur
    bot["pos"][t] -= qty
    if frac >= 0.999 or bot["pos"][t] * ctx.prices[t] < 0.01:
        bot["pos"].pop(t)
        bot["entry"].pop(t, None)
    log(bot, ctx, "VENTA", t, eur, nota)


# ---------------------------------------------------------------- estrategias
# Cada operacion guarda un "motivo" en lenguaje natural: que ha visto el bot
# y que regla suya le obliga a actuar.
def pc(x):
    return f"{x:+.1%}".replace(".", ",")


def s_holder(bot, ctx, p):
    if not bot["meta"].get("comprado"):
        t = p["token"]
        buy(bot, ctx, t, nota=f"Mi única regla es comprar {ctx.sym(t)} el primer día y no tocarlo nunca. "
                              f"Soy la referencia: si los demás bots no me ganan, operar no les ha servido de nada.")
        bot["meta"]["comprado"] = True


def s_momentum(bot, ctx, p):
    if ctx.now - bot["meta"].get("ultimo", 0) < p["cada_h"] * HOUR - 300:
        return
    ch = {t: ctx.change(t, p["ventana_h"]) for t in ctx.tokens}
    ch = {t: c for t, c in ch.items() if c is not None}
    if not ch:
        return
    bot["meta"]["ultimo"] = ctx.now
    best = max(ch, key=ch.get)
    cur, v = holding(bot), p["ventana_h"]
    if ch[best] <= 0:
        if cur:
            sell(bot, ctx, cur, nota=f"Ningún token sube en las últimas {v} h (el mejor, {ctx.sym(best)}, va {pc(ch[best])}). "
                                     f"Mi regla es no estar dentro cuando no hay nada con fuerza, así que paso a efectivo.")
        return
    if cur != best:
        if cur:
            sell(bot, ctx, cur, nota=f"{ctx.sym(cur)} ya no es el que más sube ({pc(ch.get(cur, 0))} en {v} h). "
                                     f"Lo cambio por {ctx.sym(best)}, que lidera con {pc(ch[best])}.")
        segundo = sorted(ch.values())[-2] if len(ch) > 1 else None
        extra = f" El segundo va {pc(segundo)}." if segundo is not None else ""
        buy(bot, ctx, best, nota=f"{ctx.sym(best)} es el que más ha subido de {len(ch)} tokens en las últimas {v} h ({pc(ch[best])}).{extra} "
                                 f"Mi regla es subirme al que tiene más fuerza y revisarlo cada {p['cada_h']} h.")


def s_contrarian(bot, ctx, p):
    cur = holding(bot)
    if cur:
        g = ctx.prices[cur] / bot["entry"][cur] - 1
        if g >= p["tp"]:
            sell(bot, ctx, cur, nota=f"{ctx.sym(cur)} ha rebotado {pc(g)} desde mi compra y mi objetivo era {pc(p['tp'])}. "
                                     f"Recojo beneficios; no espero a que vuelva a caer.")
        elif g <= -p["sl"]:
            sell(bot, ctx, cur, nota=f"{ctx.sym(cur)} ha seguido cayendo ({pc(g)} desde mi compra) y mi límite de pérdida es {pc(-p['sl'])}. "
                                     f"Salgo: el rebote que esperaba no ha llegado.")
        return
    ch = {t: ctx.change(t, p["ventana_h"]) for t in ctx.tokens}
    cands = {t: c for t, c in ch.items() if c is not None and c <= -p["caida"]}
    if cands:
        t = min(cands, key=cands.get)
        buy(bot, ctx, t, nota=f"{ctx.sym(t)} ha caído {pc(cands[t])} en {p['ventana_h']} h, más que mi umbral de {pc(-p['caida'])}. "
                              f"Apuesto a que la caída es exagerada y rebotará. Vendo a {pc(p['tp'])} o corto pérdidas a {pc(-p['sl'])}.")


def s_disciplinado(bot, ctx, p):
    t, m = p["token"], bot["meta"]
    s = ctx.sym(t)
    if t in bot["pos"]:
        g = ctx.prices[t] / bot["entry"][t] - 1
        if g >= p["tp"] and not m.get("mitad"):
            sell(bot, ctx, t, 0.5, nota=f"{s} va {pc(g)} desde mi entrada y he llegado a mi objetivo de {pc(p['tp'])}. "
                                        f"Vendo la mitad para asegurar ganancias y dejo correr el resto sin riesgo.")
            m["mitad"] = True
        elif g <= -p["sl"]:
            sell(bot, ctx, t, nota=f"{s} va {pc(g)} desde mi entrada y mi stop está en {pc(-p['sl'])}. "
                                   f"Salgo sin discutir y espero {p['espera_h']} h antes de volver a entrar.")
            m["salida"], m["mitad"] = ctx.now, False
    elif ctx.now - m.get("salida", 0) >= p["espera_h"] * HOUR:
        motivo = ("Primera entrada" if not m.get("salida")
                  else f"Han pasado {p['espera_h']} h desde mi último stop")
        buy(bot, ctx, t, nota=f"{motivo}: compro {s}. Vendo la mitad si llega a {pc(p['tp'])} y todo si cae a {pc(-p['sl'])}.")


def s_breakout(bot, ctx, p):
    cur, m = holding(bot), bot["meta"]
    if cur:
        m["peak"] = max(m.get("peak", 0), ctx.prices[cur])
        if ctx.prices[cur] <= m["peak"] * (1 - p["trailing"]):
            g = ctx.prices[cur] / bot["entry"][cur] - 1
            sell(bot, ctx, cur, nota=f"{ctx.sym(cur)} ha retrocedido un {p['trailing']:.0%} desde su máximo mientras lo tenía. "
                                     f"Mi regla es salir ahí para no devolver lo ganado. Resultado del trade: {pc(g)}.")
        return
    best, ratio = None, 1.0
    for t in ctx.tokens:
        hi = ctx.max_over(t, p["ventana_h"])
        if hi and t in ctx.prices and ctx.prices[t] / hi > ratio:
            best, ratio = t, ctx.prices[t] / hi
    if best:
        buy(bot, ctx, best, nota=f"{ctx.sym(best)} acaba de superar su máximo de las últimas {p['ventana_h']} h ({pc(ratio - 1)} por encima). "
                                 f"Una ruptura así suele atraer más compradores. Vendo si cae un {p['trailing']:.0%} desde el pico.")


def s_ia(bot, ctx, p):
    orders = load(IA_ORDERS, {"orders": []}).get("orders", [])
    done = set(bot["meta"].setdefault("hechas", []))
    for o in orders:
        oid = str(o.get("id"))
        if oid in done:
            continue
        done.add(oid)
        t = o.get("token", "")
        t = next((k for k, v in ctx.cfg["tokens"].items() if v.lower() == t.lower()), t)
        frac = max(0, min(100, float(o.get("pct", 100)))) / 100
        nota = o.get("nota") or "Orden de Claude sin motivo escrito."
        if o.get("action") == "buy":
            buy(bot, ctx, t, frac, nota=nota)
        elif o.get("action") == "sell":
            sell(bot, ctx, t, frac, nota=nota)
    bot["meta"]["hechas"] = sorted(done)


def reglas(bot, ctx):
    p, k = bot["params"], bot["tipo"]
    if k == "holder":
        return [f"Compro {ctx.sym(p['token'])} el primer día con todo el capital.",
                "No vendo nunca, pase lo que pase.",
                "Soy la referencia: el resto de bots tienen que ganarme para justificar que operan."]
    if k == "momentum":
        return [f"Cada {p['cada_h']} h miro qué token ha subido más en las últimas {p['ventana_h']} h.",
                "Compro ese token con todo mi capital; si ya tengo otro, lo vendo para cambiar.",
                "Si ningún token sube, me quedo en efectivo."]
    if k == "contrarian":
        return [f"Busco un token que haya caído más de un {p['caida']:.0%} en {p['ventana_h']} h y compro el que más ha caído.",
                f"Vendo cuando rebota un {p['tp']:.0%} desde mi compra.",
                f"Si sigue cayendo hasta un {p['sl']:.0%}, corto pérdidas."]
    if k == "disciplinado":
        return [f"Solo opero {ctx.sym(p['token'])}.",
                f"Si gana un {p['tp']:.0%}, vendo la mitad y dejo correr el resto.",
                f"Si pierde un {p['sl']:.0%}, vendo todo y espero {p['espera_h']} h antes de volver a entrar."]
    if k == "breakout":
        return [f"Compro el token que supera con más fuerza su máximo de las últimas {p['ventana_h']} h.",
                f"Vendo si retrocede un {p['trailing']:.0%} desde el pico que alcanza mientras lo tengo."]
    return ["Soy Claude. Opero solo cuando Albert me pide actualizar.",
            "Antes de cada orden leo el mercado y las noticias, y explico el motivo.",
            "Si bajo de 5 €, muero como cualquier otro bot."]


STRATS = {"holder": s_holder, "momentum": s_momentum, "contrarian": s_contrarian,
          "disciplinado": s_disciplinado, "breakout": s_breakout, "ia": s_ia}


# ---------------------------------------------------------------- bots
def new_bot(name, kind, params, ctx):
    return {"id": ctx.state["next_id"], "nombre": name, "tipo": kind, "params": params,
            "cash": ctx.cfg["capital_inicial_eur"], "pos": {}, "entry": {}, "meta": {},
            "trades": [], "vivo": True, "nacido": ctx.now, "ronda_entrada": ctx.state["ronda"]}


def add_bot(ctx, name, kind, params):
    ctx.state["bots"].append(new_bot(name, kind, params, ctx))
    ctx.state["next_id"] += 1


def random_bot(ctx, rng):
    kind = rng.choice(["holder", "momentum", "contrarian", "disciplinado", "breakout"])
    n = ctx.state["next_id"]
    tok = rng.choice(ctx.tokens)
    if kind == "holder":
        pr, name = {"token": tok}, f"Holder {ctx.sym(tok)}"
    elif kind == "momentum":
        v = rng.choice([6, 12, 24, 72, 168])
        pr, name = {"ventana_h": v, "cada_h": rng.choice([6, 12, 24])}, f"Momentum {v}h"
    elif kind == "contrarian":
        c = rng.choice([0.10, 0.15, 0.20, 0.30])
        pr = {"caida": c, "ventana_h": rng.choice([24, 72]), "tp": rng.choice([0.1, 0.15, 0.3, 0.5]),
              "sl": rng.choice([0.2, 0.3, 0.5])}
        name = f"Contrarian -{c:.0%}"
    elif kind == "disciplinado":
        pr = {"token": tok, "tp": rng.choice([0.5, 1.0, 2.0]), "sl": rng.choice([0.25, 0.4, 0.6]),
              "espera_h": rng.choice([24, 72, 168])}
        name = f"Disciplinado {ctx.sym(tok)}"
    else:
        v = rng.choice([24, 72, 168])
        pr, name = {"ventana_h": v, "trailing": rng.choice([0.1, 0.2, 0.3])}, f"Breakout {v}h"
    add_bot(ctx, f"{name} #{n}", kind, pr)


def init_state(ctx):
    add_bot(ctx, "El Holder", "holder", {"token": "based-brett"})
    add_bot(ctx, "El Momentum", "momentum", {"ventana_h": 24, "cada_h": 24})
    add_bot(ctx, "El Contrarian", "contrarian", {"caida": 0.20, "ventana_h": 24, "tp": 0.15, "sl": 0.30})
    add_bot(ctx, "El Disciplinado", "disciplinado", {"token": "pepe", "tp": 1.0, "sl": 0.40, "espera_h": 72})
    add_bot(ctx, "La IA (Claude)", "ia", {})


def ret(bot, ctx):
    return value(bot, ctx.prices) / ctx.cfg["capital_inicial_eur"] - 1


def end_round(ctx, rng):
    st, cfg = ctx.state, ctx.cfg
    alive = [b for b in st["bots"] if b["vivo"]]
    dead = [b for b in st["bots"] if not b["vivo"]]
    out = list(dead)
    rest = sorted(alive, key=lambda b: ret(b, ctx))
    while len(out) < cfg["eliminados_por_ronda"] and len(rest) > 1:
        out.append(rest.pop(0))
    rec = {"ronda": st["ronda"], "fin": ctx.now,
           "clasificacion": [{"bot": b["nombre"], "rent": round(ret(b, ctx), 4)}
                             for b in sorted(alive, key=lambda b: -ret(b, ctx))],
           "eliminados": [{"bot": b["nombre"], "motivo": "muerto" if not b["vivo"] else "ultimo",
                           "valor": round(value(b, ctx.prices), 2)} for b in out]}
    st["bots"] = [b for b in st["bots"] if b not in out]
    st["cementerio"].extend({"nombre": b["nombre"], "tipo": b["tipo"], "ronda": st["ronda"],
                             "valor_final": round(value(b, ctx.prices), 2)} for b in out)
    st["ronda"] += 1
    st["ronda_inicio"] = ctx.now
    while len(st["bots"]) < 5:
        random_bot(ctx, rng)
    rec["entran"] = [b["nombre"] for b in st["bots"] if b["ronda_entrada"] == st["ronda"]]
    st["rondas"].append(rec)


def tick(state, cfg, prices, now, rng):
    ctx = Ctx(state, cfg, prices, now)
    if not state["bots"]:
        state["ronda_inicio"] = now
        init_state(ctx)
    for t, p in prices.items():
        h = state["history"].setdefault(t, [])
        h.append([now, p])
        state["history"][t] = [x for x in h if x[0] >= now - HISTORY_DAYS * DAY]
    for bot in state["bots"]:
        bot["reglas"] = reglas(bot, ctx)
        if not bot["vivo"]:
            continue
        try:
            STRATS[bot["tipo"]](bot, ctx, bot["params"])
        except Exception as e:  # un bot roto no para la liga
            bot["meta"]["error"] = str(e)
        c = bot.setdefault("curva", [])
        c.append([now, round(value(bot, prices), 4)])
        bot["curva"] = [x for x in c if x[0] >= now - HISTORY_DAYS * DAY]
        if bot["vivo"] and value(bot, prices) < cfg["umbral_muerte_eur"]:
            for t in list(bot["pos"]):
                sell(bot, ctx, t, nota=f"He bajado de {cfg['umbral_muerte_eur']} €. Vendo todo y quedo fuera de la liga.")
            bot["vivo"], bot["muerte"] = False, now
    if now - state["ronda_inicio"] >= cfg["dias_por_ronda"] * DAY - 600:
        end_round(ctx, rng)
    state["ultima"] = now
    return ctx


# ---------------------------------------------------------------- tablero
def render(ctx):
    st, cfg = ctx.state, ctx.cfg
    bots = sorted(st["bots"], key=lambda b: (not b["vivo"], -ret(b, ctx)))
    fin = st["ronda_inicio"] + cfg["dias_por_ronda"] * DAY
    L = [f"# 🏁 Liga de memebots — Ronda {st['ronda']}", "",
         f"Actualizado: {fmt_ts(st['ultima'])} · Fin de ronda: {fmt_ts(fin)} "
         f"(quedan {max(0, fin - st['ultima']) / DAY:.1f} días)", "",
         "| # | Bot | Estrategia | Valor | Rent. | Posición | Estado |",
         "|---|---|---|---|---|---|---|"]
    for i, b in enumerate(bots, 1):
        cur = holding(b)
        pos = f"{ctx.sym(cur)} ({value(b, ctx.prices) - b['cash']:.2f}€)" if cur else "efectivo"
        est = "🟢 vivo" if b["vivo"] else "💀 muerto"
        L.append(f"| {i} | {b['nombre']} | {b['tipo']} {json.dumps(b['params'], ensure_ascii=False) if b['params'] else ''} "
                 f"| {value(b, ctx.prices):.2f}€ | {ret(b, ctx):+.1%} | {pos} | {est} |")
    L += ["", "## Mercado (para la IA)", "", "| Token | Precio € | 24h | 7d |", "|---|---|---|---|"]
    for t in ctx.tokens:
        if t in ctx.prices:
            c1, c7 = ctx.change(t, 24), ctx.change(t, 168)
            f = lambda c: "—" if c is None else f"{c:+.1%}"
            L.append(f"| {ctx.sym(t)} | {ctx.prices[t]:.8g} | {f(c1)} | {f(c7)} |")
    L += ["", "## Últimas operaciones", ""]
    trades = sorted(((tr, b["nombre"]) for b in st["bots"] for tr in b["trades"]),
                    key=lambda x: -x[0]["ts"])[:15]
    for tr, n in trades:
        L.append(f"- {fmt_ts(tr['ts'])} · **{n}** · {tr['accion']} {tr['token']} "
                 f"{tr['eur']:.2f}€ — {tr['nota']}")
    if st["rondas"]:
        L += ["", "## Historial de rondas", ""]
        for r in reversed(st["rondas"]):
            el = ", ".join(f"{e['bot']} ({e['motivo']}, {e['valor']}€)" for e in r["eliminados"])
            L.append(f"- **Ronda {r['ronda']}** · ganador: {r['clasificacion'][0]['bot'] if r['clasificacion'] else '—'} · "
                     f"eliminados: {el or '—'} · entran: {', '.join(r['entran']) or '—'}")
    BOARD.write_text("\n".join(L) + "\n")


def empty_state():
    return {"bots": [], "history": {}, "ronda": 1, "ronda_inicio": 0, "rondas": [],
            "cementerio": [], "next_id": 1, "ultima": 0}


def main():
    cfg = load(CONFIG, None)
    state = load(STATE, empty_state())
    rng = random.Random()
    if len(sys.argv) > 2 and sys.argv[1] == "--mock":
        mrng = random.Random(42)
        now = state["ultima"] or 1_790_000_000
        for _ in range(int(sys.argv[2])):
            now += HOUR
            ctx = tick(state, cfg, mock_prices(list(cfg["tokens"]), state["history"], mrng), now, rng)
    else:
        prices = fetch_prices(list(cfg["tokens"]))
        if not prices:
            sys.exit("CoinGecko no devolvió precios")
        ctx = tick(state, cfg, prices, int(datetime.now(timezone.utc).timestamp()), rng)
    save(STATE, state)
    render(ctx)


if __name__ == "__main__":
    main()
