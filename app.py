import streamlit as st
import pandas as pd
import numpy as np
import re
import unicodedata
from datetime import date

st.set_page_config(page_title="Générateur d'écritures - Feuilles de caisse", layout="wide")

# ============================================================
# Utils
# ============================================================

FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12
}

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def norm(s) -> str:
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return ""
    s = str(s).strip()
    s = strip_accents(s).upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def to_float(x) -> float:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0.0
    if isinstance(x, (int, float, np.number)):
        return float(x)
    s = str(x).strip().replace(" ", "").replace(",", ".")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except:
        return 0.0

def extract_dossier_number(raw_df: pd.DataFrame) -> str | None:
    max_r = min(50, raw_df.shape[0])
    max_c = min(25, raw_df.shape[1])
    for r in range(max_r):
        for c in range(max_c):
            v = raw_df.iat[r, c]
            if isinstance(v, str):
                m = re.search(r"\b(\d{6,8})\b", v)
                if m:
                    return m.group(1)
    return None

def extract_period_from_sheetname(sheet_name: str) -> tuple[int, int] | None:
    s = sheet_name.strip().lower()
    parts = s.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        year = int(parts[-1])
        month_name = " ".join(parts[:-1]).strip()
        month = FRENCH_MONTHS.get(month_name)
        if month:
            return (year, month)
    return None

def find_row_contains(raw_df: pd.DataFrame, needle: str, search_rows: int = 120) -> int | None:
    n = norm(needle)
    for r in range(min(search_rows, raw_df.shape[0])):
        row_text = " ".join(norm(x) for x in raw_df.iloc[r].tolist())
        if n in row_text:
            return r
    return None

def vat_split(ttc: float, rate: float) -> tuple[float, float]:
    rate = float(rate)
    if rate <= 0:
        return (ttc, 0.0)
    ht = ttc / (1.0 + rate)
    tva = ttc - ht
    return (ht, tva)

def mk_line(journal, dt_, piece, compte, libelle, debit, credit, dossier, tva_rate=None):
    return {
        "Journal": journal,
        "Date": dt_.isoformat(),
        "Piece": piece,
        "Compte": str(compte),
        "Libelle": libelle,
        "Debit": round(float(debit), 2),
        "Credit": round(float(credit), 2),
        "Dossier": dossier or "",
        "TVA_rate": "" if tva_rate is None else tva_rate,
    }

# ============================================================
# Détection blocs RECETTES / DEPENSES par "bornage"
# ============================================================

def detect_block_ranges(raw_df: pd.DataFrame) -> dict:
    """
    But:
      - identifier les colonnes couvertes par le bloc RECETTES et par le bloc DEPENSES
      - sans dépendre des libellés internes (peu importe nb de colonnes / noms)
    Hypothèse fréquente:
      - ligne "niveau 1" contient des titres de blocs (RECETTES / DEPENSES) souvent en cellules fusionnées
      - ligne "niveau 2" juste en dessous contient les libellés de colonnes
    Stratégie:
      - on cherche la première ligne qui contient RECETTES
      - sur cette ligne, on repère toutes les colonnes où le texte contient RECETTES / DEPENSES
      - on déduit les intervalles de colonnes de chaque bloc
    Retour:
      {
        "row_block": int,
        "row_cols": int,
        "recettes": (start_col, end_col_inclusive),
        "depenses": (start_col, end_col_inclusive)
      }
    """
    r_block = find_row_contains(raw_df, "RECETTES")
    if r_block is None:
        raise ValueError("Impossible de trouver un titre de bloc 'RECETTES' dans l'onglet.")

    # on prend la ligne suivante comme libellés colonnes (souvent)
    r_cols = min(r_block + 1, raw_df.shape[0] - 1)

    row_vals = [norm(x) for x in raw_df.iloc[r_block].tolist()]

    rec_positions = [i for i, v in enumerate(row_vals) if "RECETTES" in v]
    dep_positions = [i for i, v in enumerate(row_vals) if "DEPENSES" in v]

    if not rec_positions:
        raise ValueError("Bloc 'RECETTES' introuvable (sur la ligne titre).")
    if not dep_positions:
        # certains fichiers écrivent "DEPENSE" sans S
        dep_positions = [i for i, v in enumerate(row_vals) if "DEPENSE" in v]
    if not dep_positions:
        raise ValueError("Bloc 'DEPENSES' introuvable (sur la ligne titre).")

    rec_start = min(rec_positions)
    dep_start = min(dep_positions)

    # bornes :
    # recettes = de rec_start à (dep_start - 1)
    # depenses = de dep_start à fin (ou jusqu'à prochaine section si existe)
    rec_end = dep_start - 1
    dep_end = raw_df.shape[1] - 1

    if rec_end < rec_start:
        raise ValueError("Bornes incohérentes: DEPENSES avant RECETTES.")

    return {
        "row_block": r_block,
        "row_cols": r_cols,
        "recettes": (rec_start, rec_end),
        "depenses": (dep_start, dep_end)
    }

