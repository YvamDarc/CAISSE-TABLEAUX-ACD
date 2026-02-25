import streamlit as st
import pandas as pd
import numpy as np
import re
from datetime import date

st.set_page_config(page_title="Générateur d'écritures - Feuilles de caisse", layout="wide")

# -----------------------------
# Helpers
# -----------------------------
FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12
}

def norm_txt(x: str) -> str:
    if x is None:
        return ""
    return str(x).strip()

def to_float(x) -> float:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0.0
    if isinstance(x, (int, float, np.number)):
        return float(x)
    s = str(x).strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except:
        return 0.0

def extract_dossier_number(raw_df: pd.DataFrame) -> str | None:
    # cherche un bloc type: "1024457 SARL ...."
    for r in range(min(30, raw_df.shape[0])):
        for c in range(min(15, raw_df.shape[1])):
            v = raw_df.iat[r, c]
            if isinstance(v, str):
                m = re.search(r"\b(\d{6,8})\b", v)
                if m:
                    return m.group(1)
    return None

def extract_period_from_sheetname(sheet_name: str) -> tuple[int, int] | None:
    # ex: "Avril 2024"
    s = sheet_name.strip().lower()
    parts = s.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        year = int(parts[-1])
        month_name = " ".join(parts[:-1]).strip()
        month = FRENCH_MONTHS.get(month_name)
        if month:
            return (year, month)
    return None

def find_row_with_value(raw_df: pd.DataFrame, needle: str) -> int | None:
    needle_low = needle.lower()
    for r in range(raw_df.shape[0]):
        row_vals = raw_df.iloc[r].astype(str).str.lower().tolist()
        if any(needle_low in v for v in row_vals):
            return r
    return None

def parse_cash_sheet(raw_df: pd.DataFrame, sheet_name: str) -> dict:
    """
    Attend un format proche de ton fichier :
    - ligne avec "PREST.ESPECES", "PREST.CHEQUES", "PREST.CARTES", "DIVERS" + "Total" + partie DEPENSES
    - ligne suivante avec "Jour"
    - puis lignes jour 1..31 + une ligne "Total"
    """
    dossier = extract_dossier_number(raw_df)

    ym = extract_period_from_sheetname(sheet_name)
    if not ym:
        # fallback: tente de lire une cellule "Période:" + "Avril 2024"
        r_per = find_row_with_value(raw_df, "période")
        if r_per is not None:
            # cherche à droite
            for c in range(raw_df.shape[1]):
                v = raw_df.iat[r_per, c]
                if isinstance(v, str) and re.search(r"\d{4}", v):
                    ym = extract_period_from_sheetname(v)
                    break
    if not ym:
        return {"ok": False, "error": f"Impossible de déterminer la période pour l'onglet '{sheet_name}'."}

    year, month = ym

    # Repère la ligne qui contient "Jour"
    r_jour = find_row_with_value(raw_df, "jour")
    if r_jour is None:
        return {"ok": False, "error": f"Onglet '{sheet_name}': ligne 'Jour' introuvable."}

    # La ligne d'entête des colonnes est souvent juste au-dessus
    r_hdr = max(0, r_jour - 1)
    headers = [norm_txt(x) for x in raw_df.iloc[r_hdr].tolist()]

    # map colonnes par mots-clés
    def col_idx_contains(keyword: str) -> int | None:
        k = keyword.lower()
        for i, h in enumerate(headers):
            if k in h.lower():
                return i
        return None

    c_day = 0  # dans ton fichier, Jour est colonne 0
    col_map = {
        "prest_especes": col_idx_contains("prest.especes"),
        "prest_cheques": col_idx_contains("prest.cheques"),
        "prest_cartes": col_idx_contains("prest.cartes"),
        "prest_divers": col_idx_contains("divers"),  # attention: aussi "divers" côté dépenses parfois
        "dep_especes": col_idx_contains("depot especes"),
        "dep_cheques": col_idx_contains("depot cheques"),
        "dep_cartes": col_idx_contains("depot cartes"),
        "dep_fournisseurs": col_idx_contains("fournisseurs"),
        "dep_divers": None,
    }

    # si "divers" apparaît 2 fois (recettes + dépenses), on choisit le 1er pour recettes.
    # pour dépenses divers, on cherche "depenses" ligne d'entête et une colonne "divers" à droite
    # Heuristique simple : si on a "fournisseurs" et un autre "divers" après, c'est dep_divers.
    divers_positions = [i for i, h in enumerate(headers) if "divers" in h.lower()]
    if len(divers_positions) >= 2:
        col_map["prest_divers"] = divers_positions[0]
        col_map["dep_divers"] = divers_positions[-1]
    elif len(divers_positions) == 1:
        col_map["prest_divers"] = divers_positions[0]

    # Lecture des lignes jours
    rows = []
    for r in range(r_jour + 1, raw_df.shape[0]):
        d = raw_df.iat[r, c_day]
        if isinstance(d, str) and d.strip().lower() == "total":
            break
        if d is None or (isinstance(d, float) and np.isnan(d)):
            continue

        try:
            day_int = int(float(d))
        except:
            continue

        # date
        try:
            dt_ = date(year, month, day_int)
        except:
            continue

        def get_amount(key: str) -> float:
            c = col_map.get(key)
            if c is None:
                return 0.0
            return to_float(raw_df.iat[r, c])

        rec_especes = get_amount("prest_especes")
        rec_cheques = get_amount("prest_cheques")
        rec_cartes = get_amount("prest_cartes")
        rec_divers = get_amount("prest_divers")

        dep_especes = get_amount("dep_especes")
        dep_cheques = get_amount("dep_cheques")
        dep_cartes = get_amount("dep_cartes")
        dep_four = get_amount("dep_fournisseurs")
        dep_div = get_amount("dep_divers")

        rows.append({
            "dossier": dossier,
            "sheet": sheet_name,
            "date": dt_,
            "rec_especes": rec_especes,
            "rec_cheques": rec_cheques,
            "rec_cartes": rec_cartes,
            "rec_divers": rec_divers,
            "dep_especes": dep_especes,
            "dep_cheques": dep_cheques,
            "dep_cartes": dep_cartes,
            "dep_fournisseurs": dep_four,
            "dep_divers": dep_div,
        })

    return {"ok": True, "dossier": dossier, "year": year, "month": month, "rows": rows}

