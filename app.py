"""NSE Arbitrage Intelligence V1.0 - Streamlit edition (scanner + paper trading only; NO live orders)."""
import json, math, sqlite3, datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd, streamlit as st
from kiteconnect import KiteConnect
from streamlit_autorefresh import st_autorefresh

IST = ZoneInfo("Asia/Kolkata"); CAPITAL = 1000000.0
st.set_page_config("NSE Arbitrage Intelligence V1.0", layout="wide")

# ---------- storage (SQLite; on free Streamlit Cloud it resets on restart, so download history) ----------
@st.cache_resource
def db():
    c = sqlite3.connect("paper.db", check_same_thread=False)
    c.execute("create table if not exists trades(id integer primary key autoincrement,date text,opened text,closed text,symbol text,strategy text,cls text,legs text,capital real,gross real,costs real,exp_net real,pnl real,status text,expiry text,reason text)")
    c.execute("create table if not exists kv(k text primary key,v text)")
    return c
con = db()
now = lambda: dt.datetime.now(IST)
today = lambda: now().date()

# ---------- access gate + Kite login ----------
if "APP_PASSWORD" in st.secrets and st.sidebar.text_input("App password", type="password") != st.secrets["APP_PASSWORD"]:
    st.info("Enter the app password in the sidebar."); st.stop()

kite = KiteConnect(api_key=st.secrets["KITE_API_KEY"])
def stored_token():
    r = con.execute("select v from kv where k='tok'").fetchone()
    if r:
        d, t = json.loads(r[0]); return t if d == str(today()) else None
rt = st.query_params.get("request_token")
if rt and not stored_token():
    try:
        tok = kite.generate_session(rt, api_secret=st.secrets["KITE_API_SECRET"])["access_token"]
        con.execute("insert or replace into kv values('tok',?)", (json.dumps([str(today()), tok]),)); con.commit()
    except Exception as e: st.error(f"Kite login failed: {e}")
    st.query_params.clear()
tok = stored_token(); connected = bool(tok)
if connected: kite.set_access_token(tok)

# ---------- helpers ----------
def market_open():
    n = now(); return n.weekday() < 5 and dt.time(9, 15) <= n.time() <= dt.time(15, 30)

def quotes(keys):
    out = {}
    for i in range(0, len(keys), 200):
        try: out.update(kite.quote(keys[i:i + 200]))
        except Exception as e: st.session_state["err"] = str(e)
    return out

def px(q, side, qty, strict=True):
    """Executable price: buy at best ask, sell at best bid; needs top-of-book size >= qty and fresh data."""
    if not q: return None
    ts = q.get("timestamp")
    if strict and ts and (now().replace(tzinfo=None) - ts).total_seconds() > 30: return None
    lv = ((q.get("depth") or {}).get("sell" if side == "B" else "buy")) or []
    return lv[0]["price"] if lv and lv[0]["price"] > 0 and lv[0]["quantity"] >= qty else None

def charges(legs):
    """Approximate Indian charges per leg (verify against Zerodha's brokerage calculator)."""
    tot = 0
    for l in legs:
        v = l["qty"] * l["px"]; buy = l["side"] == "B"; k = l["kind"]
        brk = 0 if k == "EQ" else min(20, .0003 * v)
        if k == "EQ": stt, ex, st_ = .001 * v, .0000297 * v, (.00015 * v if buy else 0)
        elif k == "FUT": stt, ex, st_ = (0 if buy else .0002 * v), .0000183 * v, (.00002 * v if buy else 0)
        else: stt, ex, st_ = (0 if buy else .001 * v), .0003503 * v, (.00003 * v if buy else 0)
        sebi = 1e-6 * v; tot += brk + stt + ex + st_ + sebi + .18 * (brk + ex + sebi)
    return tot

N = lambda x: .5 * (1 + math.erf(x / 2 ** .5))
def b76(F, K, T, v, r, call):
    df = math.exp(-r * T); d1 = (math.log(F / K) + .5 * v * v * T) / (v * T ** .5); d2 = d1 - v * T ** .5
    return df * (F * N(d1) - K * N(d2)) if call else df * (K * N(-d2) - F * N(-d1))