def get_columns_under_block(raw_df: pd.DataFrame, row_cols: int, start: int, end: int) -> list[dict]:
    """
    Récupère la liste des colonnes (index + libellé) sous un bloc, en excluant TOTAL.
    """
    cols = []
    for c in range(start, end + 1):
        label = norm(raw_df.iat[row_cols, c])
        if not label:
            continue
        if "TOTAL" in label:
            continue
        cols.append({"col_index": c, "label": label})
    return cols

def find_day_col(raw_df: pd.DataFrame, row_cols: int) -> int:
    """
    Trouve la colonne du jour si libellé "JOUR" existe, sinon 0.
    """
    for c in range(raw_df.shape[1]):
        if "JOUR" in norm(raw_df.iat[row_cols, c]):
            return c
    return 0

def parse_cash_sheet_bounded(raw_df: pd.DataFrame, sheet_name: str) -> dict:
    dossier = extract_dossier_number(raw_df)

    ym = extract_period_from_sheetname(sheet_name)
    if not ym:
        # fallback: si une cellule "PERIODE" existe
        r_per = find_row_contains(raw_df, "PERIODE")
        if r_per is not None:
            for c in range(raw_df.shape[1]):
                v = raw_df.iat[r_per, c]
                if isinstance(v, str) and re.search(r"\d{4}", v):
                    ym2 = extract_period_from_sheetname(v)
                    if ym2:
                        ym = ym2
                        break
    if not ym:
        return {"ok": False, "error": f"Impossible de déterminer la période pour l'onglet '{sheet_name}'."}
    year, month = ym

    try:
        blocks = detect_block_ranges(raw_df)
    except Exception as e:
        return {"ok": False, "error": f"Onglet '{sheet_name}': {e}"}

    row_block = blocks["row_block"]
    row_cols = blocks["row_cols"]
    rec_start, rec_end = blocks["recettes"]
    dep_start, dep_end = blocks["depenses"]

    recettes_cols = get_columns_under_block(raw_df, row_cols, rec_start, rec_end)
    depenses_cols = get_columns_under_block(raw_df, row_cols, dep_start, dep_end)

    # colonne jour
    day_col = find_day_col(raw_df, row_cols)

    # Parcours des lignes après row_cols (souvent row_cols + 1)
    start_data = row_cols + 1

    rows = []
    for r in range(start_data, raw_df.shape[0]):
        v_day = raw_df.iat[r, day_col]

        # arrêt si ligne "TOTAL" dans la colonne jour
        if isinstance(v_day, str) and norm(v_day) == "TOTAL":
            break

        if v_day is None or (isinstance(v_day, float) and np.isnan(v_day)):
            continue

        try:
            day_int = int(float(v_day))
        except:
            continue

        try:
            dt_ = date(year, month, day_int)
        except:
            continue

        # montants recettes et dépenses sous forme "col_label -> montant"
        rec_values = {}
        for c in recettes_cols:
            rec_values[c["label"]] = to_float(raw_df.iat[r, c["col_index"]])

        dep_values = {}
        for c in depenses_cols:
            dep_values[c["label"]] = to_float(raw_df.iat[r, c["col_index"]])

        rows.append({
            "dossier": dossier,
            "sheet": sheet_name,
            "date": dt_,
            "rec_values": rec_values,
            "dep_values": dep_values,
        })

    if not rows:
        return {"ok": False, "error": f"Onglet '{sheet_name}': aucune ligne jour exploitable trouvée."}

    return {
        "ok": True,
        "dossier": dossier,
        "year": year,
        "month": month,
        "rows": rows,
        "blocks": blocks,
        "recettes_cols": recettes_cols,
        "depenses_cols": depenses_cols
    }