def vat_split(ttc: float, rate: float) -> tuple[float, float]:
    """
    Retourne (HT, TVA) à partir de TTC et taux.
    """
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

# -----------------------------
# Paramétrage (template + import)
# -----------------------------
DEFAULT_PARAMS = pd.DataFrame([
    # Ventes (recettes)
    {"flux": "rec_especes", "type": "vente_mode_paiement", "compte": "531000", "libelle": "Ventes espèces", "tva_rate": 0.20},
    {"flux": "rec_cheques", "type": "vente_mode_paiement", "compte": "511200", "libelle": "Ventes chèques", "tva_rate": 0.20},
    {"flux": "rec_cartes", "type": "vente_mode_paiement", "compte": "511100", "libelle": "Ventes CB", "tva_rate": 0.20},
    {"flux": "rec_divers", "type": "vente_mode_paiement", "compte": "531000", "libelle": "Ventes divers", "tva_rate": 0.20},

    # Comptes communs ventes
    {"flux": "sales_revenue", "type": "compte_commun", "compte": "706000", "libelle": "Chiffre d'affaires", "tva_rate": ""},
    {"flux": "sales_vat", "type": "compte_commun", "compte": "445710", "libelle": "TVA collectée", "tva_rate": ""},

    # Dépôts banque
    {"flux": "dep_especes", "type": "depot_banque", "compte": "512000", "libelle": "Dépôt espèces", "tva_rate": ""},
    {"flux": "dep_cheques", "type": "depot_banque", "compte": "512000", "libelle": "Dépôt chèques", "tva_rate": ""},
    {"flux": "dep_cartes", "type": "depot_banque", "compte": "512000", "libelle": "Remise CB", "tva_rate": ""},

    # Comptes source dépôts (ce qu’on crédite)
    {"flux": "src_dep_especes", "type": "compte_commun", "compte": "531000", "libelle": "Caisse espèces", "tva_rate": ""},
    {"flux": "src_dep_cheques", "type": "compte_commun", "compte": "511200", "libelle": "Chèques à encaisser", "tva_rate": ""},
    {"flux": "src_dep_cartes", "type": "compte_commun", "compte": "511100", "libelle": "CB à encaisser", "tva_rate": ""},

    # Dépenses (si présentes dans l’onglet)
    {"flux": "dep_fournisseurs", "type": "depense", "compte": "606000", "libelle": "Achats / fournisseurs", "tva_rate": 0.20},
    {"flux": "dep_divers", "type": "depense", "compte": "623000", "libelle": "Dépenses diverses", "tva_rate": 0.20},
    {"flux": "expense_vat", "type": "compte_commun", "compte": "445660", "libelle": "TVA déductible", "tva_rate": ""},
    {"flux": "expense_pay_account", "type": "compte_commun", "compte": "531000", "libelle": "Paiement par caisse", "tva_rate": ""},
])

