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

VAT_RATES_ALLOWED = [0.20, 0.10, 0.055, 0.021, 0.0]

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

def safe_rate(x) -> float:
    """
    Convertit en float et "snap" sur les taux autorisés (2.1 / 5.5 / 10 / 20 / 0).
    """
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0.0
    if isinstance(x, (int, float, np.number)):
        v = float(x)
    else:
        s = str(x).strip().replace("%", "").replace(",", ".")
        if s == "":
            return 0.0
        try:
            v = float(s)
        except:
            return 0.0

    if v > 1.0:
        v = v / 100.0

    # snap
    best = min(VAT_RATES_ALLOWED, key=lambda r: abs(r - v))
    if abs(best - v) > 0.003:  # tolérance
        return v
    return best

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

def find_row_contains(raw_df: pd.DataFrame, needle: str, search_rows: int = 140) -> int | None:
    n = norm(needle)
    for r in range(min(search_rows, raw_df.shape[0])):
        row_text = " ".join(norm(x) for x in raw_df.iloc[r].tolist())
        if n in row_text:
            return r
    return None

# ============================================================
# Détection blocs RECETTES / DEPENSES par bornage
#   + exclusion colonnes Total / Solde progressif / Intitulé
# ============================================================

EXCLUDE_HEADER_TOKENS = ["TOTAL", "SOLDE", "PROGRESSIF", "PROGRESSIVE", "INTITULE", "INTITULES", "INTITULEE", "INTITULEES"]

def is_excluded_column(label_norm: str) -> bool:
    if not label_norm:
        return True
    return any(tok in label_norm for tok in EXCLUDE_HEADER_TOKENS)

def detect_block_ranges(raw_df: pd.DataFrame) -> dict:
    r_block = find_row_contains(raw_df, "RECETTES")
    if r_block is None:
        raise ValueError("Impossible de trouver un titre de bloc 'RECETTES' dans l'onglet.")

    r_cols = min(r_block + 1, raw_df.shape[0] - 1)
    row_vals = [norm(x) for x in raw_df.iloc[r_block].tolist()]

    rec_positions = [i for i, v in enumerate(row_vals) if "RECETTES" in v]
    dep_positions = [i for i, v in enumerate(row_vals) if "DEPENSES" in v]
    if not dep_positions:
        dep_positions = [i for i, v in enumerate(row_vals) if "DEPENSE" in v]

    if not rec_positions:
        raise ValueError("Bloc 'RECETTES' introuvable (sur la ligne titre).")
    if not dep_positions:
        raise ValueError("Bloc 'DEPENSES' introuvable (sur la ligne titre).")

    rec_start = min(rec_positions)
    dep_start = min(dep_positions)

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
    cols = []
    for c in range(start, end + 1):
        label = norm(raw_df.iat[row_cols, c])
        if is_excluded_column(label):
            continue
        cols.append({"col_index": c, "label": label})
    return cols

def find_day_col(raw_df: pd.DataFrame, row_cols: int) -> int:
    for c in range(raw_df.shape[1]):
        if "JOUR" in norm(raw_df.iat[row_cols, c]):
            return c
    return 0

def parse_cash_sheet_bounded(raw_df: pd.DataFrame, sheet_name: str) -> dict:
    dossier = extract_dossier_number(raw_df)

    ym = extract_period_from_sheetname(sheet_name)
    if not ym:
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

    row_cols = blocks["row_cols"]
    rec_start, rec_end = blocks["recettes"]
    dep_start, dep_end = blocks["depenses"]

    recettes_cols = get_columns_under_block(raw_df, row_cols, rec_start, rec_end)
    depenses_cols = get_columns_under_block(raw_df, row_cols, dep_start, dep_end)

    day_col = find_day_col(raw_df, row_cols)
    start_data = row_cols + 1

    rows = []
    for r in range(start_data, raw_df.shape[0]):
        v_day = raw_df.iat[r, day_col]

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

        rec_values = {c["label"]: to_float(raw_df.iat[r, c["col_index"]]) for c in recettes_cols}
        dep_values = {c["label"]: to_float(raw_df.iat[r, c["col_index"]]) for c in depenses_cols}

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
#   - Si aucun compte paramétré => AUCUNE écriture
#   - TVA collectée / déductible par taux (paramétrable)
# ============================================================

PARAM_COLUMNS = ["bloc", "colonne", "type", "compte", "compte_contrepartie", "tva_rate", "libelle"]

def default_type_guess(bloc: str, col_label: str) -> str:
    s = norm(col_label)
    if bloc == "RECETTES":
        return "vente"
    if any(k in s for k in ["DEPOT", "REMISE", "VERSEMENT"]):
        return "depot"
    return "charge"