# ============================================================
# Paramétrage dynamique par colonnes détectées
# ============================================================

PARAM_COLUMNS = ["bloc", "colonne", "type", "compte", "compte_contrepartie", "tva_rate", "libelle"]

def default_type_guess(bloc: str, col_label: str) -> str:
    """
    Proposition automatique (modifiable):
      - RECETTES -> vente
      - DEPENSES :
          - si contient DEPOT/REMISE/VERSEMENT -> depot
          - sinon -> charge
    """
    s = norm(col_label)
    if bloc == "RECETTES":
        return "vente"
    # DEPENSES
    if any(k in s for k in ["DEPOT", "REMISE", "VERSEMENT"]):
        return "depot"
    return "charge"

def default_account_guess(bloc: str, col_label: str) -> tuple[str, str]:
    """
    Proposition simple (à adapter):
      - vente : espèces->531, chèques->5112, cartes/cb->5111, sinon 531
      - depot : banque 512, contrepartie selon moyen (531/5112/5111)
      - charge : 606 si fournisseurs/achats sinon 623 ; contrepartie caisse 531
    Retour: (compte, compte_contrepartie)
    """
    s = norm(col_label)

    if bloc == "RECETTES":
        if any(k in s for k in ["ESPECES", "ESPECE", "CASH", "LIQUIDE"]):
            return ("531000", "")
        if any(k in s for k in ["CHEQUE", "CHEQUES", "CHQ"]):
            return ("511200", "")
        if any(k in s for k in ["CARTE", "CARTES", "CB", "BANCAIRE", "TPE"]):
            return ("511100", "")
        return ("531000", "")

    # DEPENSES
    if any(k in s for k in ["DEPOT", "REMISE", "VERSEMENT"]):
        # mouvement banque
        if any(k in s for k in ["ESPECES", "ESPECE", "CASH"]):
            return ("512000", "531000")
        if any(k in s for k in ["CHEQUE", "CHEQUES", "CHQ"]):
            return ("512000", "511200")
        if any(k in s for k in ["CARTE", "CARTES", "CB", "TPE"]):
            return ("512000", "511100")
        return ("512000", "531000")

    # charge
    if any(k in s for k in ["FOURNISSEUR", "FOURNISSEURS", "ACHAT", "ACHATS"]):
        return ("606000", "531000")
    return ("623000", "531000")

def build_default_param_from_detected(rec_cols: list[dict], dep_cols: list[dict]) -> pd.DataFrame:
    rows = []
    for c in rec_cols:
        col_label = c["label"]
        typ = default_type_guess("RECETTES", col_label)
        compte, cp = default_account_guess("RECETTES", col_label)
        rows.append({
            "bloc": "RECETTES",
            "colonne": col_label,
            "type": typ,
            "compte": compte,
            "compte_contrepartie": cp,
            "tva_rate": 0.20 if typ in ["vente", "charge"] else "",
            "libelle": f"{col_label.title()}",
        })

    for c in dep_cols:
        col_label = c["label"]
        typ = default_type_guess("DEPENSES", col_label)
        compte, cp = default_account_guess("DEPENSES", col_label)
        rows.append({
            "bloc": "DEPENSES",
            "colonne": col_label,
            "type": typ,
            "compte": compte,
            "compte_contrepartie": cp,
            "tva_rate": 0.20 if typ in ["vente", "charge"] else "",
            "libelle": f"{col_label.title()}",
        })

    df = pd.DataFrame(rows)
    return df[PARAM_COLUMNS].copy()