PARAM_COLUMNS = ["flux", "type", "compte", "libelle", "tva_rate"]

def params_to_dict(params_df: pd.DataFrame) -> dict:
    d = {}
    for _, r in params_df.iterrows():
        d[str(r["flux"]).strip()] = {
            "type": r.get("type", ""),
            "compte": str(r.get("compte", "")).strip(),
            "libelle": str(r.get("libelle", "")).strip(),
            "tva_rate": r.get("tva_rate", ""),
        }
    return d

# -----------------------------
# UI
# -----------------------------
st.title("🧾 Générateur d'écritures comptables – Feuilles de caisse Excel")

with st.sidebar:
    st.header("1) Import")
    files = st.file_uploader("Dépose tes feuilles de caisse (.xlsx)", type=["xlsx"], accept_multiple_files=True)

    st.header("2) Paramétrage")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("⬇️ Télécharger template paramétrage (CSV)"):
            tmp = DEFAULT_PARAMS[PARAM_COLUMNS].copy()
            st.download_button(
                "Télécharger",
                data=tmp.to_csv(index=False, sep=";").encode("utf-8"),
                file_name="parametrage_feuille_caisse.csv",
                mime="text/csv"
            )
    with c2:
        param_file = st.file_uploader("Importer un paramétrage (CSV ; séparateur ;) ", type=["csv"])

# Charge / initialise paramètres
if "params_df" not in st.session_state:
    st.session_state.params_df = DEFAULT_PARAMS[PARAM_COLUMNS].copy()

if param_file is not None:
    try:
        p = pd.read_csv(param_file, sep=";")
        p = p[PARAM_COLUMNS].copy()
        st.session_state.params_df = p
        st.success("Paramétrage importé.")
    except Exception as e:
        st.error(f"Import paramétrage impossible : {e}")

st.subheader("2) Paramétrage des comptes et TVA")
st.caption("Tu peux modifier directement ici. Les flux non utilisés dans un mois seront ignorés.")
params_df = st.data_editor(
    st.session_state.params_df,
    use_container_width=True,
    num_rows="dynamic",
    key="params_editor"
)
st.session_state.params_df = params_df

# -----------------------------
# Parse Excel(s)
# -----------------------------
all_rows = []
meta = []

if files:
    for f in files:
        try:
            xls = pd.ExcelFile(f)
            for sh in xls.sheet_names:
                raw = xls.parse(sh, header=None)
                parsed = parse_cash_sheet(raw, sh)
                if parsed["ok"]:
                    meta.append({
                        "file": f.name,
                        "sheet": sh,
                        "dossier": parsed["dossier"] or "",
                        "year": parsed["year"],
                        "month": parsed["month"],
                        "period": f"{parsed['year']}-{parsed['month']:02d}",
                    })
                    all_rows.extend(parsed["rows"])
                else:
                    st.warning(parsed["error"])
        except Exception as e:
            st.error(f"Erreur lecture fichier {f.name}: {e}")

if not all_rows:
    st.info("Importe au moins un fichier Excel pour commencer.")
    st.stop()

meta_df = pd.DataFrame(meta).drop_duplicates()
data_df = pd.DataFrame(all_rows)

# Sélecteurs
st.subheader("3) Sélection dossier + mois")
colA, colB, colC = st.columns([2, 2, 2])

dossiers = sorted([d for d in meta_df["dossier"].unique().tolist() if str(d).strip() != ""])
if not dossiers:
    dossiers = [""]

with colA:
    dossier_sel = st.selectbox("N° dossier", options=dossiers, index=0)

periods = meta_df.loc[meta_df["dossier"] == dossier_sel, "period"].unique().tolist()
periods = sorted(periods)
with colB:
    period_sel = st.selectbox("Mois (YYYY-MM)", options=periods, index=0)

