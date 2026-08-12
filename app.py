import streamlit as st
import pandas as pd
import plotly.express as px
import gspread
import time
import io

st.set_page_config(page_title="Gestion des Primes", layout="wide")

# --- CORRECTION ERGONOMIE EXTRÊME : Forcer la touche TAB ---
st.markdown("""
<style>
[data-testid="InputClearButton"],
[data-testid="stNumberInputContainer"] button,
button[aria-label="Clear input"] { 
    display: none !important; 
    visibility: hidden !important;
    width: 0 !important;
    height: 0 !important;
    pointer-events: none !important;
}
input[type="number"]::-webkit-inner-spin-button,
input[type="number"]::-webkit-outer-spin-button { 
    -webkit-appearance: none; 
    margin: 0; 
}
input[type="number"] { 
    -moz-appearance: textfield; 
}
</style>
""", unsafe_allow_html=True)

MOIS_FR = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]

def formater_label(nom, poids):
    if '%' in str(nom):
        return str(nom)
    return f"{nom} ({int(poids)}%)"

def date_premier_du_mois(mois_str):
    mois_dict = {
        "Janvier": "01", "Février": "02", "Mars": "03", "Avril": "04", 
        "Mai": "05", "Juin": "06", "Juillet": "07", "Août": "08", 
        "Septembre": "09", "Octobre": "10", "Novembre": "11", "Décembre": "12"
    }
    try:
        parts = str(mois_str).strip().split()
        if len(parts) >= 2:
            return f"01/{mois_dict.get(parts[0], '01')}/{parts[1]}"
    except:
        pass
    return mois_str

# ==========================================
# 1. CONNEXION CLOUD ET GESTION DES DONNÉES
# ==========================================
@st.cache_resource
def get_google_sheet():
    gc = gspread.service_account(filename="google_credentials.json")
    sh = gc.open_by_key("1H6hKyRx6gpHA2O_Qs04bL-lbN6NNWqOwx5L49pk-dio")
    return sh

@st.cache_data(ttl=600)
def charger_parametrage():
    try:
        sh = get_google_sheet()
        df_hierarchie = pd.DataFrame(sh.worksheet("Hierarchie").get_all_records())
        df_criteres = pd.DataFrame(sh.worksheet("Criteres").get_all_records())
        df_poids = pd.DataFrame(sh.worksheet("Poids_Globaux").get_all_records())
        
        df_hierarchie.columns = df_hierarchie.columns.str.strip()
        df_criteres.columns = df_criteres.columns.str.strip()
        df_poids.columns = df_poids.columns.str.strip()
        
        if 'Groupe' not in df_criteres.columns:
            df_criteres['Groupe'] = 'Général'
        if 'Poids Groupe' not in df_criteres.columns:
            df_criteres['Poids Groupe'] = 100.0
        if 'Poids Critere' not in df_criteres.columns and 'Poids Pourcentage' in df_criteres.columns:
            df_criteres.rename(columns={'Poids Pourcentage': 'Poids Critere'}, inplace=True)
            
        # --- LE CORRECTIF EST ICI ---
        # Nettoyage musclé des textes issus de Google Sheets pour les forcer en chiffres
        def nettoyer_pourcentages(serie):
            s = serie.astype(str).str.replace('%', '').str.replace(',', '.').str.strip()
            s = pd.to_numeric(s, errors='coerce').fillna(0)
            return s.apply(lambda x: x * 100 if 0 < x <= 1 else x)
            
        for col in ['Poids Critere', 'Poids Groupe']:
            if col in df_criteres.columns:
                df_criteres[col] = nettoyer_pourcentages(df_criteres[col])
                
        for col in ['Poids Collectif', 'Poids Individuel']:
            if col in df_poids.columns:
                df_poids[col] = nettoyer_pourcentages(df_poids[col])
        # ---------------------------
        
        for df in [df_hierarchie, df_criteres, df_poids]:
            for col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].astype(str).str.strip()
                    
        return df_hierarchie, df_criteres, df_poids
    except Exception as e:
        st.error(f"Erreur de lecture du Google Sheets (Paramétrage) : {e}")
        st.stop()