def param_df_to_map(param_df: pd.DataFrame) -> dict:
    """
    key = (bloc, colonne_norm)
    value = dict(type, compte, compte_contrepartie, tva_rate, libelle)
    """
    m = {}
    for _, r in param_df.iterrows():
        bloc = norm(r.get("bloc"))
        coln = norm(r.get("colonne"))
        if not bloc or not coln:
            continue
        m[(bloc, coln)] = {
            "type": str(r.get("type", "")).strip().lower(),
            "compte": str(r.get("compte", "")).strip(),
            "compte_contrepartie": str(r.get("compte_contrepartie", "")).strip(),
            "tva_rate": r.get("tva_rate", ""),
            "libelle": str(r.get("libelle", "")).strip(),
        }
    return m

# ============================================================
# Génération des écritures à partir des valeurs colonnes
# ============================================================

def generate_entries_for_day(journal: str,
                             dossier: str,
                             dt_: date,
                             piece: str,
                             rec_values: dict,
                             dep_values: dict,
                             param_map: dict,
                             global_params: dict,
                             group_monthly: bool):
    """
    Global params attendus:
      - sales_revenue_acct
      - sales_vat_acct
      - expense_vat_acct
      - default_payment_acct (crédit pour charges si pas de compte_contrepartie)
    """
    lines = []

    # ---------- RECETTES / VENTES ----------
    # On cumule TTC des colonnes typées "vente"
    sales_ttc_by_col = []
    total_sales_ttc = 0.0
    used_vat_rate = None  # si plusieurs taux, on garde celui du 1er (tu peux améliorer plus tard)

    for col_label, amount in rec_values.items():
        if amount <= 0:
            continue
        key = ("RECETTES", norm(col_label))
        p = param_map.get(key)
        if not p:
            continue
        if p["type"] != "vente":
            continue

        acct = p["compte"]
        lib = p["libelle"] or col_label
        rate = p["tva_rate"]
        try:
            rate_f = float(str(rate).replace(",", ".")) if rate != "" else 0.0
        except:
            rate_f = 0.0

        if used_vat_rate is None:
            used_vat_rate = rate_f

        lines.append(mk_line(journal, dt_, piece, acct, lib, debit=amount, credit=0, dossier=dossier, tva_rate=rate_f))
        total_sales_ttc += amount
        sales_ttc_by_col.append((col_label, amount))

    if total_sales_ttc > 0:
        rate_f = used_vat_rate if used_vat_rate is not None else 0.0
        ht, tva = vat_split(total_sales_ttc, rate_f)
        lines.append(mk_line(journal, dt_, piece, global_params["sales_revenue_acct"], "Chiffre d'affaires (HT)", debit=0, credit=ht, dossier=dossier, tva_rate=rate_f))
        if tva > 0:
            lines.append(mk_line(journal, dt_, piece, global_params["sales_vat_acct"], "TVA collectée", debit=0, credit=tva, dossier=dossier, tva_rate=rate_f))

    # ---------- DEPENSES ----------
    # Deux cas : depot (mouvement trésorerie) / charge
    for col_label, amount in dep_values.items():
        if amount <= 0:
            continue
        key = ("DEPENSES", norm(col_label))
        p = param_map.get(key)
        if not p:
            continue

        typ = p["type"]
        acct = p["compte"]
        cp = p["compte_contrepartie"] or global_params["default_payment_acct"]
        lib = p["libelle"] or col_label

        # depot : débit banque (acct=512) / crédit cp (531/511)
        if typ == "depot":
            lines.append(mk_line(journal, dt_, piece, acct, lib, debit=amount, credit=0, dossier=dossier))
            lines.append(mk_line(journal, dt_, piece, cp,   lib, debit=0, credit=amount, dossier=dossier))

        # charge : débit charge HT + débit TVA / crédit cp TTC
        elif typ == "charge":
            rate = p["tva_rate"]
            try:
                rate_f = float(str(rate).replace(",", ".")) if rate != "" else 0.0
            except:
                rate_f = 0.0

            ht, tva = vat_split(amount, rate_f)
            lines.append(mk_line(journal, dt_, piece, acct, f"{lib} (HT)", debit=ht, credit=0, dossier=dossier, tva_rate=rate_f))
            if tva > 0:
                lines.append(mk_line(journal, dt_, piece, global_params["expense_vat_acct"], "TVA déductible", debit=tva, credit=0, dossier=dossier, tva_rate=rate_f))
            lines.append(mk_line(journal, dt_, piece, cp, lib, debit=0, credit=amount, dossier=dossier, tva_rate=rate_f))

        # si quelqu'un met "vente" côté dépenses, on ignore (ou on pourrait traiter autrement)
        else:
            continue

    return lines