def default_account_guess(bloc: str, col_label: str) -> tuple[str, str]:
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
        if any(k in s for k in ["ESPECES", "ESPECE", "CASH"]):
            return ("512000", "531000")
        if any(k in s for k in ["CHEQUE", "CHEQUES", "CHQ"]):
            return ("512000", "511200")
        if any(k in s for k in ["CARTE", "CARTES", "CB", "TPE"]):
            return ("512000", "511100")
        return ("512000", "531000")

    if any(k in s for k in ["FOURNISSEUR", "FOURNISSEURS", "ACHAT", "ACHATS"]):
        return ("606000", "531000")
    return ("623000", "531000")

def build_default_param_from_detected(rec_cols: list[str], dep_cols: list[str]) -> pd.DataFrame:
    rows = []
    for col_label in rec_cols:
        typ = default_type_guess("RECETTES", col_label)
        compte, cp = default_account_guess("RECETTES", col_label)
        rows.append({
            "bloc": "RECETTES",
            "colonne": col_label,
            "type": typ,
            "compte": compte,
            "compte_contrepartie": cp,
            "tva_rate": 0.20 if typ in ["vente", "charge"] else "",
            "libelle": col_label.title(),
        })

    for col_label in dep_cols:
        typ = default_type_guess("DEPENSES", col_label)
        compte, cp = default_account_guess("DEPENSES", col_label)
        rows.append({
            "bloc": "DEPENSES",
            "colonne": col_label,
            "type": typ,
            "compte": compte,
            "compte_contrepartie": cp,
            "tva_rate": 0.20 if typ in ["vente", "charge"] else "",
            "libelle": col_label.title(),
        })

    df = pd.DataFrame(rows)
    return df[PARAM_COLUMNS].copy()

def param_df_to_map(param_df: pd.DataFrame) -> dict:
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
            "tva_rate": safe_rate(r.get("tva_rate", "")),
            "libelle": str(r.get("libelle", "")).strip(),
        }
    return m

def get_vat_account(vat_map: dict, kind: str, rate: float) -> str:
    """
    kind: 'collected' ou 'deductible'
    vat_map: dict {('collected', 0.2): '445710', ...}
    """
    rate = safe_rate(rate)
    return (vat_map.get((kind, rate)) or "").strip()

# ============================================================
# Génération des écritures
#   - pas d'écriture si compte manquant
#   - TVA collectée/déductible par taux
# ============================================================