@st.cache_data(ttl=600)
def charger_historique():
    try:
        sh = get_google_sheet()
        records = sh.worksheet("Historique_Primes").get_all_records()
        if not records:
            return pd.DataFrame(columns=['Mois', 'Nom', 'Type Score', 'Groupe', 'Poids Groupe', 'Critere', 'Poids Critere', 'Note'])
        
        df = pd.DataFrame(records)
        if 'Groupe' not in df.columns:
            df['Groupe'] = 'Général'
            df['Poids Groupe'] = 100.0
            
        # Nettoyage des pourcentages de l'historique au cas où
        if 'Poids Critere' in df.columns:
            df['Poids Critere'] = df['Poids Critere'].astype(str).str.replace('%', '').str.replace(',', '.').str.strip()
            df['Poids Critere'] = pd.to_numeric(df['Poids Critere'], errors='coerce').fillna(0)
        
        # On ne garde que la dernière saisie si un manager a corrigé une prime
        df = df.drop_duplicates(subset=['Mois', 'Nom', 'Critere'], keep='last')
        return df
    except Exception as e:
        st.error(f"Erreur de lecture de l'historique : {e}")
        return pd.DataFrame()

def sauvegarder_saisie(mois, nom, liste_resultats):
    if liste_resultats:
        try:
            sh = get_google_sheet()
            worksheet_historique = sh.worksheet("Historique_Primes")
            
            lignes_a_inserer = []
            for res in liste_resultats:
                lignes_a_inserer.append([
                    res['Mois'], res['Nom'], res['Type Score'], res['Groupe'], 
                    res['Poids Groupe'], res['Critere'], res['Poids Critere'], res['Note']
                ])
                
            worksheet_historique.append_rows(lignes_a_inserer)
            st.cache_data.clear() # Force le rafraîchissement des données Cloud
        except Exception as e:
            st.error(f"Erreur lors de l'enregistrement sur le Cloud : {e}")

# ==========================================
# 2. MOTEUR DE CALCUL 
# ==========================================
def calculer_resultats_mois(mois, df_saisies, df_hierarchie, df_poids):
    saisies_mois = df_saisies[df_saisies['Mois'] == mois].copy()
    
    if saisies_mois.empty:
        scores = pd.DataFrame(columns=['Nom', 'Collectif', 'Individuel'])
    else:
        saisies_mois['Points Critere'] = pd.to_numeric(saisies_mois['Note']) * (pd.to_numeric(saisies_mois['Poids Critere']) / 100)
        scores_groupes = saisies_mois.groupby(['Nom', 'Type Score', 'Groupe', 'Poids Groupe'])['Points Critere'].sum().reset_index()
        scores_groupes['Points Finaux'] = scores_groupes['Points Critere'] * (pd.to_numeric(scores_groupes['Poids Groupe']) / 100)
        scores = scores_groupes.groupby(['Nom', 'Type Score'])['Points Finaux'].sum().unstack(fill_value=0).reset_index()
    
    if 'Collectif' not in scores.columns: scores['Collectif'] = 0.0
    if 'Individuel' not in scores.columns: scores['Individuel'] = 0.0
        
    df_res = pd.merge(df_hierarchie[['Nom', 'Type Profil', 'Manager', 'Role Hierarchique']], scores, on='Nom', how='left')
    df_res['Collectif'] = df_res['Collectif'].fillna(0.0)
    df_res['Individuel'] = df_res['Individuel'].fillna(0.0)
    df_res = pd.merge(df_res, df_poids, on='Type Profil', how='left')
    
    df_res['Global'] = pd.NA
    managers_list = df_res['Manager'].dropna().unique()
    is_base = ~df_res['Nom'].isin(managers_list)
    
    df_res.loc[is_base, 'Global'] = (df_res.loc[is_base, 'Collectif'] * (df_res.loc[is_base, 'Poids Collectif'] / 100)) + \
                                    (df_res.loc[is_base, 'Individuel'] * (df_res.loc[is_base, 'Poids Individuel'] / 100))
                                    
    for _ in range(3):
        for idx, row in df_res[df_res['Global'].isna()].iterrows():
            equipe = df_res[df_res['Manager'] == row['Nom']]
            if not equipe.empty and not equipe['Global'].isna().any():
                score_collectif_equipe = equipe['Global'].mean()
                df_res.at[idx, 'Collectif'] = score_collectif_equipe
                df_res.at[idx, 'Global'] = (score_collectif_equipe * (row['Poids Collectif'] / 100)) + \
                                           (row['Individuel'] * (row['Poids Individuel'] / 100))
                                           
    return df_res.round(2)