def iv(p, F, K, T, r, call):
    lo, hi = .01, 3.
    if p <= b76(F, K, T, lo, r, call) or p >= b76(F, K, T, hi, r, call): return None
    for _ in range(50):
        m = (lo + hi) / 2; lo, hi = (m, hi) if b76(F, K, T, m, r, call) < p else (lo, m)
    return (lo + hi) / 2

@st.cache_data(ttl=21600)
def load_inst(day):
    d = pd.DataFrame(kite.instruments("NFO")); return d[d.segment.isin(["NFO-FUT", "NFO-OPT"])]
@st.cache_data(ttl=21600)
def rvol(token, day):
    h = kite.historical_data(token, day - dt.timedelta(days=45), day, "day"); c = [x["close"] for x in h][-21:]
    r = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]; m = sum(r) / len(r)
    return (sum((x - m) ** 2 for x in r) / (len(r) - 1)) ** .5 * math.sqrt(252)

def L(ts, kind, side, qty, price): return dict(ts=ts, kind=kind, side=side, qty=qty, px=price, ex="NSE" if kind == "EQ" else "NFO")

# ---------- five engines ----------
def scan(syms, r, strict):
    inst = load_inst(today()); fu = inst[inst.instrument_type == "FUT"]; op = inst[inst.instrument_type.isin(["CE", "PE"])]
    base, keys = {}, []
    for s in syms:
        f = fu[(fu.name == s) & (fu.expiry >= today() + dt.timedelta(days=3))].sort_values("expiry")
        if len(f) >= 2: base[s] = f.iloc[:2]; keys += [f"NSE:{s}"] + [f"NFO:{t}" for t in f.tradingsymbol[:2]]
    Q = quotes(keys); sel = {}; k2 = []
    for s, f in base.items():
        sp = Q.get(f"NSE:{s}", {}).get("last_price")
        if not sp: continue
        o = op[(op.name == s) & (op.expiry == f.iloc[0].expiry)]; ks = sorted(o.strike.unique())
        if len(ks) < 5: continue
        i = min(range(len(ks)), key=lambda j: abs(ks[j] - sp)); sel[s] = {(x.strike, x.instrument_type): x.tradingsymbol for x in o[o.strike.isin(ks[max(0, i - 2):i + 3])].itertuples()}
        k2 += [f"NFO:{t}" for t in sel[s].values()]
    Q.update(quotes(k2)); out = []
    for s, ops in sel.items():
        f = base[s]; n, fr = f.iloc[0], f.iloc[1]; lot = int(n.lot_size); sq = Q[f"NSE:{s}"]
        T = (n.expiry - today()).days / 365; T2 = (fr.expiry - today()).days / 365; S = sq["last_price"]
        P = lambda t, side, ex="NFO": px(Q.get(f"{ex}:{t}"), side, lot, strict)
        rec = lambda name, cls, rv, legs, gross: out.append(dict(symbol=s, strategy=name, cls=cls, rv=rv, expiry=str(n.expiry), legs=legs, gross=gross * lot if name != "Volatility Arbitrage" else gross, days=(n.expiry - today()).days))
        Fa, Fb = P(n.tradingsymbol, "B"), P(n.tradingsymbol, "S"); Sa = P(s, "B", "NSE")
        # 1 Put-Call Parity (futures form) on each strike
        for K in sorted({k for k, _ in ops}):
            c, p = ops.get((K, "CE")), ops.get((K, "PE"))
            if not (c and p and Fa and Fb): continue
            cb, ca, pb, pa = P(c, "S"), P(c, "B"), P(p, "S"), P(p, "B"); g = 1 + r * T
            if cb and pa: rec("Put-Call Parity", "Potential arbitrage", False, [L(c, "OPT", "S", lot, cb), L(p, "OPT", "B", lot, pa), L(n.tradingsymbol, "FUT", "B", lot, Fa)], (K - Fa) + (cb - pa) * g)
            if ca and pb: rec("Put-Call Parity", "Potential arbitrage", False, [L(c, "OPT", "B", lot, ca), L(p, "OPT", "S", lot, pb), L(n.tradingsymbol, "FUT", "S", lot, Fb)], (Fb - K) + (pb - ca) * g)
        # 2 Cash-Futures (cash-and-carry; dividends NOT modelled - check ex-dividend dates)
        if Sa and Fb: rec("Cash-Futures", "Potential arbitrage", False, [L(s, "EQ", "B", lot, Sa), L(n.tradingsymbol, "FUT", "S", lot, Fb)], Fb - Sa * (1 + r * T))
        # 3 Box spread (adjacent strikes)
        ks = sorted({k for k, _ in ops})
        for K1, K2 in zip(ks, ks[1:]):
            t = [ops.get(x) for x in [(K1, "CE"), (K2, "CE"), (K2, "PE"), (K1, "PE")]]
            if not all(t): continue
            c1a, c2b, p2a, p1b = P(t[0], "B"), P(t[1], "S"), P(t[2], "B"), P(t[3], "S")
            if all((c1a, c2b, p2a, p1b)): rec("Box Spread", "Defined-payoff candidate", False, [L(t[0], "OPT", "B", lot, c1a), L(t[1], "OPT", "S", lot, c2b), L(t[2], "OPT", "B", lot, p2a), L(t[3], "OPT", "S", lot, p1b)], (K2 - K1) - (c1a - c2b + p2a - p1b) * (1 + r * T))
            c1b, c2a, p2b, p1a = P(t[0], "S"), P(t[1], "B"), P(t[2], "S"), P(t[3], "B")
            if all((c1b, c2a, p2b, p1a)): rec("Box Spread", "Defined-payoff candidate", False, [L(t[0], "OPT", "S", lot, c1b), L(t[1], "OPT", "B", lot, c2a), L(t[2], "OPT", "S", lot, p2b), L(t[3], "OPT", "B", lot, p1a)], (c1b - c2a + p2b - p1a) * (1 + r * T) - (K2 - K1))
        # 4 Calendar spread vs theoretical carry (relative value, NOT guaranteed)
        nb, na, fb, fa = Fb, Fa, P(fr.tradingsymbol, "S"), P(fr.tradingsymbol, "B"); carry = S * r * (T2 - T)
        if fb and na: rec("Calendar Spread", "Relative value", True, [L(fr.tradingsymbol, "FUT", "S", lot, fb), L(n.tradingsymbol, "FUT", "B", lot, na)], (fb - na) - carry)
        if fa and nb: rec("Calendar Spread", "Relative value", True, [L(fr.tradingsymbol, "FUT", "B", lot, fa), L(n.tradingsymbol, "FUT", "S", lot, nb)], carry - (fa - nb))
        # 5 Volatility (ATM straddle IV vs 20d realised vol; statistical)
        try:
            K = min(ks, key=lambda x: abs(x - S)); c, p = ops.get((K, "CE")), ops.get((K, "PE")); rv = rvol(sq["instrument_token"], today()); vega = 2 * S * .3989 * T ** .5
            if c and p:
                cb, pb, ca, pa = P(c, "S"), P(p, "S"), P(c, "B"), P(p, "B")
                if cb and pb and (a := iv(cb, S, K, T, r, True)) and (b := iv(pb, S, K, T, r, False)) and (a + b) / 2 - rv >= .05:
                    rec("Volatility Arbitrage", "Statistical opportunity", True, [L(c, "OPT", "S", lot, cb), L(p, "OPT", "S", lot, pb)], ((a + b) / 2 - rv) * vega * lot)
                if ca and pa and (a := iv(ca, S, K, T, r, True)) and (b := iv(pa, S, K, T, r, False)) and rv - (a + b) / 2 >= .05:
                    rec("Volatility Arbitrage", "Statistical opportunity", True, [L(c, "OPT", "B", lot, ca), L(p, "OPT", "B", lot, pa)], (rv - (a + b) / 2) * vega * lot)
        except Exception: pass
    for o in out: o["entry_costs"] = charges(o["legs"]); o["costs"] = 2 * o["entry_costs"]; o["net"] = o["gross"] - o["costs"]
    return sorted(out, key=lambda o: -o["net"])