def generate_entries_for_period(
    journal: str,
    dossier: str,
    dt_piece: date,
    piece: str,
    rec_values: dict,
    dep_values: dict,
    param_map: dict,
    revenue_acct: str,
    vat_accounts: dict,      # {('collected', rate): acct, ('deductible', rate): acct}
    default_payment_acct: str,
    issues: list
):
    lines = []

    revenue_acct = (revenue_acct or "").strip()
    if not revenue_acct:
        issues.append({"piece": piece, "type": "PARAM_GLOBAL", "detail": "Compte CA (HT) manquant => aucune écriture de ventes possible."})

    # -------- VENTES (RECETTES / type=vente) --------
    # 1) Débits TTC sur comptes de règlement (par colonne)
    # 2) Crédit CA HT (compte global)
    # 3) Crédit TVA collectée (compte par taux)
    sales_ttc_by_rate = {}  # rate -> ttc
    for col_label, amount in rec_values.items():
        if amount <= 0:
            continue
        key = ("RECETTES", norm(col_label))
        p = param_map.get(key)
        if not p:
            issues.append({"piece": piece, "type": "PARAM_MISSING", "detail": f"Recette '{col_label}': pas de ligne de paramétrage => ignorée."})
            continue
        if p["type"] != "vente":
            continue

        acct = (p["compte"] or "").strip()
        if not acct:
            issues.append({"piece": piece, "type": "ACCOUNT_MISSING", "detail": f"Recette '{col_label}': compte vide => aucune écriture générée pour cette colonne."})
            continue

        rate = safe_rate(p.get("tva_rate", 0.0))
        lib = p["libelle"] or col_label

        lines.append(mk_line(journal, dt_piece, piece, acct, lib, debit=amount, credit=0, dossier=dossier, tva_rate=rate))
        sales_ttc_by_rate[rate] = sales_ttc_by_rate.get(rate, 0.0) + float(amount)

    # Credits CA + TVA par taux
    for rate, ttc in sales_ttc_by_rate.items():
        if ttc <= 0:
            continue
        if not revenue_acct:
            continue

        ht, tva = vat_split(ttc, rate)
        # Crédit CA HT
        lines.append(mk_line(journal, dt_piece, piece, revenue_acct, f"Chiffre d'affaires HT ({rate*100:.1f}%)", debit=0, credit=ht, dossier=dossier, tva_rate=rate))

        # Crédit TVA collectée (par taux)
        if tva > 0:
            vat_acct = get_vat_account(vat_accounts, "collected", rate)
            if not vat_acct:
                issues.append({"piece": piece, "type": "VAT_ACCOUNT_MISSING", "detail": f"TVA collectée {rate*100:.1f}%: compte non paramétré => TVA non comptabilisée."})
            else:
                lines.append(mk_line(journal, dt_piece, piece, vat_acct, f"TVA collectée ({rate*100:.1f}%)", debit=0, credit=tva, dossier=dossier, tva_rate=rate))

    # -------- DEPENSES (DEPENSES) --------
    for col_label, amount in dep_values.items():
        if amount <= 0:
            continue
        key = ("DEPENSES", norm(col_label))
        p = param_map.get(key)
        if not p:
            issues.append({"piece": piece, "type": "PARAM_MISSING", "detail": f"Dépense '{col_label}': pas de ligne de paramétrage => ignorée."})
            continue

        typ = p["type"]
        acct = (p["compte"] or "").strip()
        cp = (p["compte_contrepartie"] or "").strip() or (default_payment_acct or "").strip()

        lib = p["libelle"] or col_label

        if typ == "depot":
            # debit banque (acct) / credit cp
            if not acct:
                issues.append({"piece": piece, "type": "ACCOUNT_MISSING", "detail": f"Dépôt '{col_label}': compte banque vide => ignoré."})
                continue
            if not cp:
                issues.append({"piece": piece, "type": "ACCOUNT_MISSING", "detail": f"Dépôt '{col_label}': compte contrepartie vide => ignoré."})
                continue
            lines.append(mk_line(journal, dt_piece, piece, acct, lib, debit=amount, credit=0, dossier=dossier))
            lines.append(mk_line(journal, dt_piece, piece, cp,   lib, debit=0, credit=amount, dossier=dossier))

        elif typ == "charge":
            # debit charge HT + debit TVA ded / credit cp TTC
            if not acct:
                issues.append({"piece": piece, "type": "ACCOUNT_MISSING", "detail": f"Charge '{col_label}': compte de charge vide => ignorée."})
                continue
            if not cp:
                issues.append({"piece": piece, "type": "ACCOUNT_MISSING", "detail": f"Charge '{col_label}': compte paiement vide => ignorée."})
                continue

            rate = safe_rate(p.get("tva_rate", 0.0))
            ht, tva = vat_split(amount, rate)

            lines.append(mk_line(journal, dt_piece, piece, acct, f"{lib} (HT)", debit=ht, credit=0, dossier=dossier, tva_rate=rate))

            if tva > 0:
                vat_acct = get_vat_account(vat_accounts, "deductible", rate)
                if not vat_acct:
                    issues.append({"piece": piece, "type": "VAT_ACCOUNT_MISSING", "detail": f"TVA déductible {rate*100:.1f}%: compte non paramétré => TVA non comptabilisée."})
                else:
                    lines.append(mk_line(journal, dt_piece, piece, vat_acct, f"TVA déductible ({rate*100:.1f}%)", debit=tva, credit=0, dossier=dossier, tva_rate=rate))

            lines.append(mk_line(journal, dt_piece, piece, cp, lib, debit=0, credit=amount, dossier=dossier, tva_rate=rate))

        else:
            # inconnu => ignore
            issues.append({"piece": piece, "type": "PARAM_INVALID", "detail": f"Dépense '{col_label}': type '{typ}' non géré => ignorée."})
            continue

    return lines

# ============================================================
# Contrôles
# ============================================================

def balance_check(out_df: pd.DataFrame) -> pd.DataFrame:
    if out_df.empty:
        return pd.DataFrame(columns=["Piece", "Debit", "Credit", "Ecart"])
    g = out_df.groupby("Piece", dropna=False).agg({"Debit": "sum", "Credit": "sum"}).reset_index()
    g["Ecart"] = (g["Debit"] - g["Credit"]).round(2)
    return g

# ============================================================
# UI
# ============================================================

st.title("🧾 Générateur d'écritures – Feuilles de caisse (bornage blocs + TVA multi-taux)")