# ============================================================
# UI
# ============================================================

st.title("🧾 Générateur d'écritures comptables – Feuilles de caisse Excel (bornage blocs)")

with st.sidebar:
    st.header("1) Import")
    files = st.file_uploader("Dépose tes feuilles de caisse (.xlsx)", type=["xlsx"], accept_multiple_files=True)

    st.header("2) Paramétrage")
    param_file = st.file_uploader("Importer paramétrage (CSV ;)", type=["csv"])

    st.divider()
    st.markdown("**Comptes globaux**")
    sales_revenue_acct = st.text_input("Compte CA (HT)", value="706000")
    sales_vat_acct = st.text_input("TVA collectée", value="445710")
    expense_vat_acct = st.text_input("TVA déductible", value="445660")
    default_payment_acct = st.text_input("Compte paiement charges (si vide)", value="531000")

if not files:
    st.info("Importe au moins un fichier Excel pour commencer.")
    st.stop()

# ============================================================
# Liste des onglets + sélection
# ============================================================
st.subheader("1) Choix des onglets à traiter")

available = []
for f in files:
    try:
        xls = pd.ExcelFile(f)
        for sh in xls.sheet_names:
            available.append({"file": f.name, "sheet": sh})
    except Exception as e:
        st.error(f"Erreur lecture fichier {f.name}: {e}")

avail_df = pd.DataFrame(available)
if avail_df.empty:
    st.warning("Aucun onglet trouvé.")
    st.stop()

selected_rows = st.data_editor(
    avail_df.assign(selected=True),
    use_container_width=True,
    hide_index=True,
    column_config={
        "selected": st.column_config.CheckboxColumn("Traiter", default=True),
        "file": st.column_config.TextColumn("Fichier"),
        "sheet": st.column_config.TextColumn("Onglet"),
    },
    disabled=["file", "sheet"]
)

selected = selected_rows[selected_rows["selected"] == True][["file", "sheet"]].copy()
if selected.empty:
    st.warning("Aucun onglet sélectionné.")
    st.stop()

# ============================================================
# Parse des onglets sélectionnés
# ============================================================
st.subheader("2) Lecture & détection des colonnes (RECETTES / DEPENSES)")

all_days = []
meta = []
detected_columns_union = {"RECETTES": set(), "DEPENSES": set()}
debug = []

for f in files:
    try:
        xls = pd.ExcelFile(f)
        wanted_sheets = selected.loc[selected["file"] == f.name, "sheet"].tolist()
        for sh in wanted_sheets:
            raw = xls.parse(sh, header=None)
            parsed = parse_cash_sheet_bounded(raw, sh)
            if not parsed["ok"]:
                st.warning(parsed["error"])
                continue

            # union colonnes détectées
            for c in parsed["recettes_cols"]:
                detected_columns_union["RECETTES"].add(c["label"])
            for c in parsed["depenses_cols"]:
                detected_columns_union["DEPENSES"].add(c["label"])

            meta.append({
                "file": f.name,
                "sheet": sh,
                "dossier": parsed["dossier"] or "",
                "year": parsed["year"],
                "month": parsed["month"],
                "period": f"{parsed['year']}-{parsed['month']:02d}",
            })

            for d in parsed["rows"]:
                all_days.append(d)

            debug.append({
                "file": f.name,
                "sheet": sh,
                "row_block": parsed["blocks"]["row_block"],
                "row_cols": parsed["blocks"]["row_cols"],
                "rec_range": parsed["blocks"]["recettes"],
                "dep_range": parsed["blocks"]["depenses"],
                "rec_cols_count": len(parsed["recettes_cols"]),
                "dep_cols_count": len(parsed["depenses_cols"]),
            })

    except Exception as e:
        st.error(f"Erreur parsing fichier {f.name}: {e}")

