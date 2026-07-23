# -*- coding: utf-8 -*-
"""
Смывы · панель службы качества.
Загрузка Excel-журнала -> чистая база -> дашборд, журнал несоответствий,
история по точкам, выгрузка свода. Streamlit + Plotly.
"""
import io
import os
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from parser import parse_workbook, KMA_LIMIT

st.set_page_config(page_title="Смывы · Служба качества", page_icon="🧫",
                   layout="wide", initial_sidebar_state="expanded")

# ---------- палитра (клиническая) ----------
ACCENT = "#0e8f86"
GOOD = "#0a8f24"
WARN = "#d98a00"
CRIT = "#cf3b3b"
INK = "#0e1618"
MUTED = "#8a938f"
GRID = "#e4e8e7"
CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#c98500", "#e87ba4", "#4a3aa7"]

st.markdown(f"""
<style>
  .block-container {{ padding-top: 1.6rem; max-width: 1300px; }}
  h1, h2, h3 {{ letter-spacing: -0.02em; }}
  [data-testid="stMetricValue"] {{ font-variant-numeric: tabular-nums; }}
  .kpi-card {{ background: var(--background-color); border: 1px solid {GRID};
    border-radius: 14px; padding: 16px 18px; }}
  .tag {{ font-family: ui-monospace, monospace; font-size: 11px; letter-spacing:.05em;
    text-transform: uppercase; color: {MUTED}; }}
  .big {{ font-size: 34px; font-weight: 700; letter-spacing: -.03em; line-height: 1;
    font-variant-numeric: tabular-nums; }}
  .sub {{ font-size: 12px; color: {MUTED}; }}
  .stDataFrame {{ border-radius: 12px; }}
</style>
""", unsafe_allow_html=True)

DEFAULT_FILE = "07. Результаты смывов 2026.xlsx"


@st.cache_data(show_spinner="Разбираю журнал…")
def load(path_or_bytes, key):
    df = parse_workbook(path_or_bytes)
    return df


# ---------- источник данных ----------
st.sidebar.markdown("### 🧫 Смывы · Служба качества")
st.sidebar.caption("Микробиологический контроль оборудования")

up = st.sidebar.file_uploader("Загрузить журнал (Excel)", type=["xlsx"])
if up is not None:
    data = load(io.BytesIO(up.getvalue()), up.name + str(up.size))
    src_name = up.name
elif os.path.exists(DEFAULT_FILE):
    data = load(DEFAULT_FILE, DEFAULT_FILE + str(os.path.getmtime(DEFAULT_FILE)))
    src_name = DEFAULT_FILE
else:
    st.info("Загрузите Excel-журнал смывов в левой панели.")
    st.stop()

if data.empty:
    st.error("В файле не найдено журналов цехов с данными.")
    st.stop()

# ---------- фильтры ----------
st.sidebar.divider()
st.sidebar.markdown("**Фильтры**")
cehs = sorted(data["цех"].unique())
sel_ceh = st.sidebar.multiselect("Цех", cehs, default=cehs)
inds = [i for i in ["КМАФАнМ", "БГКП", "Proteus", "Salmonella", "Listeria", "Staph", "Плесень", "Дрожжи"]
        if i in data["показатель"].unique()]
sel_ind = st.sidebar.multiselect("Показатель", inds, default=inds)

dvalid = data.dropna(subset=["дата"])
dmin, dmax = dvalid["дата"].min().date(), dvalid["дата"].max().date()
dr = st.sidebar.date_input("Период", (dmin, dmax), min_value=dmin, max_value=dmax)
if isinstance(dr, tuple) and len(dr) == 2:
    d0, d1 = pd.Timestamp(dr[0]), pd.Timestamp(dr[1])
else:
    d0, d1 = pd.Timestamp(dmin), pd.Timestamp(dmax)

f = data[data["цех"].isin(sel_ceh) & data["показатель"].isin(sel_ind)
         & data["дата"].between(d0, d1)].copy()

st.sidebar.divider()
st.sidebar.caption(f"Источник: {src_name}")

# ---------- агрегаты ----------
probes = f.groupby(["цех", "дата", "точка"]).ngroups
bad = f[f["статус"] == "несоответствие"]
over = f[f["статус"] == "превышение"]
tested = f[f["статус"] != "не тестировали"]
conform = 100 * (1 - len(bad) / max(probes, 1))

# =========================================================
st.markdown("## Панель контроля смывов")
st.caption(f"Сезон {dmin.year} · {len(sel_ceh)} цехов · автоматический разбор журнала")