with colC:
    journal = st.text_input("Journal", value="CAIS")

# Filtrage data
sel_year, sel_month = map(int, period_sel.split("-"))
df_sel = data_df.copy()
df_sel = df_sel[df_sel["dossier"].fillna("") == dossier_sel]
df_sel = df_sel[(pd.to_datetime(df_sel["date"]).dt.year == sel_year) & (pd.to_datetime(df_sel["date"]).dt.month == sel_month)]
df_sel = df_sel.sort_values("date")

# -----------------------------
# Génération écritures
# -----------------------------
st.subheader("4) Génération des écritures")

params = params_to_dict(params_df)

def get_param(flux: str, field: str, default=None):
    return params.get(flux, {}).get(field, default)

sales_revenue_acct = get_param("sales_revenue", "compte", "706000")
sales_vat_acct = get_param("sales_vat", "compte", "445710")

bank_acct = "512000"  # par défaut, mais pris depuis les lignes dep_* (compte = banque)
src_dep_especes = get_param("src_dep_especes", "compte", "531000")
src_dep_cheques = get_param("src_dep_cheques", "compte", "511200")
src_dep_cartes = get_param("src_dep_cartes", "compte", "511100")

expense_vat_acct = get_param("expense_vat", "compte", "445660")
expense_pay_acct = get_param("expense_pay_account", "compte", "531000")

piece_mode = st.radio("Numéro de pièce", ["Par jour (recommandé)", "Mensuel (1 pièce)"], horizontal=True)
group_monthly = (piece_mode == "Mensuel (1 pièce)")

lines = []

def add_sales_entry(dt_, dossier, amounts: dict):
    # amounts: rec_especes, rec_cheques, rec_cartes, rec_divers
    total_ttc = sum(amounts.values())
    if total_ttc <= 0:
        return

    # TVA : on suppose un taux unique (celui de rec_especes s’il existe, sinon 20%)
    rate = get_param("rec_especes", "tva_rate", 0.20)
    try:
        rate = float(rate)
    except:
        rate = 0.20

    ht, tva = vat_split(total_ttc, rate)

    piece = f"{dossier}-{dt_.strftime('%Y%m%d')}-VENTE" if not group_monthly else f"{dossier}-{dt_.strftime('%Y%m')}-VENTE"
    lib_base = "Ventes caisse"

    # Débits : comptes par mode de paiement
    for flux, amt in amounts.items():
        if amt <= 0:
            continue
        acct = get_param(flux, "compte", "")
        lib = get_param(flux, "libelle", lib_base)
        if not acct:
            continue
        lines.append(mk_line(journal, dt_, piece, acct, lib, debit=amt, credit=0, dossier=dossier, tva_rate=rate))

    # Crédit CA HT
    lines.append(mk_line(journal, dt_, piece, sales_revenue_acct, "Chiffre d'affaires (HT)", debit=0, credit=ht, dossier=dossier, tva_rate=rate))

    # Crédit TVA collectée
    if tva > 0:
        lines.append(mk_line(journal, dt_, piece, sales_vat_acct, "TVA collectée", debit=0, credit=tva, dossier=dossier, tva_rate=rate))

def add_deposit_entry(dt_, dossier, dep_flux: str, amount: float):
    if amount <= 0:
        return
    piece = f"{dossier}-{dt_.strftime('%Y%m%d')}-DEP" if not group_monthly else f"{dossier}-{dt_.strftime('%Y%m')}-DEP"
    bank = get_param(dep_flux, "compte", "512000")
    lib = get_param(dep_flux, "libelle", "Dépôt banque")

    if dep_flux == "dep_especes":
        src = src_dep_especes
    elif dep_flux == "dep_cheques":
        src = src_dep_cheques
    else:
        src = src_dep_cartes

    # Débit banque / Crédit compte d'attente/caisse
    lines.append(mk_line(journal, dt_, piece, bank, lib, debit=amount, credit=0, dossier=dossier))
    lines.append(mk_line(journal, dt_, piece, src, lib, debit=0, credit=amount, dossier=dossier))

