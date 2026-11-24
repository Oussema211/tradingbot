"""
THE 5 PILLARS, graded deeply.

    1. Price $2 - $20         2. Low float          3. High relative volume
    4. High volume            5. Good catalyst

A yes/no check per pillar throws away most of what matters: a 3M float that has already rotated 4 times is not the
same stock as a 19M float nobody is trading; an FDA approval from 20 minutes ago is not the same as a vague press
release from last night. So each pillar is graded 0-100 from several indicators, says WHY in plain English, and
cites where the rule comes from. `evaluate()` combines them into one Pillar Score (0-100) and a grade.

Each pillar returns:
    key, name, ok (True / False / None = unknown or borderline), score 0-100, value (short headline),
    tier (one word), indicators [[label, value, tone]], why (one sentence), ref (where the rule comes from)
tone: "good" | "warn" | "bad" | "info"

REFERENCES (what the thresholds are based on)
  [RC]  Ross Cameron, Warrior Trading: the 5 criteria for momentum day trades (price $2-20, up 10%+, relative volume
        5x+, news, float under 20M, ideally under 10M). "How to Day Trade: The Plain Truth" (2020) and his free
        Warrior Trading lessons on stock selection.
  [AZ]  Andrew Aziz, "How to Day Trade for a Living" (2016): "Stocks in Play", relative volume, float and catalysts.
  [CH]  Wesley Chan, "Stock price reaction to news and no-news: drift and reversal after headlines",
        Journal of Financial Economics 70 (2003): big moves WITH news keep drifting, big moves WITHOUT news reverse.
  [GKM] Gervais, Kaniel & Mingelgrin, "The High-Volume Return Premium", Journal of Finance 56 (2001):
        unusually high volume draws attention and is followed by higher returns.
  [LS]  Lee & Swaminathan, "Price Momentum and Trading Volume", Journal of Finance 55 (2000): volume tells you how
        much attention / fuel a move has.
  [TS]  Tetlock, "Giving Content to Investor Sentiment", Journal of Finance 62 (2007) and Tetlock, Saar-Tsechansky &
        Macskassy, "More Than Words", Journal of Finance 63 (2008): the words in a headline predict the reaction;
        negative words (offering, lawsuit, miss) matter most.
Education only, not financial advice.
"""
import re
from datetime import datetime, time as dtime

WEIGHTS = {"catalyst": 25, "rvol": 25, "float": 20, "volume": 15, "price": 15}   # sums to 100


def _clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def _step(v, table, default=0):
    """table = [(threshold, points), ...] high to low: points of the first threshold v reaches."""
    for th, pts in table:
        if v >= th:
            return pts
    return default


def _big(n):
    if n is None:
        return "-"
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= d:
            return ("%.2f%s" if abs(n) < 10 * d else "%.1f%s") % (n / d, s)
    return "%d" % n


# ------------------------------------------------------------------ 1. PRICE
def price_pillar(p, daily, bars, prev_close):
    ind = []
    if 4 <= p <= 12:
        base, tier = 100, "sweet spot"
    elif 2 <= p <= 20:
        base, tier = 80, "in range"
    elif 1 <= p < 2 or 20 < p <= 30:
        base, tier = 25, "edge"
    else:
        base, tier = 0, "out"
    ind.append(["Price", "$%.2f" % p, "good" if base >= 80 else "warn" if base else "bad"])
    # daily range potential: average true range over the last 14 days, as % of price
    atrp = None
    if daily and len(daily) >= 5:
        trs, pc = [], None
        for b in daily[-15:]:
            tr = b["h"] - b["l"] if pc is None else max(b["h"], pc) - min(b["l"], pc)
            trs.append(tr); pc = b["c"]
        trs = trs[-14:]
        atrp = sum(trs) / len(trs) / p * 100 if p else None
    rng_pts = 50
    if atrp is not None:
        rng_pts = _step(atrp, [(8, 100), (5, 75), (3, 50)], 25)
        ind.append(["Avg daily range (ATR14)", "%.1f%%" % atrp, "good" if atrp >= 5 else "warn" if atrp >= 3 else "bad"])
    if bars:
        hi, lo = max(b["h"] for b in bars), min(b["l"] for b in bars)
        if lo > 0:
            ind.append(["Today's range", "%.1f%%" % ((hi / lo - 1) * 100), "info"])
        o = bars[0]["o"]
        if prev_close and o:
            gap = (o / prev_close - 1) * 100
            ind.append(["Gap at first candle", "%+.1f%%" % gap, "good" if gap >= 4 else "info"])
    # tick economics: under $2 one cent is a big % (wide spreads, sloppy fills); over $20 it moves slower in %
    if p < 2:
        ind.append(["Tick size", "1c = %.1f%% of price" % (1 / p), "bad"])
    score = round(0.7 * base + 0.3 * rng_pts) if base else 0
    why = ("Cheap enough to move fast in %% terms, liquid enough to fill cleanly." if base >= 80 else
           "Under $2 spreads and fake moves eat you; over $20 small caps move slower in %%." if base else
           "Outside the $2-20 zone where momentum small caps work best.")
    return dict(key="price", name="Price $2-20", ok=2 <= p <= 20, score=score, value="$%.2f" % p, tier=tier,
                indicators=ind, why=why.replace("%%", "%"), ref="[RC] price $2-20; ATR = daily range potential")