c = st.columns(6)
kpis = [
    ("ПРОБ В ВЫБОРКЕ", f"{probes:,}".replace(",", " "), "уник. цех+дата+точка", INK),
    ("ЦЕХОВ", f"{f['цех'].nunique()}", "на контроле", INK),
    ("СООТВЕТСТВИЕ", f"{conform:.1f}%", f"{probes - len(bad)} из {probes} проб", GOOD),
    ("НЕСООТВЕТСТВИЙ", f"{len(bad)}", "«обнаружено»", CRIT),
    ("ПРЕВЫШЕНИЙ НОРМЫ", f"{len(over)}", "КМАФАнМ > 1×10³", CRIT),
    ("ТОЧЕК ОТБОРА", f"{f['точка'].nunique()}", "уникальных", INK),
]
for col, (cap, val, sub, color) in zip(c, kpis):
    col.markdown(
        f'<div class="kpi-card"><div class="tag">{cap}</div>'
        f'<div class="big" style="color:{color}">{val}</div>'
        f'<div class="sub">{sub}</div></div>', unsafe_allow_html=True)

st.markdown("")

# ---------- вкладки ----------
t1, t2, t3, t4 = st.tabs(["📊 Обзор", "🚨 Несоответствия", "🔍 История по точке", "📥 Свод и качество данных"])

# ======= ОБЗОР =======
with t1:
    col1, col2 = st.columns(2)

    # объём по цехам
    byc = f.groupby("цех")["точка"].count().sort_values()  # число показателей
    prb = f.groupby("цех").apply(lambda g: g.groupby(["дата", "точка"]).ngroups).sort_values()
    fig = go.Figure(go.Bar(x=prb.values, y=prb.index, orientation="h",
                           marker_color=ACCENT, hovertemplate="%{y}<br>%{x} проб<extra></extra>"))
    fig.update_layout(title="Объём контроля по цехам", height=360,
                      margin=dict(l=10, r=10, t=44, b=10), plot_bgcolor="rgba(0,0,0,0)",
                      paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor=GRID), yaxis=dict(title=""))
    col1.plotly_chart(fig, use_container_width=True)

    # динамика по месяцам
    mf = f.dropna(subset=["дата"]).copy()
    mf["мес"] = mf["дата"].dt.to_period("M").astype(str)
    prb_m = mf.groupby("мес").apply(lambda g: g.groupby(["дата", "точка"]).ngroups)
    bad_m = mf[mf["статус"] == "несоответствие"].groupby("мес").size()
    over_m = mf[mf["статус"] == "превышение"].groupby("мес").size()
    idx = prb_m.index
    fig2 = go.Figure()
    fig2.add_bar(x=idx, y=prb_m.values, name="Проб", marker_color=ACCENT,
                 hovertemplate="%{x}<br>%{y} проб<extra></extra>")
    fig2.add_scatter(x=idx, y=bad_m.reindex(idx, fill_value=0).values, name="Несоответствия",
                     mode="lines+markers", line=dict(color=CRIT, width=2), yaxis="y2")
    fig2.add_scatter(x=idx, y=over_m.reindex(idx, fill_value=0).values, name="Превышения",
                     mode="lines+markers", line=dict(color=WARN, width=2, dash="dot"), yaxis="y2")
    fig2.update_layout(title="Динамика по месяцам", height=360,
                       margin=dict(l=10, r=10, t=44, b=10), plot_bgcolor="rgba(0,0,0,0)",
                       paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor=GRID),
                       yaxis=dict(title="проб", gridcolor=GRID),
                       yaxis2=dict(title="сигналы", overlaying="y", side="right", showgrid=False),
                       legend=dict(orientation="h", y=1.14, x=0))
    col2.plotly_chart(fig2, use_container_width=True)

    col3, col4 = st.columns(2)
    # заполненность показателей
    tot_pr = probes
    fillv = (f[f["статус"] != "не тестировали"].groupby("показатель")
             .apply(lambda g: g.groupby(["дата", "точка"]).ngroups))
    order = [i for i in inds if i in fillv.index]
    fillv = fillv.reindex(order).fillna(0)
    cols_fill = [CRIT if v == 0 else (WARN if v < tot_pr * 0.1 else ACCENT) for v in fillv.values]
    fig3 = go.Figure(go.Bar(x=fillv.values, y=fillv.index, orientation="h", marker_color=cols_fill,
                            hovertemplate="%{y}<br>%{x} проб<extra></extra>"))
    fig3.update_layout(title="Что реально тестируют (проб с анализом)", height=340,
                       margin=dict(l=10, r=10, t=44, b=10), plot_bgcolor="rgba(0,0,0,0)",
                       paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor=GRID))
    col3.plotly_chart(fig3, use_container_width=True)

    # очаг КМАФАнМ
    elev = f[f["показатель"] == "КМАФАнМ"].copy()
    elev = elev[elev["кмафанм_кое"].notna()]
    elev = elev[~elev["значение"].astype(str).str.lower().str.contains("менее")]
    ec = elev.groupby("цех").size().sort_values()
    if len(ec):
        colors = [WARN if v == ec.max() else CAT[0] for v in ec.values]
        fig4 = go.Figure(go.Bar(x=ec.values, y=ec.index, orientation="h", marker_color=colors,
                                hovertemplate="%{y}<br>%{x} проб с повыш. КМАФАнМ<extra></extra>"))
        fig4.update_layout(title="Очаг повышенной микрофлоры (КМАФАнМ)", height=340,
                           margin=dict(l=10, r=10, t=44, b=10), plot_bgcolor="rgba(0,0,0,0)",
                           paper_bgcolor="rgba(0,0,0,0)", xaxis=dict(gridcolor=GRID))
        col4.plotly_chart(fig4, use_container_width=True)
    else:
        col4.info("Повышенных проб КМАФАнМ в выборке нет.")