def calculer_tous_les_mois(df_saisies, df_hierarchie, df_poids):
    if df_saisies.empty:
        return pd.DataFrame()
    
    mois_disponibles = df_saisies['Mois'].unique()
    resultats_totaux = []
    
    for m in mois_disponibles:
        df_m = calculer_resultats_mois(m, df_saisies, df_hierarchie, df_poids)
        if not df_m.empty:
            df_m['Mois'] = m
            resultats_totaux.append(df_m)
            
    return pd.concat(resultats_totaux, ignore_index=True) if resultats_totaux else pd.DataFrame()

def afficher_tableau_annuel(df_donnees, annee, colonnes_mois):
    if df_donnees.empty:
        st.write("Aucune donnée saisie pour cette période.")
        return
        
    df_pivot = df_donnees.pivot_table(index=['Type Profil', 'Nom'], columns='Mois', values='Global', aggfunc='first').reset_index()
    
    for col in colonnes_mois:
        if col not in df_pivot.columns:
            df_pivot[col] = None
            
    df_pivot = df_pivot[['Type Profil', 'Nom'] + colonnes_mois]
    
    for col in colonnes_mois:
        df_pivot[col] = pd.to_numeric(df_pivot[col], errors='coerce')
        
    df_pivot['Moyenne Annuelle'] = df_pivot[colonnes_mois].mean(axis=1, skipna=True)
    
    format_dict = {m: "{:.1f}%" for m in colonnes_mois}
    format_dict['Moyenne Annuelle'] = "{:.1f}%"
    
    st.dataframe(df_pivot.style.format(format_dict, na_rep="-"), use_container_width=True, hide_index=True)

# ==========================================
# 3. INTERFACE VISUELLE : NAVIGATION ET FILTRES
# ==========================================
df_hierarchie, df_criteres, df_poids = charger_parametrage()
df_historique = charger_historique()

st.sidebar.header("Connexion")
df_managers = df_hierarchie[df_hierarchie['Role Hierarchique'].isin(['N+1', 'N+2'])]
utilisateur_actuel = st.sidebar.selectbox("Connecté en tant que :", df_managers['Nom'].tolist())
infos_user = df_hierarchie[df_hierarchie['Nom'] == utilisateur_actuel].iloc[0]

st.sidebar.markdown("---")
st.sidebar.header("Filtre Global (Période)")

# Application du Filtre Unique
mois_saisie_nom = st.sidebar.selectbox("Sélectionnez le mois :", MOIS_FR)
annee_saisie = st.sidebar.selectbox("Sélectionnez l'année :", [2026, 2027, 2028, 2029])
mois_saisie = f"{mois_saisie_nom} {annee_saisie}"
annee_filtre = annee_saisie

# --- CORPS DE LA PAGE ---
onglets = st.tabs(["📊 Tableau de Bord Annuel", "✍️ Saisie des Primes"])