with st.sidebar:
    st.header("1) Import")
    files = st.file_uploader("Dépose tes feuilles de caisse (.xlsx)", type=["xlsx"], accept_multiple_files=True)

    st.header("2) Paramétrage")
    param_file = st.file_uploader("Importer paramétrage (CSV ;)", type=["csv"])

    st.divider()
    st.markdown("### Comptes globaux (défauts PCG / Pennylane)")
    revenue_acct = st.text_input("Compte CA (HT)", value="706000")
    default_payment_acct = st.text_input("Compte paiement charges (si vide)", value="531000")

    st.markdown("### TVA collectée (par taux)")
    vat_col_20 = st.text_input("TVA collectée 20%", value="445710")
    vat_col_10 = st.text_input("TVA collectée 10%", value="445712")
    vat_col_55 = st.text_input("TVA collectée 5.5%", value="445713")
    vat_col_21 = st.text_input("TVA collectée 2.1%", value="445714")

    st.markdown("### TVA déductible (par taux)")
    vat_ded_20 = st.text_input("TVA déductible 20%", value="445660")
    vat_ded_10 = st.text_input("TVA déductible 10%", value="445662")
    vat_ded_55 = st.text_input("TVA déductible 5.5%", value="445663")
    vat_ded_21 = st.text_input("TVA déductible 2.1%", value="445664")

if not files:
    st.info("Importe au moins un fichier Excel pour commencer.")
    st.stop()

vat_accounts = {
    ("collected", 0.20): vat_col_20.strip(),
    ("collected", 0.10): vat_col_10.strip(),
    ("collected", 0.055): vat_col_55.strip(),
    ("collected", 0.021): vat_col_21.strip(),
    ("deductible", 0.20): vat_ded_20.strip(),
    ("deductible", 0.10): vat_ded_10.strip(),
    ("deductible", 0.055): vat_ded_55.strip(),
    ("deductible", 0.021): vat_ded_21.strip(),
}

# ============================================================
# Onglets: sélection
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
# Parsing + colonnes détectées (union)
# ============================================================
st.subheader("2) Lecture & colonnes détectées (TOTAL / SOLDE / INTITULÉ exclus)")

all_days = []
meta = []
detected_union = {"RECETTES": set(), "DEPENSES": set()}
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

            for c in parsed["recettes_cols"]:
                detected_union["RECETTES"].add(c["label"])
            for c in parsed["depenses_cols"]:
                detected_union["DEPENSES"].add(c["label"])

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
days_df = pd.DataFrame(all_days)

with st.expander("🔍 Debug détection (si un onglet ne passe pas)"):
    st.dataframe(pd.DataFrame(debug), use_container_width=True)

rec_cols_list = sorted(list(detected_union["RECETTES"]))
dep_cols_list = sorted(list(detected_union["DEPENSES"]))

cA, cB = st.columns(2)
with cA:
    st.markdown("### Colonnes RECETTES")
    st.dataframe(pd.DataFrame({"colonne": rec_cols_list}), use_container_width=True, height=230)
with cB:
    st.markdown("### Colonnes DEPENSES")
    st.dataframe(pd.DataFrame({"colonne": dep_cols_list}), use_container_width=True, height=230)

# ============================================================
# Paramétrage (auto + import)
# ============================================================
st.subheader("3) Paramétrage par colonnes (si compte vide => pas d'écriture)")

if "param_df" not in st.session_state:
    st.session_state.param_df = build_default_param_from_detected(rec_cols_list, dep_cols_list)

if param_file is not None:
    try:
        p = pd.read_csv(param_file, sep=";")
        for c in PARAM_COLUMNS:
            if c not in p.columns:
                p[c] = ""
        st.session_state.param_df = p[PARAM_COLUMNS].copy()
        st.success("✅ Paramétrage importé.")
    except Exception as e:
        st.error(f"❌ Import paramétrage impossible : {e}")