def margin(legs):
    try:
        orders = [dict(exchange=l["ex"], tradingsymbol=l["ts"], transaction_type="BUY" if l["side"] == "B" else "SELL", variety="regular", product="CNC" if l["kind"] == "EQ" else "NRML", order_type="MARKET", quantity=l["qty"]) for l in legs]
        return float(kite.basket_margins(orders, consider_positions=False)["final"]["total"])
    except Exception: return sum(l["qty"] * l["px"] for l in legs if l["side"] == "S" or l["kind"] == "EQ") * .2  # rough fallback

# ---------- paper-trading engine ----------
def avail():
    p = con.execute("select coalesce(sum(pnl),0) from trades where status='CLOSED'").fetchone()[0]
    c = con.execute("select coalesce(sum(capital),0) from trades where status='OPEN'").fetchone()[0]
    return CAPITAL + p - c

def monitor(strict):
    for id_, sym, strat, legs, cost, net, exp, opened, cap in con.execute("select id,symbol,strategy,legs,costs,exp_net,expiry,opened,capital from trades where status='OPEN'").fetchall():
        legs = json.loads(legs); Q = quotes([f"{l['ex']}:{l['ts']}" for l in legs]); mtm, ex_legs = 0, []
        for l in legs:
            xs = "S" if l["side"] == "B" else "B"; p = px(Q.get(f"{l['ex']}:{l['ts']}"), xs, l["qty"], strict)
            if not p: break
            mtm += (p - l["px"]) * l["qty"] * (1 if l["side"] == "B" else -1); ex_legs.append(dict(l, side=xs, px=p))
        else:
            pnl = mtm - cost / 2 - charges(ex_legs); days = (dt.date.fromisoformat(exp) - today()).days; held = (now() - dt.datetime.fromisoformat(opened)).days
            reason = "Target reached" if pnl >= .8 * net else "Stop loss" if pnl <= -max(2 * abs(net), 300) else "Expiry within 1 day (physical settlement risk)" if days <= 1 else "Max hold 3 days" if held >= 3 else None
            if reason: con.execute("update trades set status='CLOSED',closed=?,pnl=?,costs=?,reason=? where id=?", (now().isoformat(), pnl, cost / 2 + charges(ex_legs), reason, id_)); con.commit()