if not all_days:
    st.warning("Aucune donnée exploitable sur les onglets sélectionnés.")
    st.stop()

meta_df = pd.DataFrame(meta).drop_duplicates()
days_df = pd.DataFrame(all_days)  # colonnes: dossier, sheet, date, rec_values, dep_values

with st.expander("🔍 Debug détection (si un onglet ne passe pas)"):
    st.dataframe(pd.DataFrame(debug), use_container_width=True)

# ============================================================
# Affiche colonnes détectées + propose paramétrage
# ============================================================
st.subheader("3) Colonnes détectées (hors TOTAL)")

rec_cols_list = sorted(list(detected_columns_union["RECETTES"]))
dep_cols_list = sorted(list(detected_columns_union["DEPENSES"]))

colA, colB = st.columns(2)
with colA:
    st.markdown("### RECETTES")
    st.dataframe(pd.DataFrame({"colonne": rec_cols_list}), use_container_width=True, height=250)

with colB:
    st.markdown("### DEPENSES")
    st.dataframe(pd.DataFrame({"colonne": dep_cols_list}), use_container_width=True, height=250)

# Paramétrage : soit import CSV ; soit génération auto depuis colonnes détectées
st.subheader("4) Paramétrage par colonnes")
st.caption("Le paramétrage associe chaque colonne détectée à un type (vente / depot / charge) et à des comptes.")

if "param_df" not in st.session_state:
    st.session_state.param_df = build_default_param_from_detected(
        rec_cols=[{"label": c} for c in rec_cols_list],
        dep_cols=[{"label": c} for c in dep_cols_list]
    )

if param_file is not None:
    try:
        p = pd.read_csv(param_file, sep=";")
        # assure colonnes
        for c in PARAM_COLUMNS:
            if c not in p.columns:
                p[c] = ""
        st.session_state.param_df = p[PARAM_COLUMNS].copy()
        st.success("✅ Paramétrage importé.")
    except Exception as e:
        st.error(f"❌ Import paramétrage impossible : {e}")

# bouton téléchargement param auto (pratique)
param_auto_csv = st.session_state.param_df.to_csv(index=False, sep=";").encode("utf-8")
st.download_button(
    "⬇️ Télécharger le paramétrage actuel (CSV ;)",
    data=param_auto_csv,
    file_name="parametrage_colonnes_feuille_caisse.csv",
    mime="text/csv"
)

param_df = st.data_editor(
    st.session_state.param_df,
    use_container_width=True,
    num_rows="dynamic",
    key="param_editor",
    column_config={
        "bloc": st.column_config.SelectboxColumn("bloc", options=["RECETTES", "DEPENSES"]),
        "type": st.column_config.SelectboxColumn("type", options=["vente", "depot", "charge"]),
    }
)
st.session_state.param_df = param_df

param_map = param_df_to_map(param_df)

# ============================================================
# Filtrage dossier + mois
# ============================================================
st.subheader("5) Filtrer : N° dossier + mois")

dossiers = sorted([d for d in meta_df["dossier"].unique().tolist() if str(d).strip() != ""])
if not dossiers:
    dossiers = [""]

c1, c2, c3 = st.columns([2, 2, 2])
with c1:
    dossier_sel = st.selectbox("N° dossier", options=dossiers, index=0)

periods = meta_df.loc[meta_df["dossier"] == dossier_sel, "period"].unique().tolist()
periods = sorted(periods)
if not periods:
    st.warning("Aucune période trouvée pour ce dossier.")
    st.stop()

with c2:
    period_sel = st.selectbox("Mois (YYYY-MM)", options=periods, index=0)

with c3:
    journal = st.text_input("Journal", value="CAIS")

sel_year, sel_month = map(int, period_sel.split("-"))

df_sel = days_df.copy()
df_sel = df_sel[df_sel["dossier"].fillna("") == dossier_sel]
df_sel = df_sel[(pd.to_datetime(df_sel["date"]).dt.year == sel_year) & (pd.to_datetime(df_sel["date"]).dt.month == sel_month)]
df_sel = df_sel.sort_values("date")