st.download_button(
    "⬇️ Télécharger paramétrage (CSV ;)",
    data=st.session_state.param_df.to_csv(index=False, sep=";").encode("utf-8"),
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
st.subheader("4) Filtrer : dossier + mois")

dossiers = sorted([d for d in meta_df["dossier"].unique().tolist() if str(d).strip() != ""])
if not dossiers:
    dossiers = [""]

x1, x2, x3 = st.columns([2, 2, 2])
with x1:
    dossier_sel = st.selectbox("N° dossier", options=dossiers, index=0)

periods = meta_df.loc[meta_df["dossier"] == dossier_sel, "period"].unique().tolist()
periods = sorted(periods)
if not periods:
    st.warning("Aucune période trouvée pour ce dossier.")
    st.stop()

with x2:
    period_sel = st.selectbox("Mois (YYYY-MM)", options=periods, index=0)

with x3:
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
# Pièces + génération
# ============================================================
st.subheader("5) Génération des écritures + contrôles")

piece_mode = st.radio("Numéro de pièce", ["Par jour (recommandé)", "Mensuel (1 pièce)"], horizontal=True)
group_monthly = (piece_mode == "Mensuel (1 pièce)")

issues = []
lines = []

if group_monthly:
    dt_piece = date(sel_year, sel_month, 1)
    piece = f"{dossier_sel}-{dt_piece.strftime('%Y%m')}-MOIS"

    rec_agg = {}
    dep_agg = {}
    for _, r in df_sel.iterrows():
        rv = r["rec_values"] or {}
        dv = r["dep_values"] or {}
        for k, v in rv.items():
            rec_agg[k] = rec_agg.get(k, 0.0) + float(v)
        for k, v in dv.items():
            dep_agg[k] = dep_agg.get(k, 0.0) + float(v)

    lines.extend(generate_entries_for_period(
        journal=journal,
        dossier=dossier_sel,
        dt_piece=dt_piece,
        piece=piece,
        rec_values=rec_agg,
        dep_values=dep_agg,
        param_map=param_map,
        revenue_acct=revenue_acct,
        vat_accounts=vat_accounts,
        default_payment_acct=default_payment_acct,
        issues=issues
    ))
else:
    for _, r in df_sel.iterrows():
        dt_piece = pd.to_datetime(r["date"]).date()
        piece = f"{dossier_sel}-{dt_piece.strftime('%Y%m%d')}-JOUR"
        lines.extend(generate_entries_for_period(
            journal=journal,
            dossier=dossier_sel,
            dt_piece=dt_piece,
            piece=piece,
            rec_values=r["rec_values"] or {},
            dep_values=r["dep_values"] or {},
            param_map=param_map,
            revenue_acct=revenue_acct,
            vat_accounts=vat_accounts,
            default_payment_acct=default_payment_acct,
            issues=issues
        ))

out_df = pd.DataFrame(lines)
if not out_df.empty:
    out_df = out_df[~((out_df["Debit"] == 0) & (out_df["Credit"] == 0))].copy()

# Contrôle équilibre débit/crédit
bal_df = balance_check(out_df)
unbalanced = bal_df[bal_df["Ecart"].abs() > 0.01].copy()

# ============================================================
# Affichages
# ============================================================

def flatten_values(df: pd.DataFrame, field: str, all_cols: list[str]) -> pd.DataFrame:
    tmp = df[["date", field]].copy()
    for c in all_cols:
        tmp[c] = tmp[field].apply(lambda d: float(d.get(c, 0.0)) if isinstance(d, dict) else 0.0)
    return tmp.drop(columns=[field])

with st.expander("Aperçu RECETTES (détail colonnes)"):
    st.dataframe(flatten_values(df_sel, "rec_values", rec_cols_list), use_container_width=True, height=260)

with st.expander("Aperçu DEPENSES (détail colonnes)"):
    st.dataframe(flatten_values(df_sel, "dep_values", dep_cols_list), use_container_width=True, height=260)

st.markdown("### Écritures générées")
st.dataframe(out_df, use_container_width=True, height=450)

st.markdown("### Contrôle équilibre Débit / Crédit par pièce")
st.dataframe(bal_df, use_container_width=True, height=220)

if not unbalanced.empty:
    st.error("Certaines pièces ne sont PAS équilibrées (Débit ≠ Crédit). Regarde le tableau ci-dessus.")
else:
    st.success("Toutes les pièces sont équilibrées (Débit = Crédit).")

if issues:
    st.warning("Contrôles / Paramétrage : certaines colonnes ont été ignorées (compte vide / param manquant / TVA non paramétrée).")
    with st.expander("Voir le détail des contrôles"):
        st.dataframe(pd.DataFrame(issues), use_container_width=True, height=260)

# ============================================================
# Export
# ============================================================
st.subheader("6) Export")

if out_df.empty:
    st.info("Aucune écriture générée (souvent: comptes vides / paramétrage manquant).")
else:
    csv_bytes = out_df.to_csv(index=False, sep=";").encode("utf-8")
    st.download_button(
        "⬇️ Télécharger écritures (CSV ;)",
        data=csv_bytes,
        file_name=f"ecritures_{dossier_sel}_{period_sel}.csv",
        mime="text/csv"
    )

st.caption(
    "Règles: colonnes TOTAL / SOLDE PROGRESSIF / INTITULÉ sont ignorées. "
    "Si une colonne a un compte vide, aucune écriture n'est créée pour cette colonne (et c'est listé en contrôle). "
    "TVA collectée/déductible est ventilée par taux (2.1 / 5.5 / 10 / 20%)."
)