def open_trade(o, cap):
    con.execute("insert into trades(date,opened,symbol,strategy,cls,legs,capital,gross,costs,exp_net,pnl,status,expiry) values(?,?,?,?,?,?,?,?,?,?,0,'OPEN',?)", (str(today()), now().isoformat(), o["symbol"], o["strategy"], o["cls"], json.dumps(o["legs"]), cap, o["gross"], o["entry_costs"], o["net"], o["expiry"])); con.commit()

# ---------- sidebar ----------
sb = st.sidebar; sb.title("Settings")
if connected: sb.success("Kite connected for today")
else: sb.link_button("Connect Zerodha Kite", kite.login_url(), type="primary"); sb.warning("Not connected. Log in once each trading day.")
syms = [x.strip().upper() for x in sb.text_input("Symbols (F&O stocks)", "RELIANCE,TCS,HDFCBANK,INFY,SBIN,ICICIBANK,ITC,AXISBANK").split(",") if x.strip()]
r = sb.number_input("Risk-free / financing rate (annual)", 0.0, 0.2, 0.07, 0.005)
min_net = sb.number_input("Min net edge (INR)", 0, 100000, 150, 50)
auto = sb.checkbox("Auto paper trading", True); auto_rv = sb.checkbox("Include relative-value (calendar / vol)", False)
override = sb.checkbox("Scan outside market hours (stale depth, testing only)", False)
every = sb.slider("Refresh (seconds)", 5, 60, 10); st_autorefresh(interval=every * 1000, key="rf")