# ------------------------------------------
# ONGLET 1 : TABLEAU DE BORD
# ------------------------------------------
with onglets[0]:
    colonnes_mois_annee = [f"{m} {annee_filtre}" for m in MOIS_FR]
    df_all_res = calculer_tous_les_mois(df_historique, df_hierarchie, df_poids)
    
    if df_all_res.empty:
        st.info("Aucune saisie effectuée sur le Cloud pour le moment.")
    else:
        df_annee = df_all_res[df_all_res['Mois'].str.contains(str(annee_filtre))]
        collab_list_graph = []
        
        if infos_user['Role Hierarchique'] == "N+2":
            equipe_n2 = df_hierarchie[(df_hierarchie['Role Hierarchique'] == 'N+1') | (df_hierarchie['Manager'] == utilisateur_actuel)]['Nom'].tolist()
            
            st.subheader("🏢 Résultats de l'encadrement (N+1) et directs")
            df_n2 = df_annee[df_annee['Nom'].isin(equipe_n2)]
            afficher_tableau_annuel(df_n2, annee_filtre, colonnes_mois_annee)
            
            collab_list_graph = [c for c in equipe_n2 if c in df_all_res['Nom'].values]
            
            # --- EXTRACTION EXCEL (RÉSERVÉ N+2) ---
            st.write("---")
            st.subheader("📥 Export Paie / RH")
            with st.expander("Générer un fichier d'export Excel", expanded=False):
                mois_export_dispos = df_historique['Mois'].unique().tolist()
                
                if not mois_export_dispos:
                    st.info("Aucune donnée à exporter.")
                else:
                    mois_export = st.selectbox("Sélectionner la période à exporter :", mois_export_dispos)
                    df_res_export = calculer_resultats_mois(mois_export, df_historique, df_hierarchie, df_poids)
                    
                    if not df_res_export.empty:
                        df_excel = pd.DataFrame({
                            'Période': date_premier_du_mois(mois_export),
                            'Entité': '',
                            'Salarié': df_res_export['Nom'],
                            'Score collectif': df_res_export['Collectif'].apply(lambda x: round(x, 2)),
                            'Score individuel': df_res_export['Individuel'].apply(lambda x: round(x, 2))
                        })
                        
                        buffer = io.BytesIO()
                        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                            df_excel.to_excel(writer, index=False, sheet_name='Export_Primes')
                        
                        st.download_button(
                            label=f"⬇️ Télécharger l'export de {mois_export}",
                            data=buffer.getvalue(),
                            file_name=f"Export_Primes_{mois_export.replace(' ', '_')}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
            
        elif infos_user['Role Hierarchique'] == "N+1":
            equipe = df_hierarchie[df_hierarchie['Manager'] == utilisateur_actuel]['Nom'].tolist()
            
            st.subheader("👥 Résultats de mon équipe")
            df_equipe = df_annee[df_annee['Nom'].isin(equipe)]
            afficher_tableau_annuel(df_equipe, annee_filtre, colonnes_mois_annee)
            
            collab_list_graph = [c for c in equipe if c in df_all_res['Nom'].values]

        if len(collab_list_graph) > 0:
            st.write("---")
            st.subheader("📈 Analyse Détaillée par Collaborateur")
            collab_graph = st.selectbox("Sélectionner un collaborateur :", collab_list_graph)
            
            if collab_graph:
                col1, col2 = st.columns(2)
                with col1:
                    df_graph_global = df_annee[df_annee['Nom'] == collab_graph]
                    if not df_graph_global.empty:
                        fig1 = px.line(df_graph_global, x='Mois', y='Global', title=f"Évolution du Score Global", markers=True, text='Global', category_orders={'Mois': colonnes_mois_annee})
                        fig1.update_traces(textposition="top center", texttemplate='%{text:.1f}%')
                        fig1.update_yaxes(range=[0, max(100, df_graph_global['Global'].max() + 5)])
                        st.plotly_chart(fig1, use_container_width=True)
                    
                with col2:
                    df_hist_collab = df_historique[(df_historique['Nom'] == collab_graph) & (df_historique['Mois'].str.contains(str(annee_filtre)))].copy()
                    if not df_hist_collab.empty:
                        df_hist_collab['Score Partiel'] = pd.to_numeric(df_hist_collab['Note']) * (pd.to_numeric(df_hist_collab['Poids Critere']) / 100)
                        df_groupes_graph = df_hist_collab.groupby(['Mois', 'Groupe'])['Score Partiel'].sum().reset_index()
                        
                        fig2 = px.line(df_groupes_graph, x='Mois', y='Score Partiel', color='Groupe', markers=True, title=f"Évolution des Sous-groupes", category_orders={'Mois': colonnes_mois_annee})
                        fig2.update_yaxes(range=[0, max(100, df_groupes_graph['Score Partiel'].max() + 5)])
                        st.plotly_chart(fig2, use_container_width=True)

# ------------------------------------------
# ONGLET 2 : SAISIE DES PRIMES
# ------------------------------------------
with onglets[1]:
    st.info(f"Saisie en cours pour la période : **{mois_saisie}**")
    
    if infos_user['Role Hierarchique'] == "N+2":
        choix_equipe = df_hierarchie[(df_hierarchie['Role Hierarchique'] == 'N+1') | (df_hierarchie['Manager'] == utilisateur_actuel)]['Nom'].tolist()
    else:
        choix_equipe = df_hierarchie[df_hierarchie['Manager'] == utilisateur_actuel]['Nom'].tolist()
        
    collab_choisi = st.selectbox("Évaluer le collaborateur :", choix_equipe)
    
    if collab_choisi:
        profil_collab = df_hierarchie[df_hierarchie['Nom'] == collab_choisi]['Type Profil'].iloc[0]
        criteres_collab = df_criteres[df_criteres['Type Profil'] == profil_collab]
        
        df_deja_saisi = df_historique[(df_historique['Mois'] == mois_saisie) & (df_historique['Nom'] == collab_choisi)]
        df_res_mois = calculer_resultats_mois(mois_saisie, df_historique, df_hierarchie, df_poids)
        
        score_collectif_actuel = 0.0
        score_individuel_actuel = 0.0
        score_global_actuel = 0.0
        
        if not df_res_mois.empty:
            res_collab = df_res_mois[df_res_mois['Nom'] == collab_choisi]
            if not res_collab.empty:
                score_collectif_actuel = res_collab['Collectif'].iloc[0] if not pd.isna(res_collab['Collectif'].iloc[0]) else 0.0
                score_individuel_actuel = res_collab['Individuel'].iloc[0] if not pd.isna(res_collab['Individuel'].iloc[0]) else 0.0
                score_global_actuel = res_collab['Global'].iloc[0] if not pd.isna(res_collab['Global'].iloc[0]) else 0.0
        
        st.markdown(f"""
        <div style="background: linear-gradient(135deg, #1f77b4 0%, #00d2ff 100%); padding: 15px; border-radius: 10px; color: white; text-align: center; margin-bottom: 20px; box-shadow: 0px 4px 6px rgba(0,0,0,0.1);">
            <h2 style="margin: 0; color: white; font-size: 26px; font-weight: bold;">
                🏆 Score Global Actuel : {score_global_actuel:.1f}%
            </h2>
        </div>
        """, unsafe_allow_html=True)
        
        def obtenir_valeur_par_defaut(critere):
            if not df_deja_saisi.empty:
                ligne = df_deja_saisi[df_deja_saisi['Critere'] == critere]
                if not ligne.empty:
                    return int(ligne['Note'].iloc[0])
            return None
        
        st.write("---")
        tout_developper = st.toggle("📂 Développer tous les groupes pour une saisie rapide (Navigation TAB)")
        
        with st.form("form_saisie"):
            resultats = []
            
            for type_score in ['Collectif', 'Individuel']:
                poids_serie = df_poids[df_poids['Type Profil'] == profil_collab][f"Poids {type_score}"]
                if poids_serie.empty: continue
                poids_global = int(poids_serie.iloc[0])
                if poids_global == 0: continue
                
                criteres_type = criteres_collab[criteres_collab['Type Score'] == type_score]
                score_groupe_affiche = score_collectif_actuel if type_score == 'Collectif' else score_individuel_actuel
                
                st.markdown(f"### 🎯 Score {type_score} ({poids_global}%) — Actuel : {score_groupe_affiche:.1f}%")
                
                if criteres_type.empty:
                    st.info(f"💡 Ce score de {poids_global}% est calculé automatiquement à partir de la moyenne des résultats de l'équipe.")
                else:
                    groupes = criteres_type['Groupe'].unique()
                    for grp in groupes:
                        criteres_groupe = criteres_type[criteres_type['Groupe'] == grp]
                        poids_groupe = criteres_groupe['Poids Groupe'].iloc[0]
                        label_groupe = formater_label(grp, poids_groupe)
                        
                        score_actuel_ss_grp = 0.0
                        if not df_deja_saisi.empty:
                            saisies_grp = df_deja_saisi[df_deja_saisi['Groupe'] == grp]
                            if not saisies_grp.empty:
                                score_actuel_ss_grp = (pd.to_numeric(saisies_grp['Note']) * (pd.to_numeric(saisies_grp['Poids Critere']) / 100)).sum()
                        
                        titre_expander = f"📦 {label_groupe} - Score actuel : {score_actuel_ss_grp:.1f}%" if not df_deja_saisi.empty else f"📦 {label_groupe}"
                        
                        with st.expander(titre_expander, expanded=tout_developper):
                            for _, row in criteres_groupe.iterrows():
                                val_defaut = obtenir_valeur_par_defaut(row['Critere'])
                                cle_unique = f"{type_score}_{mois_saisie}_{collab_choisi}_{row['Critere']}"
                                label_critere = formater_label(row['Critere'], row['Poids Critere'])
                                
                                val = st.number_input(f"↳ {label_critere}", min_value=0, max_value=100, value=val_defaut, placeholder="Saisir un score (0 à 100)", key=cle_unique)
                                
                                resultats.append({
                                    'Mois': mois_saisie, 'Nom': collab_choisi, 'Type Score': row['Type Score'], 
                                    'Groupe': row['Groupe'], 'Poids Groupe': row['Poids Groupe'], 
                                    'Critere': row['Critere'], 'Poids Critere': row['Poids Critere'], 'Note': val
                                })
            
            st.write("---")
            cle_checkbox = f"confirm_{mois_saisie}_{collab_choisi}"
            confirmation = st.checkbox("Je confirme être sûr de vouloir valider ces saisies vers Google Sheets.", key=cle_checkbox)
            
            if st.form_submit_button("Valider la saisie vers le Cloud"):
                manager_officiel = df_hierarchie[df_hierarchie['Nom'] == collab_choisi]['Manager'].iloc[0]
                
                if infos_user['Role Hierarchique'] != "N+2" and manager_officiel != utilisateur_actuel:
                    st.error("🛑 Vous n'êtes pas autorisé à modifier les données de ce collaborateur.")
                elif len(resultats) > 0 and None in [res['Note'] for res in resultats]:
                    st.error("⚠️ Veuillez remplir tous les critères (les cases vides ne sont pas acceptées).")
                elif not confirmation:
                    st.warning("⚠️ Veuillez cocher la case de confirmation pour pouvoir sauvegarder.")
                else:
                    sauvegarder_saisie(mois_saisie, collab_choisi, resultats)
                    st.success(f"✅ Saisie enregistrée en direct sur le Cloud pour {collab_choisi} !")
                    time.sleep(1.5)
                    st.rerun()