if df_sel.empty:
    st.warning("Aucune ligne jour pour ce filtre.")
    st.stop()

# ============================================================
# Choix pièce : jour / mois
# ============================================================
st.subheader("6) Génération des écritures")
piece_mode = st.radio("Numéro de pièce", ["Par jour (recommandé)", "Mensuel (1 pièce)"], horizontal=True)
group_monthly = (piece_mode == "Mensuel (1 pièce)")

global_params = {
    "sales_revenue_acct": sales_revenue_acct.strip() or "706000",
    "sales_vat_acct": sales_vat_acct.strip() or "445710",
    "expense_vat_acct": expense_vat_acct.strip() or "445660",
    "default_payment_acct": default_payment_acct.strip() or "531000",
}

# ============================================================
# Construction écritures
# ============================================================
lines = []

if group_monthly:
    # Agrège toutes les colonnes du mois
    dt_piece = date(sel_year, sel_month, 1)
    piece = f"{dossier_sel}-{dt_piece.strftime('%Y%m')}-MOIS"

    # agrégation dictionnaires
    rec_agg = {}
    dep_agg = {}

    for _, r in df_sel.iterrows():
        rv = r["rec_values"] or {}
        dv = r["dep_values"] or {}
        for k, v in rv.items():
            rec_agg[k] = rec_agg.get(k, 0.0) + float(v)
        for k, v in dv.items():
            dep_agg[k] = dep_agg.get(k, 0.0) + float(v)

    lines.extend(generate_entries_for_day(
        journal=journal,
        dossier=dossier_sel,
        dt_=dt_piece,
        piece=piece,
        rec_values=rec_agg,
        dep_values=dep_agg,
        param_map=param_map,
        global_params=global_params,
        group_monthly=True
    ))
else:
    for _, r in df_sel.iterrows():
        dt_ = pd.to_datetime(r["date"]).date()
        piece = f"{dossier_sel}-{dt_.strftime('%Y%m%d')}-JOUR"
        lines.extend(generate_entries_for_day(
            journal=journal,
            dossier=dossier_sel,
            dt_=dt_,
            piece=piece,
            rec_values=r["rec_values"] or {},
            dep_values=r["dep_values"] or {},
            param_map=param_map,
            global_params=global_params,
            group_monthly=False
        ))

out_df = pd.DataFrame(lines)
if not out_df.empty:
    out_df = out_df[~((out_df["Debit"] == 0) & (out_df["Credit"] == 0))].copy()

# ============================================================
# Affichage + export
# ============================================================
st.subheader("7) Contrôle & export")

# Affiche un aperçu "aplati" des données du mois pour contrôle
def flatten_values(df: pd.DataFrame, field: str, all_cols: list[str]) -> pd.DataFrame:
    tmp = df[["date", field]].copy()
    for c in all_cols:
        tmp[c] = tmp[field].apply(lambda d: float(d.get(c, 0.0)) if isinstance(d, dict) else 0.0)
    tmp = tmp.drop(columns=[field])
    return tmp

with st.expander("Aperçu RECETTES (détail colonnes)"):
    st.dataframe(flatten_values(df_sel, "rec_values", rec_cols_list), use_container_width=True, height=260)

with st.expander("Aperçu DEPENSES (détail colonnes)"):
    st.dataframe(flatten_values(df_sel, "dep_values", dep_cols_list), use_container_width=True, height=260)

st.markdown("### Écritures générées")
st.dataframe(out_df, use_container_width=True, height=450)

if out_df.empty:
    st.info("Aucune écriture générée (vérifie le paramétrage: type/compte).")
else:
    csv_bytes = out_df.to_csv(index=False, sep=";").encode("utf-8")
    st.download_button(
        "⬇️ Télécharger écritures (CSV ;)",
        data=csv_bytes,
        file_name=f"ecritures_{dossier_sel}_{period_sel}.csv",
        mime="text/csv"
    )

st.caption(
    "Notes: Les colonnes TOTAL sont ignorées automatiquement. "
    "Le paramétrage est basé sur (bloc + libellé colonne). "
    "Si un libellé change, il apparaîtra dans la liste des colonnes détectées et tu pourras l'ajouter au param."
)