st.title("NSE Arbitrage Intelligence V1.0")
st.caption("Paper trading only. No orders are ever sent to Zerodha. Calendar and volatility signals are relative-value, not guaranteed arbitrage.")
live = market_open() or override
if connected and live:
    if time_ok := (now().timestamp() - st.session_state.get("last", 0) >= every - 1):
        st.session_state["last"] = now().timestamp(); strict = market_open()
        try:
            monitor(strict); res = scan(syms, r, strict)
            for o in [x for x in res if x["net"] >= min_net][:10]:
                o["capital"] = margin(o["legs"]); o["status"] = "Qualified"
                if o["capital"] > avail(): o["status"] = "Rejected: capital"
                elif con.execute("select 1 from trades where status='OPEN' and symbol=? and strategy=? and expiry=?", (o["symbol"], o["strategy"], o["expiry"])).fetchone(): o["status"] = "Already open"
                elif auto and (auto_rv or not o["rv"]): open_trade(o, o["capital"]); o["status"] = "Paper trade opened"
            for o in res: o.setdefault("status", "Net edge below threshold")
            st.session_state["res"] = res; st.session_state["at"] = now().strftime("%H:%M:%S")
        except Exception as e: st.error(f"Scan error: {e}")
elif not connected: st.info("Connect Zerodha Kite in the sidebar to start scanning.")
else: st.info("NSE is closed. The scanner runs 09:15-15:30 IST, Monday-Friday.")
if st.session_state.get("err"): st.caption(f"Last API message: {st.session_state['err']}")

hist = pd.read_sql("select * from trades", con); closed = hist[hist.status == "CLOSED"]; td = closed[closed.date == str(today())]
a, b, c, d = st.columns(4)
a.metric("Paper capital", "₹1,000,000"); b.metric("Available", f"₹{avail():,.0f}")
c.metric("Open positions", int((hist.status == "OPEN").sum())); d.metric("Today's net P&L", f"₹{td.pnl.sum():,.2f}", f"{len(td)} closed")

t1, t2, t3, t4 = st.tabs(["Opportunities", "Open paper trades", "Trade history", "Rules"])
with t1:
    res = st.session_state.get("res", []); st.caption(f"Last scan: {st.session_state.get('at', '-')}")
    if res: st.dataframe(pd.DataFrame([{k: (round(v, 2) if isinstance(v, float) else v) for k, v in o.items() if k not in ("legs", "rv", "entry_costs")} for o in res]), use_container_width=True, hide_index=True)
    else: st.write("No opportunities yet.")
with t2:
    op_ = hist[hist.status == "OPEN"][["opened", "symbol", "strategy", "capital", "exp_net", "expiry"]]
    st.dataframe(op_, use_container_width=True, hide_index=True) if len(op_) else st.write("No open positions.")
with t3:
    cols = ["date", "opened", "closed", "symbol", "strategy", "cls", "capital", "gross", "costs", "pnl", "reason"]
    dates = sorted(closed.date.unique(), reverse=True)
    if dates:
        sel_d = st.selectbox("Date", dates); day = closed[closed.date == sel_d][cols]
        x1, x2 = st.columns(2)
        x1.download_button(f"Download {sel_d} (CSV)", day.to_csv(index=False).encode("utf-8-sig"), f"paper_trades_{sel_d}.csv", "text/csv")
        x2.download_button("Download all dates (CSV)", closed[cols].to_csv(index=False).encode("utf-8-sig"), "paper_trades_all.csv", "text/csv")
        st.dataframe(day, use_container_width=True, hide_index=True)
        st.subheader("Daily summary"); st.dataframe(closed.groupby("date").agg(trades=("id", "count"), net_pnl=("pnl", "sum")).reset_index().sort_values("date", ascending=False), use_container_width=True, hide_index=True)
    else: st.write("No closed trades yet.")
with t4:
    st.markdown("""- **Put-Call Parity, Cash-Futures, Box Spread:** executable bid/ask on every leg, charges, margin from Kite's basket-margin API.
- **Calendar Spread, Volatility:** relative-value / statistical. Not guaranteed; they can lose money.
- Every position is paper-closed at target, stop, 3 days, or 1 day before expiry (stock options settle physically).
- Cash-futures ignores dividends. Verify ex-dividend dates before trusting a signal.
- Charges are estimates. Streamlit Cloud storage is temporary, so download history regularly.""")