# ======= НЕСООТВЕТСТВИЯ =======
with t2:
    st.markdown("### Журнал несоответствий и превышений")
    issues = f[f["статус"].isin(["несоответствие", "превышение"])].copy()
    if issues.empty:
        st.success("В выбранной выборке несоответствий и превышений нет.")
    else:
        issues = issues.sort_values("дата", ascending=False)
        show = issues[["дата", "цех", "точка", "показатель", "значение", "статус"]].copy()
        show["дата"] = show["дата"].dt.strftime("%d.%m.%Y")
        show.columns = ["Дата", "Цех", "Точка отбора", "Показатель", "Значение", "Статус"]
        st.dataframe(show, use_container_width=True, hide_index=True, height=460)

        st.markdown("**Повторяющиеся точки-нарушители**")
        rep = (issues.groupby(["цех", "точка"]).size().reset_index(name="случаев")
               .sort_values("случаев", ascending=False).head(10))
        rep.columns = ["Цех", "Точка отбора", "Случаев"]
        st.dataframe(rep, use_container_width=True, hide_index=True)

# ======= ИСТОРИЯ ПО ТОЧКЕ =======
with t3:
    st.markdown("### История результатов по точке отбора")
    cc1, cc2 = st.columns(2)
    ceh_pick = cc1.selectbox("Цех", sorted(f["цех"].unique()))
    pts = sorted(f[f["цех"] == ceh_pick]["точка"].unique())
    pt_pick = cc2.selectbox("Точка отбора", pts)
    hist = f[(f["цех"] == ceh_pick) & (f["точка"] == pt_pick)].copy()
    hist = hist.sort_values("дата", ascending=False)
    h = hist[["дата", "показатель", "значение", "статус"]].copy()
    h["дата"] = h["дата"].dt.strftime("%d.%m.%Y")
    h.columns = ["Дата", "Показатель", "Значение", "Статус"]

    def paint(v):
        c = {"несоответствие": CRIT, "превышение": WARN, "норма": GOOD}.get(v, MUTED)
        return f"color:{c}; font-weight:600"
    st.dataframe(h.style.map(paint, subset=["Статус"]),
                 use_container_width=True, hide_index=True, height=380)
    nbad = (hist["статус"] == "несоответствие").sum()
    nover = (hist["статус"] == "превышение").sum()
    st.caption(f"Записей: {len(hist)} · несоответствий: {nbad} · превышений: {nover}")

# ======= СВОД И КАЧЕСТВО =======
with t4:
    st.markdown("### Чистый свод для отчётов и аудита")
    clean = f.copy()
    clean_out = clean.copy()
    clean_out["дата"] = clean_out["дата"].dt.strftime("%d.%m.%Y")
    clean_out = clean_out[["дата", "цех", "точка", "показатель", "значение", "статус", "кмафанм_кое"]]
    clean_out.columns = ["Дата", "Цех", "Точка", "Показатель", "Значение", "Статус", "КМАФАнМ, КОЕ/см²"]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        clean_out.to_excel(xw, sheet_name="Свод", index=False)
        (f[f["статус"].isin(["несоответствие", "превышение"])]
         .assign(дата=lambda d: d["дата"].dt.strftime("%d.%m.%Y"))
         [["дата", "цех", "точка", "показатель", "значение", "статус"]]
         .to_excel(xw, sheet_name="Несоответствия", index=False))
    st.download_button("⬇️ Скачать чистый свод (Excel)", buf.getvalue(),
                       file_name="Свод_смывов.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.dataframe(clean_out.head(200), use_container_width=True, hide_index=True, height=320)

    st.markdown("### 🔎 Проверка качества данных")
    warns = []
    nodate = f[f["дата"].isna()]
    if len(nodate):
        warns.append(f"Строк без даты: **{nodate.groupby(['цех','точка']).ngroups}** (проверьте объединённые ячейки).")
    dup = (f.groupby(["цех", "дата", "точка", "показатель"]).size().reset_index(name="n"))
    dup = dup[dup["n"] > 1]
    if len(dup):
        warns.append(f"Задвоенных записей (цех+дата+точка+показатель): **{len(dup)}**.")
    yrs = f["дата"].dropna().dt.year.value_counts()
    if len(yrs) > 1:
        minor = yrs[yrs == yrs.min()]
        warns.append(f"Даты из разных годов: {dict(yrs)} — возможна опечатка в годе.")
    if warns:
        for w in warns:
            st.warning(w)
    else:
        st.success("Явных проблем качества данных не обнаружено.")
