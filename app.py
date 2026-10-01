import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st

DEFAULT_URL = "https://api.anticorruzione.it/apicig/1.0.0/getSmartCig/{cig}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
CIG_RE = re.compile(r"\b[0-9A-Za-z]{10}\b")
DETAIL_URL = "https://dati.anticorruzione.it/superset/dashboard/dettaglio_cig/?cig={}"

st.set_page_config(page_title="Ricerca CIG - API ANAC", page_icon="🔎", layout="wide")


def parse_cigs(text: str) -> list[str]:
    seen, out = set(), []
    for c in CIG_RE.findall(text or ""):
        c = c.upper()
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def fetch_one(cig: str, url_tpl: str, timeout: int, retries: int, extra_headers: dict):
    """Restituisce (cig, esito, payload). esito: ok | non_trovato | errore."""
    url = url_tpl.format(cig=cig)
    last = ""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers={**HEADERS, **extra_headers}, timeout=timeout)
            if r.status_code == 200:
                try:
                    return cig, "ok", r.json()
                except ValueError:
                    return cig, "errore", f"Risposta non JSON: {r.text[:200]}"
            if r.status_code in (404, 204):
                return cig, "non_trovato", f"HTTP {r.status_code}"
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            return cig, "errore", last
        except requests.RequestException as e:
            last = str(e)
            time.sleep(1.0 * (attempt + 1))
    return cig, "errore", last


def flatten(cig: str, payload) -> list[dict]:
    """Appiattisce il JSON in una o più righe; liste annidate -> stringa JSON."""
    records = payload if isinstance(payload, list) else [payload]
    rows = []
    for rec in records:
        if not isinstance(rec, dict):
            rec = {"valore": rec}
        flat = pd.json_normalize(rec, sep=".").iloc[0].to_dict()
        for k, v in flat.items():
            if isinstance(v, (list, dict)):
                flat[k] = json.dumps(v, ensure_ascii=False)
        rows.append({"cig_cercato": cig, **flat})
    return rows


# ------------------------------------------------------------------ UI
st.title("🔎 Ricerca CIG tramite API ANAC")
st.caption("Una chiamata per ogni CIG all'API `getSmartCig` di ANAC; risultati visualizzabili e scaricabili.")

with st.sidebar:
    st.header("⚙️ Impostazioni")
    url_tpl = st.text_input(
        "URL API (usa {cig} come segnaposto)", DEFAULT_URL,
        help="Se in futuro cambia l'endpoint basta modificarlo qui.",
    )
    workers = st.slider("Richieste in parallelo", 1, 10, 4,
                        help="Tieni basso per non sovraccaricare ANAC / evitare blocchi.")
    timeout = st.slider("Timeout (secondi)", 5, 120, 30)
    retries = st.slider("Tentativi aggiuntivi", 0, 5, 2)
    token = st.text_input("Header Authorization (opzionale)", "", type="password",
                          help="Es. 'Bearer xxx', solo se l'API lo richiedesse.")

tab_txt, tab_file = st.tabs(["✍️ Incolla lista", "📄 Carica file"])
with tab_txt:
    txt = st.text_area("CIG (uno per riga o separati da virgola/spazio/;)", height=160,
                       placeholder="918052266A\nB1234567CD")
with tab_file:
    up = st.file_uploader("CSV / TXT / XLSX con i CIG", type=["csv", "txt", "xlsx"])
    file_txt = ""
    if up is not None:
        if up.name.lower().endswith(".xlsx"):
            file_txt = pd.read_excel(up, dtype=str).to_csv(index=False)
        else:
            file_txt = up.read().decode("utf-8", errors="ignore")

cigs = parse_cigs(txt + "\n" + file_txt)
st.write(f"**CIG riconosciuti:** {len(cigs)}")

if st.button("🚀 Interroga ANAC", type="primary", disabled=not cigs):
    extra = {"Authorization": token} if token else {}
    rows, raw, esiti = [], {}, {}
    bar = st.progress(0.0, text="Avvio…")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_one, c, url_tpl, timeout, retries, extra) for c in cigs]
        for i, f in enumerate(as_completed(futs), 1):
            cig, esito, payload = f.result()
            esiti[cig] = (esito, payload if esito != "ok" else "")
            if esito == "ok":
                raw[cig] = payload
                rows += flatten(cig, payload)
            bar.progress(i / len(cigs), text=f"{i}/{len(cigs)} CIG elaborati")
    bar.empty()

    order = {c: i for i, c in enumerate(cigs)}
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("cig_cercato", key=lambda s: s.map(order)).reset_index(drop=True)
    st.session_state.update(df=df, raw=raw, esiti=esiti, cigs=cigs)

if "df" in st.session_state:
    df: pd.DataFrame = st.session_state["df"]
    raw, esiti, searched = st.session_state["raw"], st.session_state["esiti"], st.session_state["cigs"]
    nf = [c for c, (e, _) in esiti.items() if e == "non_trovato"]
    err = {c: m for c, (e, m) in esiti.items() if e == "errore"}

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("CIG cercati", len(searched))
    c2.metric("Trovati", len(raw))
    c3.metric("Non trovati", len(nf))
    c4.metric("Errori", len(err))

    if df.empty:
        st.error("Nessun risultato. Controlla l'URL dell'API o gli errori qui sotto.")
    else:
        stato_cols = [c for c in df.columns if "stato" in c.lower()]
        if stato_cols:
            st.caption(f"Colonne di stato rilevate: {', '.join(stato_cols)}")
        st.subheader("Risultati")
        st.dataframe(df, use_container_width=True, height=420)

        d1, d2, d3 = st.columns(3)
        d1.download_button("⬇️ CSV", df.to_csv(index=False, sep=";").encode("utf-8-sig"),
                           "cig_anac.csv", "text/csv")
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            df.to_excel(xw, index=False, sheet_name="CIG")
            pd.DataFrame(
                [{"cig": c, "esito": e, "dettaglio": m} for c, (e, m) in esiti.items() if e != "ok"]
            ).to_excel(xw, index=False, sheet_name="Non trovati-errori")
        d2.download_button("⬇️ Excel", buf.getvalue(), "cig_anac.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        d3.download_button("⬇️ JSON originale", json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8"),
                           "cig_anac_raw.json", "application/json")

        with st.expander("Vedi JSON grezzo per CIG"):
            pick = st.selectbox("CIG", list(raw))
            st.json(raw[pick])

    if nf:
        with st.expander(f"CIG non trovati ({len(nf)})"):
            st.write(nf)
            st.markdown(", ".join(f"[{c}]({DETAIL_URL.format(c)})" for c in nf[:30]))
    if err:
        with st.expander(f"Errori ({len(err)})"):
            st.dataframe(pd.DataFrame({"cig": list(err), "errore": list(err.values())}))