# ------------------------------------------------------------------ 2. LOW FLOAT
def float_pillar(fund, fullvol):
    fl = fund.get("float")
    ind = []
    if not fl:
        loaded = fund.get("loaded")
        return dict(key="float", name="Low float", ok=None, score=50, value="?", tier="loading" if not loaded else "unknown",
                    indicators=[["Float", "loading..." if not loaded else "unknown", "warn"]],
                    why="Float not known yet: counted as half. Check it in your broker before trading.",
                    ref="[RC] float under 20M, ideally under 10M")
    m = fl / 1e6
    base = _step(-m, [(-5, 100), (-10, 92), (-20, 72), (-50, 30)], 0)
    tier = "micro" if m < 5 else "low" if m < 10 else "ok" if m < 20 else "mid" if m < 50 else "large"
    ind.append(["Float", "%sM shares (%s)" % (("%.2f" if m < 10 else "%.1f") % m, fund.get("float_src") or "?"),
                "good" if m < 10 else "warn" if m < 20 else "bad"])
    bonus = 0
    rot = fullvol / fl if fullvol else None
    if rot is not None:
        bonus += _step(rot, [(3, 15), (1, 10), (0.5, 5)], 0)
        ind.append(["Float rotation today", "%.1fx" % rot, "good" if rot >= 1 else "info"])
    sp = fund.get("short_pct")
    if sp:
        if sp >= 15:
            bonus += 5
        ind.append(["Short interest", "%.0f%% of float" % sp, "good" if sp >= 15 else "info"])
    ins = fund.get("insider_pct")
    inst = fund.get("inst_pct")
    if ins is not None:
        ind.append(["Insiders hold", "%.0f%%" % ins, "good" if ins >= 30 else "info"])
    if inst is not None:
        ind.append(["Institutions hold", "%.0f%%" % inst, "info" if inst >= 10 else "warn"])
    sh = fund.get("shares")
    if sh and sh > fl * 3:
        ind.append(["Shares outstanding", "%s (float is only %.0f%%)" % (_big(sh), fl / sh * 100), "info"])
    score = _clamp(base + bonus) if base else min(25, bonus)
    if m < 5:
        why = "Tiny supply: a little buying moves it a lot (both ways)."
    elif m < 20:
        why = "Small supply: demand from news and volume can push it hard."
    else:
        why = "Big float: needs much more buying to move. Everything else must line up."
    if rot is not None and rot >= 1:
        why += " The whole float already changed hands %.1fx today: extreme demand." % rot
    return dict(key="float", name="Low float", ok=m < 20, score=score, value="%.1fM" % m if m >= 1 else "%.2fM" % m,
                tier=tier, indicators=ind, why=why, ref="[RC] float under 20M (under 10M best); rotation = volume / float")


