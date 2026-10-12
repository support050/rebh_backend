"""One-page Arabic daily summary (Markdown): score, regime, change vs yesterday, what flipped.

python -m breadth.reports.daily_summary -> storage/reports/summary_<date>.md (+ summary_latest.md)
Headline numbers only. No charts, no detail.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from breadth import ROOT, load_config
from breadth.reports.dashboard import REGIME_AR, _load, fmt


def _f(v, d=1):
    return fmt(v, d).replace("⁦", "").replace("⁩", "")


def build(cfg: dict) -> str:
    F, st = _load(cfg)
    comp = F["composite"].dropna(subset=["score"])
    part, ad, th, mom, secs, conc, div = (F[k] for k in ["participation", "advance_decline", "thrusts", "momentum", "sectors_summary", "concentration", "divergences"])
    tasi = F["tasi"]
    d, p = comp.index[-1], comp.index[-2]
    r, q = comp.loc[d], comp.loc[p]
    lines = [f"# ملخص اتساع السوق السعودي — {d.strftime('%Y-%m-%d')}", ""]
    lines.append(f"**Breadth Score: {r['score']:.0f} / 100** ({r['score'] - q['score']:+.1f} عن أمس) — **النظام: {REGIME_AR[r['regime']]}**"
                 + (f" (تغيّر من {REGIME_AR[q['regime']]})" if r["regime"] != q["regime"] else f" منذ {comp.index[(comp['regime']!=comp['regime'].shift())][-1].strftime('%Y-%m-%d')}"))
    if d in tasi.index and p in tasi.index:
        lines.append(f"TASI {_f(tasi.loc[d],0)} ({100*(tasi.loc[d]/tasi.loc[p]-1):+.2f}%) · الدافع اليوم: {str(r['driver']).replace('_',' ')} · Overlay (تشبّع/ثرست): {r['overlay']:.0f}")
    else:
        lines.append(f"TASI: ⚠ ملف المؤشر (historical_reports_tasi) غير محدّث حتى {d.strftime('%Y-%m-%d')} — آخر إغلاق متاح {tasi.index.max().strftime('%Y-%m-%d')} = {_f(tasi.iloc[-1],0)} · الدافع اليوم: {str(r['driver']).replace('_',' ')} · Overlay: {r['overlay']:.0f}")
    lines.append("")
    lines.append("## الأرقام الرئيسية")
    pn, pp, an, ap_ = part.loc[d], part.loc[p], ad.loc[d], ad.loc[p]
    rows = [
        ("%>200DMA", pn["pct_above_sma200"], pp["pct_above_sma200"], 0), ("%>50DMA", pn["pct_above_sma50"], pp["pct_above_sma50"], 0),
        ("% Stage 2", pn["pct_stage2"], pp["pct_stage2"], 0), ("% Trend Template (Minervini)", pn["pct_minervini"], pp["pct_minervini"], 1),
        ("NH / NL (52 أسبوع)", pn["new_highs"], pn["new_lows"], 0), ("قمم جديدة بحجم / كل القمم", pn["new_highs_vol"], pn["new_highs"], 0),
        ("McClellan", an["mcclellan_osc"], ap_["mcclellan_osc"], 0), ("TRIN-10", an["trin_10"], ap_["trin_10"], 2),
        ("صاعد/هابط ≥4%", mom.loc[d, "up_x"], mom.loc[d, "down_x"], 0), ("نسبة 4% (10 أيام)", mom.loc[d, "ratio_10"], mom.loc[p, "ratio_10"], 2),
        ("قطاعات صحية / ضعيفة", secs.loc[d, "n_healthy"], secs.loc[d, "n_weak"], 0), ("EW − TASI (60 يوم، نقاط)", conc.loc[d, "spread_60"], conc.loc[p, "spread_60"], 1),
    ]
    lines.append("| المقياس | اليوم | أمس / المقابل |"); lines.append("|---|---|---|")
    for n, a, b, dd in rows:
        lines.append(f"| {n} | {_f(a, dd)} | {_f(b, dd)} |")
    lines.append("")
    # what flipped
    flips = []
    if r["regime"] != q["regime"]:
        flips.append(f"النظام تغيّر: {REGIME_AR[q['regime']]} → {REGIME_AR[r['regime']]}")
    for c, name in [("zweig_signal", "Zweig Breadth Thrust"), ("whaley_signal", "Whaley thrust"), ("pct50_thrust_signal", "ثرست %>50DMA"), ("lowry_down_signal", "يوم استسلام 95%")]:
        if bool(th.loc[d, c]):
            flips.append(f"إشارة اليوم: {name}")
    if np.sign(an["mcclellan_osc"]) != np.sign(ap_["mcclellan_osc"]):
        flips.append(f"McClellan عبر الصفر ({'صاعد' if an['mcclellan_osc'] > 0 else 'هابط'})")
    for col, name in [("pct_above_sma200", "%>200DMA"), ("pct_above_sma50", "%>50DMA")]:
        for lvl in (20, 40, 60, 80):
            if (pn[col] >= lvl) != (pp[col] >= lvl):
                flips.append(f"{name} {'تجاوز' if pn[col] >= lvl else 'كسر'} {lvl}%")
    s5 = part["net_nh_nl"].rolling(5).sum(); thr5 = -0.75 * part["n_nhnl"].rolling(5).mean()
    if s5.loc[d] <= thr5.loc[d]:
        flips.append(f"⚠ كناري NH−NL نشط: مجموع 5 أيام {s5.loc[d]:.0f} ≤ الحد {thr5.loc[d]:.0f}")
    elif s5.loc[d] <= 0.6 * thr5.loc[d]:
        flips.append(f"قريب من الكناري: مجموع 5 أيام {s5.loc[d]:.0f} (الحد {thr5.loc[d]:.0f})")
    if bool(div.loc[d, "bearish_signal"]):
        flips.append("قمة 60 يوم للمؤشر بدون تأكيد من ≥2 مقياس (تحذير جودة تأكيد)")
    if (secs.loc[d, "n_healthy"] > 0) != (secs.loc[p, "n_healthy"] > 0):
        flips.append("عدد القطاعات الصحية " + ("عاد فوق الصفر" if secs.loc[d, "n_healthy"] > 0 else "أصبح صفرًا"))
    if r["overlay"] >= 75 and q["overlay"] < 75:
        flips.append("Overlay دخل منطقة تشبّع البيع (≥75)")
    lines.append("## ما الذي تغيّر اليوم")
    lines += [f"- {x}" for x in flips] or ["- لا تغيّر جوهري"]
    lines.append("")
    lines.append("## القراءة")
    reg = r["regime"]
    if reg == "Washout":
        read = "استسلام: الاتساع ضعيف جدًا مع تشبّع بيع — تاريخيًا أفضل نقطة دخول (متوسط +2.4% خلال 20 يوم خارج العينة)."
    elif reg == "Weak":
        read = "اتساع ضعيف بلا تشبّع بيع — تاريخيًا أسوأ نظام (≈ صفر عائد خلال 60 يوم). الانتظار حتى Washout أو ثرست."
    elif reg == "Neutral":
        read = "اتساع محايد — لا ميزة إحصائية واضحة؛ راقب اتجاه الـ Score و%>200DMA."
    elif reg == "Healthy":
        read = "مشاركة واسعة — النظام الأفضل تاريخيًا (≈75% من الحالات إيجابية خلال 20 و60 يوم)."
    else:
        read = "مشاركة واسعة جدًا — إيجابي لكن راقب منطقة 60–80 في %>200DMA: كسرها من أعلى كان تاريخيًا بداية القمم."
    lines.append(read)
    if pn["pct_above_sma200"] >= 60 and pn["pct_above_sma200"] < 80:
        lines.append("تحذير: %>200DMA في منطقة 60–80 — منطقة القمم في TASI تاريخيًا (−2.9% خلال 60 يوم خارج العينة).")
    lines.append("")
    lines.append(f"_يُنشأ محليًا بواسطة TASI Breadth Engine — الـ dashboard الكامل: storage/reports/dashboard_{d.date()}.html_")
    text = "\n".join(lines)
    out = st / "reports"; out.mkdir(exist_ok=True)
    pth = out / f"summary_{d.date()}.md"; pth.write_text(text, encoding="utf-8")
    (out / "summary_latest.md").write_text(text, encoding="utf-8")
    return str(pth)


if __name__ == "__main__":
    print(build(load_config()))
