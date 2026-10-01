import io
import json
import re
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
ANAC_BASE = "https://dati.anticorruzione.it/opendata"
HEADERS = {
    # Il WAF di ANAC blocca gli User-Agent non-browser
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
CIG_RE = re.compile(r"\b[0-9A-Za-z]{10}\b")
DETAIL_URL = "https://dati.anticorruzione.it/superset/dettaglio_cig/{}"

st.set_page_config(page_title="Ricerca CIG - ANAC", page_icon="🔎", layout="wide")


# ----------------------------------------------------------------------------
# Utilità
# ----------------------------------------------------------------------------
def parse_cigs(text: str) -> list[str]:
    """Estrae i CIG (10 caratteri alfanumerici) da un testo libero, senza duplicati."""
    seen, out = set(), []
    for c in CIG_RE.findall(text or ""):
        c = c.upper()
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def get_resources(base: str, dataset_id: str) -> list[dict]:
    """Elenco delle risorse di un dataset CKAN (POST: ANAC blocca le GET con query string)."""
    r = requests.post(
        f"{base}/api/3/action/package_show",
        json={"id": dataset_id},
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    res = r.json()["result"]["resources"]
    return [
        {
            "dataset": dataset_id,
            "name": x.get("name") or x.get("id"),
            "format": (x.get("format") or "").upper(),
            "url": x.get("url"),
            "size": x.get("size"),
        }
        for x in res
        if x.get("url")
    ]


def download(url: str, dest: Path, progress_cb=None) -> None:
    with requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(min(done / total, 1.0))


def _cig_column(cols) -> str | None:
    for c in cols:
        if str(c).strip().lower() == "cig":
            return c
    for c in cols:
        if "cig" == str(c).strip().lower().replace("_", ""):
            return c
    return None


def _filter_csv(fileobj, wanted: set[str]) -> list[pd.DataFrame]:
    head = fileobj.read(65536)
    fileobj.seek(0)
    enc = "utf-8"
    try:
        sample = head.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        enc, sample = "latin-1", head.decode("latin-1")
    first = sample.splitlines()[0] if sample else ""
    sep = max([";", ",", "|", "\t"], key=first.count)
    found = []
    for chunk in pd.read_csv(
        fileobj, sep=sep, dtype=str, chunksize=200_000,
        encoding=enc, on_bad_lines="skip", low_memory=False,
    ):
        col = _cig_column(chunk.columns)
        if col is None:
            continue
        m = chunk[chunk[col].str.strip().str.upper().isin(wanted)]
        if not m.empty:
            found.append(m)
    return found


def _filter_json(fileobj, wanted: set[str]) -> list[pd.DataFrame]:
    rows = []
    first = fileobj.read(1)
    fileobj.seek(0)
    if first == b"[":  # array JSON
        for rec in json.load(fileobj):
            c = next((v for k, v in rec.items() if k.lower() == "cig"), None)
            if c and str(c).strip().upper() in wanted:
                rows.append(rec)
    else:  # JSON lines
        for line in fileobj:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = next((v for k, v in rec.items() if k.lower() == "cig"), None)
            if c and str(c).strip().upper() in wanted:
                rows.append(rec)
    return [pd.json_normalize(rows).astype(str)] if rows else []


def scan_file(path: Path, fmt_hint: str, wanted: set[str]) -> list[pd.DataFrame]:
    """Legge un file (csv/json/jsonl, anche dentro uno zip) e restituisce le righe con i CIG cercati."""
    out = []

    def handle(fobj, name):
        n = name.lower()
        if n.endswith((".json", ".jsonl", ".ndjson")) or (
            not n.endswith(".csv") and fmt_hint == "JSON"
        ):
            return _filter_json(fobj, wanted)
        return _filter_csv(fobj, wanted)

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                with z.open(info) as f:
                    out += handle(io.BufferedReader(f), info.filename)
    else:
        with open(path, "rb") as f:
            out += handle(f, path.name)
    return out


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.title("🔎 Ricerca CIG nei dati aperti ANAC")
st.caption(
    "Inserisci una lista di CIG: l'app scansiona i dump open data ANAC "
    "(dati.anticorruzione.it) e restituisce i record trovati, da visualizzare e scaricare."
)

with st.sidebar:
    st.header("⚙️ Impostazioni")
    base = st.text_input("Base URL portale", ANAC_BASE)
    years = st.multiselect(
        "Anni (dataset `cig-AAAA`)",
        options=list(range(2026, 2006, -1)),
        default=[2025, 2026],
    )
    fmt = st.radio("Formato dei dump", ["CSV", "JSON"], horizontal=True)
    stop_early = st.checkbox("Ferma la ricerca quando ho trovato tutti i CIG", True)
    st.info(
        "I dump ANAC sono mensili e molto grandi (centinaia di MB). "
        "Restringi anni e mesi per velocizzare."
    )

# --- Input CIG
tab_txt, tab_file = st.tabs(["✍️ Incolla lista", "📄 Carica file"])
with tab_txt:
    txt = st.text_area(
        "CIG (uno per riga, oppure separati da virgola/spazio/;)",
        height=160,
        placeholder="918052266A\nB1234567CD",
    )
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
if cigs:
    with st.expander("Mostra elenco"):
        st.write(cigs)

# --- Risorse
resources: list[dict] = []
if years:
    for y in years:
        try:
            resources += get_resources(base, f"cig-{y}")
        except Exception as e:
            st.warning(f"Dataset cig-{y}: impossibile leggere i metadati ({e})")
    resources = [r for r in resources if r["format"] == fmt]

selected: list[dict] = []
if resources:
    df_res = pd.DataFrame(resources)
    labels = [f"{r['dataset']} · {r['name']}" for r in resources]
    chosen = st.multiselect(
        "File (mesi) da scansionare", labels, default=labels,
        help="Togli i mesi che non ti interessano per ridurre i tempi.",
    )
    selected = [r for r, l in zip(resources, labels) if l in chosen]
    st.caption(f"{len(selected)} file selezionati")
elif years:
    st.warning("Nessuna risorsa trovata per i filtri scelti.")

# --- Ricerca
if st.button("🚀 Cerca", type="primary", disabled=not (cigs and selected)):
    wanted = set(cigs)
    found_frames: list[pd.DataFrame] = []
    found_set: set[str] = set()
    overall = st.progress(0.0, text="Avvio…")
    status = st.empty()

    for i, res in enumerate(selected, 1):
        status.info(f"({i}/{len(selected)}) {res['dataset']} · {res['name']}")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "dump"
            try:
                download(
                    res["url"], dest,
                    lambda p: overall.progress(
                        (i - 1 + p * 0.5) / len(selected), text=f"Download {res['name']}"
                    ),
                )
                frames = scan_file(dest, res["format"], wanted - found_set)
            except Exception as e:
                st.warning(f"Errore su {res['name']}: {e}")
                continue
        for fr in frames:
            fr = fr.copy()
            fr.insert(0, "_fonte", f"{res['dataset']} · {res['name']}")
            found_frames.append(fr)
            col = _cig_column(fr.columns)
            if col:
                found_set |= set(fr[col].str.strip().str.upper())
        overall.progress(i / len(selected), text=f"{len(found_set)}/{len(wanted)} CIG trovati")
        if stop_early and found_set >= wanted:
            break

    status.empty()
    overall.empty()
    result = pd.concat(found_frames, ignore_index=True) if found_frames else pd.DataFrame()
    st.session_state["result"] = result
    st.session_state["cigs"] = cigs

# --- Risultati
if "result" in st.session_state:
    result: pd.DataFrame = st.session_state["result"]
    searched: list[str] = st.session_state["cigs"]
    col = _cig_column(result.columns) if not result.empty else None
    found = set(result[col].str.strip().str.upper()) if col else set()
    missing = [c for c in searched if c not in found]

    c1, c2, c3 = st.columns(3)
    c1.metric("CIG cercati", len(searched))
    c2.metric("Trovati", len(found))
    c3.metric("Non trovati", len(missing))

    if result.empty:
        st.error("Nessun CIG trovato nei file scansionati. Prova ad ampliare anni/mesi.")
    else:
        st.subheader("Risultati")
        st.dataframe(result, use_container_width=True, height=420)

        d1, d2, d3 = st.columns(3)
        d1.download_button(
            "⬇️ CSV", result.to_csv(index=False, sep=";").encode("utf-8-sig"),
            "cig_anac.csv", "text/csv",
        )
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            result.to_excel(xw, index=False, sheet_name="CIG")
            pd.DataFrame({"CIG non trovati": missing}).to_excel(
                xw, index=False, sheet_name="Non trovati"
            )
        d2.download_button(
            "⬇️ Excel", buf.getvalue(), "cig_anac.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        d3.download_button(
            "⬇️ JSON", result.to_json(orient="records", force_ascii=False).encode("utf-8"),
            "cig_anac.json", "application/json",
        )

    if missing:
        with st.expander(f"CIG non trovati ({len(missing)})"):
            st.write(missing)
            st.markdown(
                "Verifica a mano sul portale: "
                + ", ".join(f"[{c}]({DETAIL_URL.format(c)})" for c in missing[:30])
            )