# ------------------------------------------------------------------ 3. HIGH RELATIVE VOLUME
def _bar_minute(b, parse_t, NY):
    t = parse_t(b["t"]).astimezone(NY)
    return t.hour * 60 + t.minute


def rvol_pillar(rvol, rsrc, bars, hist, avg_vol, now, parse_t, NY):
    ind = [["Relative volume (day)", ("%.1fx" % rvol if rvol <= 100 else "100x+"), "good" if rvol >= 5 else "warn" if rvol >= 3 else "bad"],
           ["Compared with", rsrc, "info"]]
    base = _step(rvol, [(10, 100), (5, 85), (3, 60), (2, 35)], 10)
    m = now.hour * 60 + now.minute
    # RIGHT NOW: last 15 minutes vs the same 15 minutes on normal days. Day RV can be huge from a 7am spike that is
    # long gone; this tells you whether the crowd is still here.
    rv15 = None
    if bars:
        recent = sum(b["v"] for b in bars if 0 <= m - _bar_minute(b, parse_t, NY) < 15)
        normal = None
        if hist and len(hist) >= 3:
            normal = sum(c[m] - c[max(0, m - 15)] for c in hist) / len(hist)
        if not normal and avg_vol:
            normal = avg_vol / 390 * 15
        if normal and normal > 0:
            rv15 = recent / normal
    now_pts = 50
    if rv15 is not None:
        now_pts = _step(rv15, [(5, 100), (2, 65), (1, 35)], 5)
        ind.append(["Relative volume (last 15 min)", "%.1fx" % rv15 if rv15 <= 100 else "100x+",
                    "good" if rv15 >= 2 else "warn" if rv15 >= 1 else "bad"])
    # trend: last 5 one-minute candles vs the 5 before
    trend = None
    if len(bars) >= 10:
        a, b = sum(x["v"] for x in bars[-5:]), sum(x["v"] for x in bars[-10:-5])
        trend = a / b if b else None
        if trend is not None:
            ind.append(["Volume trend (5 vs prior 5 min)", "%s %.1fx" % ("rising" if trend >= 1.2 else "fading" if trend < 0.6 else "steady", trend),
                        "good" if trend >= 1.2 else "bad" if trend < 0.6 else "info"])
    score = round(0.75 * base + 0.25 * now_pts)
    if trend is not None and trend < 0.4 and rv15 is not None and rv15 < 2:
        score = min(score, 55)      # the crowd has left
    if rvol >= 5 and (rv15 is None or rv15 >= 2):
        why = "The crowd is here, and still here right now: breakouts get follow-through."
    elif rvol >= 5:
        why = "Huge volume earlier, but the last 15 minutes are quiet: the crowd may have moved on."
    elif rvol >= 3:
        why = "More interest than normal, but not a crowd favorite yet (5x+ is the bar)."
    else:
        why = "Normal volume: no crowd, breakouts often fail."
    return dict(key="rvol", name="High relative volume", ok=rvol >= 5, score=score,
                value="%.1fx" % rvol if rvol <= 100 else "100x+", tier="extreme" if rvol >= 10 else "high" if rvol >= 5 else "some" if rvol >= 3 else "low",
                indicators=ind, why=why, ref="[RC] relative volume 5x+; [AZ] stocks in play; [GKM] high-volume premium",
                rv15=None if rv15 is None else round(rv15, 1))