def add_expense_entry(dt_, dossier, exp_flux: str, amount_ttc: float):
    if amount_ttc <= 0:
        return
    piece = f"{dossier}-{dt_.strftime('%Y%m%d')}-DEPENSE" if not group_monthly else f"{dossier}-{dt_.strftime('%Y%m')}-DEPENSE"
    acct_charge = get_param(exp_flux, "compte", "")
    lib = get_param(exp_flux, "libelle", "Dépense")
    rate = get_param(exp_flux, "tva_rate", 0.20)
    try:
        rate = float(rate)
    except:
        rate = 0.0

    if not acct_charge:
        return

    ht, tva = vat_split(amount_ttc, rate)

    # Débit charge HT
    lines.append(mk_line(journal, dt_, piece, acct_charge, f"{lib} (HT)", debit=ht, credit=0, dossier=dossier, tva_rate=rate))

    # Débit TVA déductible
    if tva > 0:
        lines.append(mk_line(journal, dt_, piece, expense_vat_acct, "TVA déductible", debit=tva, credit=0, dossier=dossier, tva_rate=rate))

    # Crédit compte de paiement
    lines.append(mk_line(journal, dt_, piece, expense_pay_acct, lib, debit=0, credit=amount_ttc, dossier=dossier, tva_rate=rate))

# Génération : soit par jour, soit agrégée mois
if group_monthly:
    # Agrège sur tout le mois
    dt_ = date(sel_year, sel_month, 1)

    sales_amounts = {
        "rec_especes": float(df_sel["rec_especes"].sum()),
        "rec_cheques": float(df_sel["rec_cheques"].sum()),
        "rec_cartes": float(df_sel["rec_cartes"].sum()),
        "rec_divers": float(df_sel["rec_divers"].sum()),
    }
    add_sales_entry(dt_, dossier_sel, sales_amounts)

    add_deposit_entry(dt_, dossier_sel, "dep_especes", float(df_sel["dep_especes"].sum()))
    add_deposit_entry(dt_, dossier_sel, "dep_cheques", float(df_sel["dep_cheques"].sum()))
    add_deposit_entry(dt_, dossier_sel, "dep_cartes", float(df_sel["dep_cartes"].sum()))

    # dépenses si présentes
    add_expense_entry(dt_, dossier_sel, "dep_fournisseurs", float(df_sel["dep_fournisseurs"].sum()))
    add_expense_entry(dt_, dossier_sel, "dep_divers", float(df_sel["dep_divers"].sum()))

else:
    for _, r in df_sel.iterrows():
        dt_ = pd.to_datetime(r["date"]).date()

        sales_amounts = {
            "rec_especes": float(r.get("rec_especes", 0.0)),
            "rec_cheques": float(r.get("rec_cheques", 0.0)),
            "rec_cartes": float(r.get("rec_cartes", 0.0)),
            "rec_divers": float(r.get("rec_divers", 0.0)),
        }
        add_sales_entry(dt_, dossier_sel, sales_amounts)

        add_deposit_entry(dt_, dossier_sel, "dep_especes", float(r.get("dep_especes", 0.0)))
        add_deposit_entry(dt_, dossier_sel, "dep_cheques", float(r.get("dep_cheques", 0.0)))
        add_deposit_entry(dt_, dossier_sel, "dep_cartes", float(r.get("dep_cartes", 0.0)))

        add_expense_entry(dt_, dossier_sel, "dep_fournisseurs", float(r.get("dep_fournisseurs", 0.0)))
        add_expense_entry(dt_, dossier_sel, "dep_divers", float(r.get("dep_divers", 0.0)))

out_df = pd.DataFrame(lines)

# Nettoyage : supprime lignes 0/0
if not out_df.empty:
    out_df = out_df[~((out_df["Debit"] == 0) & (out_df["Credit"] == 0))].copy()

st.write("Aperçu des données brutes (mois sélectionné) :")
st.dataframe(df_sel, use_container_width=True, height=240)

st.write("Écritures générées :")
st.dataframe(out_df, use_container_width=True, height=420)

# Export
st.subheader("5) Export")
if out_df.empty:
    st.info("Aucune écriture générée avec ces filtres / paramètres.")
else:
    csv_bytes = out_df.to_csv(index=False, sep=";").encode("utf-8")
    st.download_button(
        "⬇️ Télécharger écritures (CSV ; ;) ",
        data=csv_bytes,
        file_name=f"ecritures_{dossier_sel}_{period_sel}.csv",
        mime="text/csv"
    )

st.caption("💡 Astuce : si tu veux un format Pennylane/FEC spécifique, dis-moi les colonnes attendues et je t’adapte l’export.")