# ------------------------------------------------------------------ 4. HIGH VOLUME (absolute)
def volume_pillar(fullvol, bars, vw, p, avg_vol, now, press):
    ind = []
    iexvol = sum(b["v"] for b in bars) if bars else 0
    vol = fullvol or iexvol
    src = "full market" if fullvol else "data feed"
    pre = now.time() < dtime(9, 30)
    k = 0.2 if pre else 1.0            # pre-market: a fifth of the regular-session bar
    sh_pts = _step(vol, [(5e6 * k, 100), (2e6 * k, 85), (1e6 * k, 70), (5e5 * k, 45), (2e5 * k, 20)], 5)
    ind.append(["Shares traded today", "%s (%s)" % (_big(vol), src), "good" if sh_pts >= 70 else "warn" if sh_pts >= 45 else "bad"])
    dollars = vol * (vw or p)
    d_pts = _step(dollars, [(2e7 * k, 100), (5e6 * k, 75), (1e6 * k, 45)], 10)
    ind.append(["Dollar volume", "$%s" % _big(dollars), "good" if d_pts >= 75 else "warn" if d_pts >= 45 else "bad"])
    if avg_vol:
        pct = vol / avg_vol * 100
        ind.append(["vs a normal full day", "%.0f%% of the 20-day average" % pct, "good" if pct >= 100 else "info"])
    pr_pts = 50
    if press is not None:
        pr_pts = _step(press, [(2, 100), (1.2, 70), (0.8, 40)], 10)
        ind.append(["Buy vs sell volume (10 min)", "%.1fx" % press, "good" if press >= 1.2 else "bad" if press < 0.8 else "info"])
    score = round(0.6 * sh_pts + 0.25 * d_pts + 0.15 * pr_pts)
    ok = vol >= 1e6 * k
    why = ("Heavy, real volume: easy to get in AND out, and the move is being paid for." if ok and d_pts >= 75 else
           "Enough volume to trade, but not a heavy crowd yet." if ok else
           "Thin: few shares trading, so prices jump and exits can be hard.")
    return dict(key="volume", name="High volume", ok=ok, score=score, value=_big(vol), tier="heavy" if sh_pts >= 85 else "ok" if ok else "thin",
                indicators=ind, why=why, ref="[RC] 1M+ shares traded; [LS] volume = fuel; [GKM]")


# ------------------------------------------------------------------ 5. GOOD CATALYST
# (points, label, regex). The best match in the freshest headlines wins. Negative ones override.
CAT_TIERS = [
    (100, "FDA approval / clearance", r"\bfda\b.*\b(approv|clear|grant)|\b(approv|clear)\w*\b.*\bfda\b|\bemergency use\b|\b510\(k\)"),
    (100, "Positive trial results", r"\b(phase\s*(2|3|ii|iii)|pivotal|topline|top-line)\b.*\b(positive|met|meets|success|significant)|\bprimary endpoint\b.*\bmet\b|\bmet\b.*\bprimary endpoint"),
    (100, "Buyout / merger", r"\b(to be acquired|acquired by|buyout|takeover|tender offer|definitive merger agreement|merger agreement|agree(s|ment)? to (be acquired|merge)|go(ing)?[- ]private)\b"),
    (85, "Big contract / order", r"\b(wins?|won|secures?|awarded|receives?|lands?)\b.*\b(contract|order|award|tender)|\b(contract|purchase order|award(ed)?|order)\b.*\$\s?\d|\$\s?\d[\d.,]*\s?(k|m|mm|million|b|billion)\b.*\b(contract|order|award)|\b(government|dod|department of|army|navy|air force|nasa)\b.*\b(contract|award)"),
    (85, "Regulatory approval", r"\b(ce mark|health canada|ema|nmpa|pmda|regulatory)\b.*\b(approv|clear)|\breceives?\b.*\bapproval\b"),
    (85, "Earnings beat / guidance up", r"\b(record (revenue|quarter|sales)|beats?\b|raises? (guidance|outlook)|profit(able)?\b|revenue (up|grows|increase))"),
    (80, "Breakthrough / designation", r"\b(breakthrough|fast track|orphan drug|priority review|rare pediatric)\b.*\bdesignation|\bdesignation\b"),
    (75, "Partnership / acquisition", r"\b(partnership|partner(s|ed|ing)? with|strategic (alliance|agreement|investment)|collaborat|licens(e|ing) agreement|acquires?|acquisition|joint venture)\b"),
    (70, "Agreement / deal", r"\b(agreement|deal|signs?|selected by|chosen by)\b"),
    (70, "Clinical / trial news", r"\b(phase\s*(1|2|3|i|ii|iii)|trial|clinical|study|enrollment|preclinical)\b"),
    (65, "Earnings report", r"\b(earnings|quarter(ly)? results|q[1-4] (results|revenue)|fiscal)\b"),
    (70, "Patent / launch / uplisting", r"\b(patent (granted|issued)|launch(es|ed)?|uplist|nasdaq listing|commercial(ization)?)\b"),
    (60, "Hot sector story", r"\b(ai|artificial intelligence|quantum|bitcoin|crypto|blockchain|nuclear|uranium|drone|robot\w*|data center|space)\b"),
    (55, "Upgrade / price target", r"\b(upgrade[sd]?|price target|initiat\w+ coverage|buy rating)\b"),
    (30, "Company update", r"\b(announces|update|presents?|conference|webcast|letter to shareholders|appoints)\b"),
]
# non-binding deals often never close: whatever else the headline says, it caps the grade
LOI_RX = r"\b(letter of intent|loi|memorandum of understanding|mou|non-binding|term sheet)\b"
DILUTION_RX =r"\b(securities purchase agreement|offering|registered direct|private placement|shelf|dilution|warrants?|at-the-market|atm program|convertible|priced at)\b"
BAD_TIERS = [
    ("dilution", "Offering / dilution", DILUTION_RX),
    ("dilution", "Reverse split", r"\breverse (stock )?split\b"),
    ("bad", "Delisting / deficiency", r"\b(delist\w*|deficiency|non-?compliance|minimum bid)\b"),
    ("bad", "FDA rejection / failed trial", r"\b(complete response letter|crl|fail(s|ed)?|did not meet|halt(s|ed)? (the )?trial|discontinu)\w*"),
    ("bad", "Going concern / bankruptcy", r"\b(going concern|bankruptcy|chapter 11)\b"),
    ("bad", "Lawsuit / investigation", r"\b(class action|investigation|subpoena|sec charges)\b"),
    ("bad", "Earnings miss / guidance cut", r"\b(miss(es|ed)?|lowers? (guidance|outlook)|cuts? (guidance|outlook))\b"),
]


def _ts(n, parse_t):
    try:
        return parse_t(n["t"]).timestamp()
    except Exception:
        return None


def catalyst_pillar(news_recent, news_all, sec, now, parse_t):
    """Grade the reason for the move. Returns the pillar dict plus kind in (strong, weak, dilution, none)."""
    ind = []
    best = None           # (points, label, headline, age_h, src)
    neg = None
    for n in news_recent[:6]:
        h = n.get("h") or ""
        hl = h.lower()
        t = _ts(n, parse_t)
        age = (now.timestamp() - t) / 3600 if t else None
        for kind, lab, rx in BAD_TIERS:
            if re.search(rx, hl):
                if neg is None or (kind == "dilution" and neg[0] != "dilution"):
                    neg = (kind, lab, h, age)
                break
        hits = [(pts, lab) for pts, lab, rx in CAT_TIERS if re.search(rx, hl)]
        if re.search(LOI_RX, hl):
            hits = [(40, "Letter of intent / MOU")]
        if hits:
            pts, lab = max(hits)
            if best is None or pts > best[0]:
                best = (pts, lab, h, age, n.get("src"))
    if not news_recent:
        older = [n for n in news_all if _ts(n, parse_t) and now.timestamp() - _ts(n, parse_t) <= 48 * 3600]
        ind.append(["Headlines (last 18h)", "none", "bad"])
        if older:
            ind.append(["Older news (18-48h)", older[0]["h"][:70], "info"])
        return dict(key="catalyst", name="Good catalyst", ok=False, score=10 if older else 0, value="none", tier="no news",
                    indicators=ind, why="No fresh reason for the move. Moves without news tend to fade or reverse.",
                    ref="[RC] fresh news; [CH] no-news moves reverse", kind="none")
    if neg and neg[0] == "dilution":
        ind.append(["Red flag", "%s: %s" % (neg[1], neg[2][:70]), "bad"])
        return dict(key="catalyst", name="Good catalyst", ok=False, score=0, value="dilution!", tier="dilution",
                    indicators=ind, why="Offering or reverse split: more shares are coming, sellers cap every bounce.",
                    ref="[RC] avoid offerings; [TS] negative words matter most", kind="dilution")
    pts = best[0] if best else 25
    lab = best[1] if best else neg[1] if neg else "Unclear headline"
    head = best[2] if best else news_recent[0]["h"]
    age = best[3] if best else None
    ind.append(["Type", lab, "good" if pts >= 75 else "warn" if pts >= 50 else "bad"])
    ind.append(["Headline", head[:90], "info"])
    # freshness: news that is minutes old has the most buyers still coming
    if age is not None:
        fresh = 1.0 if age <= 1 else 0.95 if age <= 4 else 0.88 if age <= 12 else 0.8
        ind.append(["Age", ("%d min" % max(1, age * 60)) if age < 1 else "%.1f h" % age, "good" if age <= 4 else "info"])
    else:
        fresh = 0.85
    score = pts * fresh
    srcs = {n.get("src") for n in news_recent[:6] if n.get("src")}
    if len(srcs) >= 2:
        score += 5
        ind.append(["Sources", ", ".join(sorted(srcs)), "good"])
    k8 = [f for f in (sec or []) if f.get("form") in ("8-K", "6-K") and f.get("age", 99) <= 1]
    if k8:
        score += 8
        ind.append(["SEC confirms", "%s filed %s" % (k8[0]["form"], k8[0]["date"]), "good"])
    if len(news_recent) >= 3:
        ind.append(["Headlines (18h)", str(len(news_recent)), "info"])
    if neg:
        score -= 35
        ind.append(["Red flag", "%s: %s" % (neg[1], neg[2][:70]), "bad"])
    score = round(_clamp(score))
    # confirmations (SEC filing, second source) add points but never turn a generic headline into a strong catalyst
    strong = score >= 70 and pts >= 70
    if not strong:
        score = min(score, 69)
    kind = "strong" if strong else "weak"
    ok = True if strong else None if score >= 40 else False
    why = ("A specific, fresh reason for the move: buyers keep coming back all morning." if strong else
           "There is news, but it's not a strong reason: take profits faster." if score >= 40 else
           "Weak or negative news: the move has little to stand on.")
    return dict(key="catalyst", name="Good catalyst", ok=ok, score=score, value=lab,
                tier=kind, indicators=ind, why=why, ref="[RC] fresh news; [CH] news drift vs no-news reversal; [TS] headline words",
                kind=kind)


# ------------------------------------------------------------------ all five
def evaluate(c, parse_t, NY):
    """c: dict(price, prev_close, bars, daily, fund, fullvol, rvol, rsrc, hist, avg_vol, vwap, press, news_recent,
    news_all, sec, now). Returns dict(list=[5 pillars], score, grade, met, label, summary, kind, rv15)."""
    now = c["now"]
    P = [price_pillar(c["price"], c.get("daily") or [], c.get("bars") or [], c.get("prev_close")),
         float_pillar(c.get("fund") or {}, c.get("fullvol")),
         rvol_pillar(c["rvol"], c.get("rsrc", ""), c.get("bars") or [], c.get("hist") or [], c.get("avg_vol"), now, parse_t, NY),
         volume_pillar(c.get("fullvol"), c.get("bars") or [], c.get("vwap"), c["price"], c.get("avg_vol"), now, c.get("press")),
         catalyst_pillar(c.get("news_recent") or [], c.get("news_all") or [], c.get("sec") or [], now, parse_t)]
    score = round(sum(WEIGHTS[x["key"]] * x["score"] for x in P) / 100)
    met = sum(1 for x in P if x["ok"])
    unknown = sum(1 for x in P if x["ok"] is None)
    grade = "A+" if met == 5 and score >= 85 else "A" if met >= 4 and score >= 75 else "B" if score >= 60 else "C" if score >= 40 else "D"
    weakest = min(P, key=lambda x: x["score"])
    label = "%d/5 pillars" % met + (" (+%d ?)" % unknown if unknown else "")
    summary = ("All 5 pillars: a textbook momentum stock." if met == 5 else
               "Missing: %s." % ", ".join(x["name"].lower() for x in P if x["ok"] is False) if any(x["ok"] is False for x in P) else
               "Close: %s is borderline." % weakest["name"].lower())
    return dict(list=P, score=score, grade=grade, met=met, label=label, summary=summary,
                kind=P[4]["kind"], rv15=P[2].get("rv15"), weakest=weakest["key"])
