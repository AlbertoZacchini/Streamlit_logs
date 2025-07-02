import streamlit as st
import sqlite3
import pandas as pd
import tempfile
import altair as alt
import numpy as np
import os
from zipfile import ZipFile
from Crypto.Cipher import AES 
from openai import OpenAI
import altair as alt
from fpdf import FPDF
import zipfile


# Carica i segreti dal file secrets.toml (solo in ambiente locale)
AES_KEY = st.secrets["AES_KEY"].encode()
AES_IV = st.secrets["AES_IV"].encode()
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

# Assicurati che la chiave e l'IV abbiano la lunghezza corretta
assert len(AES_KEY) == 16, "La chiave AES deve essere di 128 bit (16 byte)"
assert len(AES_IV) == 16, "L'IV AES deve essere di 128 bit (16 byte)"

# Carica il file Excel per associare gli errori alla descrizione
file_path = 'laser_alarms.xlsx'
excel_data = pd.ExcelFile(file_path)
df_alarms = excel_data.parse('Sheet1')

# Creiamo un dizionario che mappa l'errore (numero) alla descrizione
error_description_map = dict(zip(df_alarms['Index'], df_alarms['Error Message']))

connections = []  # Inizializza la variabile connections come lista vuota

def main():
    st.set_page_config(page_title="ASA Log Analyzer", layout="wide")
    st.sidebar.title("Navigazione")

    page = st.sidebar.radio("Vai a:", ["Analisi Log", "Form Diagnostico"])

    if page == "Analisi Log":
        main_log_analysis()  # era il tuo vecchio `main()`
    elif page == "Form Diagnostico":
        show_diagnostic_form()

# Chiusura connessioni se ce ne sono altre
for conn in connections:
    conn.close()


def main_log_analysis():
    st.title("ASA Log File Analyzer")

    # Sidebar per il caricamento del file
    with st.sidebar:
        st.header("Selezione File")
        uploaded_file = st.file_uploader("Carica un file compresso (ZIP)", type=["zip"])
    
    if uploaded_file:
        # Mostra nome del file sotto il titolo
        st.subheader(f"📂 File Caricato: `{uploaded_file.name}`")

    if uploaded_file:
        try:
            combined_df = decrypt_and_parse(uploaded_file.getvalue())

            combined_df['detail'] = combined_df['detail'].astype(str)

            combined_df['action_detail'] = combined_df.apply(
                lambda x: (
                    f"{x['action']} - {x['detail']} - {error_description_map.get(f'E{int(x['detail']):02d}', 'Descrizione non disponibile')}"
                    if x['detail'].isdigit() else f"{x['action']} - {x['detail']}"
                ),
                axis=1
            )

            combined_df['debug_key'] = combined_df['detail'].apply(lambda x: f"E{int(x):02d}" if x.isdigit() else None)
            combined_df['debug_description'] = combined_df['debug_key'].apply(lambda k: error_description_map.get(k, 'Descrizione non disponibile'))

            combined_df['action_detail'] = combined_df.apply(add_error_descriptions, axis=1)
            combined_df['action_detail'] = combined_df['action_detail'].apply(parse_param_string)

            # === METRICHE TOP IN COLONNE ===
            st.markdown("### 🔧 Metriche Generali")
            col1, col2, col3 = st.columns(3)

            with col1:
                power_on_count = combined_df[combined_df['detail'] == "app start"].shape[0]
                st.metric("Numero di Sessioni", value=power_on_count, help="Una sessione equivale ad una accensione/spegnimento. Ci possono essere più sessioni in una giornata.")

            with col2:
                session_durations = calculate_session_durations(combined_df)
                if session_durations:
                    avg_session_duration = np.mean(session_durations)
                    st.metric("Durata Media Sessione (minuti)", value=round(avg_session_duration, 2))
                else:
                    st.metric("Durata Media Sessione (minuti)", value="N/D")

            with col3:
                total_errors_count = count_filtered_errors(combined_df)
                st.metric("Errori Totali (filtrati)", value=total_errors_count, help="Esclude E36, E36 solved, E33, E35, E32.")

            # === GRAFICI ===

            with st.expander("Filtra azioni per il grafico", expanded=False):
                unique_actions = combined_df['action_detail'].unique().tolist()
                actions_to_plot = st.multiselect("Azioni da visualizzare nel grafico:", unique_actions, default=unique_actions)

            plot_df = combined_df[combined_df['action_detail'].isin(actions_to_plot)]
            visualize_updates(combined_df)
            visualize_error_chart(plot_df, show_legend=st.checkbox("Mostra legenda", value=True))

            st.markdown("### 🧩 Panoramica Errori")
            session_error_df, total_sessions = build_session_error_df(combined_df, actions_to_plot)
            chart = alt.Chart(session_error_df[session_error_df['Has Error'] == True]).mark_circle(size=30).encode(
                x=alt.X('Sessione:O', title='Numero di Sessione', scale=alt.Scale(domain=list(range(1, total_sessions + 1)))),
                y=alt.Y('Errore:N', title='Tipo di Errore'),
                color=alt.Color('Errore:N', legend=alt.Legend(orient='top')),
                tooltip=['Sessione', 'Errore']
            ).properties(
                width=900,
                height=400
            ).interactive()
            global session_chart_saved
            session_chart_saved = chart
            st.altair_chart(chart, use_container_width=True)


            # === DETTAGLIO SESSIONI ===
            st.markdown("### 🎯 Analisi Dettagliata di una Sessione")
            sessions_with_errors = find_sessions_with_errors(combined_df)
            session_selected = st.selectbox(
                "Seleziona una sessione da analizzare:",
                options=sessions_with_errors.keys(),
                format_func=lambda x: (
                    f"Sessione {x} "
                    f"(da {sessions_with_errors[x][0]} a {sessions_with_errors[x][1]})"
                    + (
                        f" 🔴 Errori: {', '.join(sessions_with_errors[x][2])}"
                        if sessions_with_errors[x][2]
                        else " 🟢 Nessun errore"
                    )
                )
            )

            session_df = combined_df[
                (combined_df['msboot'] >= sessions_with_errors[session_selected][0]) &
                (combined_df['msboot'] <= sessions_with_errors[session_selected][1])
            ]
            visualize_session_chart(session_df, actions_to_plot)
            

            st.markdown("### 🤖 Generatore di Report Tecnico")

            with st.expander("📝 Scrivi una richiesta o una nota tecnica per generare un report", expanded=True):
                with st.form("llm_form"):
                    user_input = st.text_area("Scrivi qui cosa vuoi analizzare o riassumere:", height=200)
                    submitted = st.form_submit_button("Genera Report LLM")

                if submitted:
                    if user_input.strip():
                        with st.spinner("Generazione in corso..."):
                            llm_report = generate_llm_report(user_input, combined_df)
                            st.subheader("🧾 Report Generato:")
                            st.write(llm_report)

                            # Salva i grafici
                            grafico1_path = save_chart_as_image(error_chart_saved, filename="grafico_errori.png")
                            grafico2_path = save_chart_as_image(session_chart_saved, filename="grafico_sessioni.png")
                            grafico3_path = save_chart_as_image(session_detail_chart_saved, filename="grafico_dettaglio_sessione.png")

                            # Genera il PDF completo
                            pdf_path = generate_pdf_custom(
                                llm_report=llm_report,
                                session_df=session_df,
                                grafico1_path=grafico1_path,
                                grafico2_path=grafico2_path,
                                grafico3_path=grafico3_path
                            )

                            with open(pdf_path, "rb") as f:
                                st.download_button(
                                    label="📥 Scarica Report Completo (PDF)",
                                    data=f,
                                    file_name="asa_log_report.pdf",
                                    mime="application/pdf"
                                )

                    else:
                        st.warning("Inserisci del testo prima di generare il report.")
        

        except Exception as e:
            st.error(f"Errore nella decriptazione o lettura del file: {e}")
            st.stop()


def preprocess_table(df, table_name):
    df = df.drop(columns=['USER', 'tipo'], errors='ignore')
    df['msboot'] = pd.to_datetime(df['TIMEMS'], unit='ms', origin='unix')
    df = df.drop(columns=['TIMEMS'])
    df['detail'] = df['DESC']
    df = df.drop(columns=['DESC'])
    
    if table_name == "MANUT":
        df['action'] = 'MANUT'
    else:
        df['action'] = 'ERROR'
    
    return df

def load_table_data(conn, table_name):
    query = f"SELECT * FROM {table_name}"
    return pd.read_sql_query(query, conn)

def calculate_session_durations(df):
    start_events = df[df['detail'] == "app start"].sort_values(by='msboot')
    df = df.sort_values(by='msboot')
    session_durations = []
    MAX_SESSION_DURATION = 1440  # 24 ore in minuti

    for i, start_time in enumerate(start_events['msboot']):
        next_start_time = start_events['msboot'].iloc[i + 1] if i + 1 < len(start_events) else None
        session_end = df[(df['msboot'] > start_time) & (df['msboot'] < next_start_time)]['msboot'].max() if next_start_time else df[df['msboot'] > start_time]['msboot'].max()
        
        if pd.notnull(session_end) and session_end > start_time:
            session_duration = (session_end - start_time).total_seconds() / 60
            if session_duration <= MAX_SESSION_DURATION:
                session_durations.append(session_duration)
    return session_durations

def count_filtered_errors(df):
    filtered_errors_df = df[
        (df['action_detail'].str.startswith('ERROR')) & 
        (~df['detail'].str.contains("E33|E35|E32", case=False))  # Aggiunto E32
    ]
    return filtered_errors_df.shape[0]

def visualize_error_chart(plot_df, show_legend):
    error_df = plot_df[plot_df['action_detail'].str.startswith("ERROR") & 
                       ~plot_df['action_detail'].str.contains("E33|E35|E32")]  # Aggiunto E32
    error_counts = error_df.groupby('action_detail').size().reset_index(name='count')
    
    if not error_counts.empty:
        error_chart = alt.Chart(error_counts).mark_bar().encode(
            x=alt.X('action_detail:N', title='Tipo di Errore'),
            y=alt.Y('count:Q', title='Conteggio degli Errori'),
            color=alt.Color('action_detail:N', legend=alt.Legend(orient='top') if show_legend else None),
            tooltip=['action_detail:N', 'count:Q']
        ).properties(width=900, height=400)
        global error_chart_saved
        error_chart_saved = error_chart
        st.altair_chart(error_chart_saved, use_container_width=True)

@st.cache_data
def find_sessions_with_errors(df):
    start_events = df[df['detail'] == "app start"].sort_values(by='msboot')
    sessions_with_errors = {}
    
    for i, start_time in enumerate(start_events['msboot']):
        next_start_time = (
            start_events['msboot'].iloc[i + 1] 
            if i + 1 < len(start_events) 
            else None
        )
        
        if next_start_time is not None:
            session_end = df[
                (df['msboot'] > start_time) & (df['msboot'] < next_start_time)
            ]['msboot'].max()
        else:
            session_end = df[df['msboot'] > start_time]['msboot'].max()
        
        session_df = df[
            (df['msboot'] >= start_time) & 
            (df['msboot'] <= session_end)
        ]
        
        # Maschera per le righe che contengono "ERROR" in action_detail
        error_present = session_df['action_detail'].str.contains("ERROR", case=False, na=False)
        # Escludi E32, E33, E35 dalla colonna 'detail'
        real_error_mask = (
            ~session_df['detail'].str.contains("E33|E35|E32", case=False, na=False) 
            & error_present
        )
        
        # Ricava la lista di errori reali trovati (su 'detail' o 'action_detail', a seconda di dove sono i codici)
        real_errors = session_df.loc[real_error_mask, 'detail'].unique().tolist()
        
        # Salviamo tutte le sessioni, con la lista  (vuota se non ci sono)
        sessions_with_errors[i + 1] = (start_time, session_end, real_errors)
    
    return sessions_with_errors


def visualize_session_chart(session_df, actions_to_plot):

    # Checkbox per mostrare/nascondere la legenda
    show_legend = st.checkbox("Mostra legenda", value=True, key="legend_checkbox_session_chart")

    # Filtra il DataFrame per includere solo le azioni selezionate
    filtered_session_df = session_df[session_df['action_detail'].isin(actions_to_plot)]

    # Crea il grafico
    line_chart = alt.Chart(filtered_session_df).mark_line().encode(
        x=alt.X(
            'msboot:T',
            axis=alt.Axis(format='%Y-%m-%d %H:%M:%S', title='Data e Ora')
        ),
        y=alt.Y(
            'action_detail:N',
            axis=alt.Axis(labels=False, ticks=False, title='Dettagli Azioni')
        ),
        tooltip=[
            alt.Tooltip('msboot:T', title='Data e Ora',format='%Y-%m-%d %H:%M:%S'),
            alt.Tooltip('action_detail:N', title='Dettaglio Azione')
        ]
    ).interactive()

    points = alt.Chart(filtered_session_df).mark_point(size=60).encode(
        x=alt.X(
            'msboot:T',
            axis=alt.Axis(title='Data e Ora')
        ),
        y=alt.Y(
            'action_detail:N',
            axis=alt.Axis(title='Dettagli Azioni')
        ),
        color=alt.Color(
            'action_detail:N', 
            legend=alt.Legend() if show_legend else None
        ),
        tooltip=[
            alt.Tooltip('msboot:T', title='Data e Ora',format='%Y-%m-%d %H:%M:%S'),
            alt.Tooltip('action_detail:N', title='Dettaglio Azione')
        ]
    )

    error_points = alt.Chart(filtered_session_df[filtered_session_df['action'] == 'ERROR']).mark_point(size=200, shape='circle').encode(
        x=alt.X(
            'msboot:T',
            axis=alt.Axis(title='Data e Ora')
        ),
        y=alt.Y(
            'action_detail:N',
            axis=alt.Axis(title='Dettagli Azioni')
        ),
        color=alt.value('red'),
        tooltip=[
            alt.Tooltip('msboot:T', title='Data e Ora',format='%Y-%m-%d %H:%M:%S'),
            alt.Tooltip('action_detail:N', title='Dettaglio Azione')
        ]
    )

    chart = (line_chart + points + error_points).properties(width=900, height=400)
    # 🔁 Salviamo due grafici distinti:
    global session_detail_chart_saved
    session_detail_chart_saved = chart  # Questo serve per il PDF!
    st.altair_chart(chart, use_container_width=True)


    # Mostra i parametri 'PARAM' se presenti
    params_df = session_df[session_df['action_detail'].str.startswith("PARAM")]
    if not params_df.empty:
        st.write("Parametri utilizzati per questa sessione:")
        st.dataframe(params_df[['msboot', 'action_detail']])
    else:
        st.write("Nessun parametro trovato per questa sessione.")

def visualize_session_errors_overview(combined_df, actions_to_plot):
    # Filtra le sessioni in base alle azioni selezionate
    plot_df = combined_df[combined_df['action_detail'].isin(actions_to_plot) & 
                          combined_df['action_detail'].str.startswith("ERROR") & 
                          ~combined_df['action_detail'].str.contains("E33|E35|E32")]  # Aggiunto E32

    # Identifica tutte le sessioni numerandole
    start_events = combined_df[combined_df['detail'] == "app start"].sort_values(by='msboot')
    total_sessions = len(start_events)
    session_indices = []
    error_types = []
    has_error_flags = []
    session_num = 0

    for i, start_time in enumerate(start_events['msboot']):
        next_start_time = start_events['msboot'].iloc[i + 1] if i + 1 < len(start_events) else None
        session_end = combined_df[(combined_df['msboot'] > start_time) & (combined_df['msboot'] < next_start_time)]['msboot'].max() if next_start_time else combined_df[combined_df['msboot'] > start_time]['msboot'].max()
        
        session_df = plot_df[(plot_df['msboot'] >= start_time) & (plot_df['msboot'] <= session_end)]
        
        # Includi la sessione, anche senza errori
        session_num += 1
        if not session_df.empty:
            for error in session_df['action_detail'].unique():
                session_indices.append(session_num)
                error_types.append(error)
                has_error_flags.append(True)
        else:
            # Aggiunge la sessione senza errori, senza un errore specifico
            session_indices.append(session_num)
            error_types.append(np.nan)  # Usa NaN per evitare di mostrare un punto
            has_error_flags.append(False)  # Flag per sessione senza errori

    # Crea un DataFrame per il grafico delle sessioni
    session_error_df = pd.DataFrame({
        'Sessione': session_indices,
        'Errore': error_types,
        'Has Error': has_error_flags
    })

    # Crea il grafico interattivo con punti solo per le sessioni con errori
    session_error_chart = alt.Chart(session_error_df[session_error_df['Has Error'] == True]).mark_circle(size=30).encode(
        x=alt.X('Sessione:O', title='Numero di Sessione', scale=alt.Scale(domain=list(range(1, total_sessions + 1)))),
        y=alt.Y('Errore:N', title='Tipo di Errore'),
        color=alt.Color('Errore:N', legend=alt.Legend(orient='top')),
        tooltip=['Sessione', 'Errore']
    ).properties(
        width=900,
        height=400
    ).interactive()

    st.altair_chart(session_error_chart, use_container_width=True)

@st.cache_data(show_spinner="Elaborazione sessioni ed errori...")
def build_session_error_df(combined_df, actions_to_plot):
    plot_df = combined_df[
        combined_df['action_detail'].isin(actions_to_plot) &
        combined_df['action_detail'].str.startswith("ERROR") &
        ~combined_df['action_detail'].str.contains("E33|E35|E32")
    ]

    start_events = combined_df[combined_df['detail'] == "app start"].sort_values(by='msboot')
    total_sessions = len(start_events)
    session_indices = []
    error_types = []
    has_error_flags = []
    session_num = 0

    for i, start_time in enumerate(start_events['msboot']):
        next_start_time = start_events['msboot'].iloc[i + 1] if i + 1 < len(start_events) else None
        session_end = (
            combined_df[(combined_df['msboot'] > start_time) & (combined_df['msboot'] < next_start_time)]['msboot'].max()
            if next_start_time
            else combined_df[combined_df['msboot'] > start_time]['msboot'].max()
        )

        session_df = plot_df[(plot_df['msboot'] >= start_time) & (plot_df['msboot'] <= session_end)]
        session_num += 1

        if not session_df.empty:
            for error in session_df['action_detail'].unique():
                session_indices.append(session_num)
                error_types.append(error)
                has_error_flags.append(True)
        else:
            session_indices.append(session_num)
            error_types.append(np.nan)
            has_error_flags.append(False)

    session_error_df = pd.DataFrame({
        'Sessione': session_indices,
        'Errore': error_types,
        'Has Error': has_error_flags
    })

    return session_error_df, total_sessions


def parse_param_string(param_str):
    if not param_str.startswith("PARAM"):
        return param_str
    
    param_str = param_str.replace("PARAM - ", "")
    params = dict(item.split("=") for item in param_str.split(", "))

    params['CW'] = int(params.get('CW', 0))
    params['frequenza'] = int(params.pop('f', 0))
    params['tempo'] = int(params.pop('t', 0)) / 1000  # Converti ms in secondi
    params['intensità'] = int(params.pop('i', 0))
    params['area'] = int(params.pop('a', 0))
    params['n.area'] = int(params.pop('na', 0))
    params['punti'] = int(params.pop('p', 0))
    params['terminale'] = 'manipolo' if int(params.pop('term', 0)) == 1 else 'charlie'

    readable_str = (
        f"PARAM - CW={params['CW']}, frequenza={params['frequenza']}, "
        f"tempo={params['tempo']:.2f} s, intensità={params['intensità']}, "
        f"area={params['area']}, n.area={params['n.area']}, "
        f"punti={params['punti']}, terminale={params['terminale']}"
    )
    
    return readable_str

def add_error_descriptions(row):
    # Verifica se 'action_detail' contiene un codice errore nel formato 'ERROR - E<num>'
    if "ERROR - E" in row['action_detail']:
        # Estrarre il codice errore (E<num>)
        error_code = row['action_detail'].split(" - ")[1]
        # Aggiungere la descrizione se il codice esiste nel dizionario
        description = error_description_map.get(error_code, 'Descrizione non disponibile')
        # Restituire la stringa aggiornata con la descrizione
        return f"{row['action_detail']} - {description}"
    return row['action_detail']

def visualize_updates(df):
    # Filtra i log con 'btnStartUpdate' nel dettaglio
    update_logs = df[df['detail'] == 'btnStartUpdate']

    if not update_logs.empty:
        # Conta il numero di aggiornamenti
        update_count = update_logs.shape[0]
        # Ottieni le date di tutti gli aggiornamenti
        update_dates = update_logs['msboot'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist()
        # Combina le date in una stringa
        dates_str = ", ".join(update_dates)

        # Mostra il conteggio e le date degli aggiornamenti
        st.metric(
            label="Aggiornamento eseguito",
            value=f"SI",
            delta=f"{update_count} aggiornamento/i",
            help=f"Date aggiornamenti: {dates_str}"
        )
        
    else:
        # Nessun aggiornamento trovato
        st.metric(
            label="Aggiornamento eseguito",
            value="NO",
            delta=None,
            help="Nessun aggiornamento rilevato nei log."
        )

@st.cache_data(show_spinner="Decrypting and parsing ZIP file...")
def decrypt_and_parse(zip_bytes: bytes):
    with tempfile.TemporaryDirectory() as temp_dir:
        # Salva e decomprimi
        zip_path = os.path.join(temp_dir, "uploaded.zip")
        with open(zip_path, "wb") as f:
            f.write(zip_bytes)

        with ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)

        # Decrypt .db
        extracted_files = []
        for root, _, files in os.walk(temp_dir):
            for file_name in files:
                if file_name.endswith(".db"):
                    file_path = os.path.join(root, file_name)
                    decrypted_path = os.path.join(temp_dir, f"{file_name}_decrypted.db")
                    with open(file_path, 'rb') as f:
                        encrypted_data = f.read()

                    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
                    decrypted_data = cipher.decrypt(encrypted_data)

                    with open(decrypted_path, 'wb') as f:
                        f.write(decrypted_data)

                    extracted_files.append(decrypted_path)

        # Trova file
        mlslog_path = next((f for f in extracted_files if "mlslog" in f.lower()), None)
        registri_path = next((f for f in extracted_files if "registri" in f.lower()), None)

        if not mlslog_path or not registri_path:
            raise ValueError("File mlslog.db o registri.db non trovati.")

        # Leggi DB
        log1_df = load_table(mlslog_path, "log1")
        if 'msboot' in log1_df.columns:
            log1_df['msboot'] = pd.to_datetime(log1_df['msboot'], unit='ms', origin='unix', errors='coerce')

        registro_conn = sqlite3.connect(registri_path)
        allarmi_df = preprocess_table(load_table_data(registro_conn, "ALLARMI"), "ALLARMI")
        manut_df = preprocess_table(load_table_data(registro_conn, "MANUT"), "MANUT")
        registro_conn.close()

        combined_df = pd.concat([log1_df, allarmi_df, manut_df], ignore_index=True)
        return combined_df


def load_table(db_path, table_name):
    try:
        # Controlla se il file esiste
        if not os.path.exists(db_path):
            st.error(f"Il file {db_path} non esiste.")
            return pd.DataFrame()

        # Verifica la validità del file come database SQLite
        if not is_valid_sqlite(db_path):
            st.error(f"Il file {db_path} non è un database SQLite valido.")
            return pd.DataFrame()

        # Connessione al database
        conn = sqlite3.connect(db_path)
        query = f"SELECT * FROM {table_name}"
        df = pd.read_sql_query(query, conn)
        conn.close()

        return df
    except sqlite3.DatabaseError as db_err:
        st.error(f"Errore SQLite durante l'apertura del database {db_path}: {db_err}")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"Errore generico durante il caricamento della tabella {table_name}: {e}")
        return pd.DataFrame()
    
def is_valid_sqlite(file_path):
    try:
        conn = sqlite3.connect(file_path)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1;")
        conn.close()
        return True
    except sqlite3.DatabaseError:
        return False
    
def generate_llm_report(user_input, df):
    system_prompt = (
        "Sei un assistente esperto che analizza log di dispositivi medicali a laser. "
        "Riceverai una richiesta utente e un estratto dei log recenti. "
        "Genera un report tecnico chiaro, sintetico e utile all'assistenza tecnica."
    )

    # Prepara i log recenti
    recent_logs = df[['msboot', 'action_detail']].sort_values(by='msboot', ascending=False).head(100)
    log_context = "\n".join([f"{row['msboot']} - {row['action_detail']}" for _, row in recent_logs.iterrows()])

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"LOG RECENTI:\n{log_context}"},
        {"role": "user", "content": f"RICHIESTA: {user_input}"}
    ]

    try:
        response = client.chat.completions.create(
            model="gpt-4o",  # usa "gpt-4o" o "gpt-4o-mini" se disponibile nel tuo piano
            messages=messages,
            temperature=0.3,
            max_tokens=800
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"❌ Errore nella generazione del report: {e}"


def save_chart_as_image(chart, filename="chart.png"):
    temp_path = os.path.join(tempfile.gettempdir(), filename)
    chart.save(temp_path, format='png', scale_factor=2)  # alta qualità
    return temp_path

class PDF(FPDF):
    def header(self):
        self.set_font("Arial", "B", 12)
        self.image("logo_asa.png", 10, 8, 50)
        # Titolo a destra, allineato con il logo
        self.set_xy(70, 15)  # sposta il cursore a destra del logo
        self.set_font("Arial", "B", 14)
        self.cell(0, 10, "ASA Log Report", ln=True, align="R")
        # Spazio sotto intestazione per lasciare respiro al contenuto
        self.ln(25)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "I", 8)
        self.cell(0, 10, f"Pagina {self.page_no()}", align="C")

def generate_pdf_custom(llm_report, session_df, grafico1_path, grafico2_path, grafico3_path):
    pdf = PDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", size=12)

    pdf.ln(10)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "Report automatico", ln=True)
    pdf.set_font("Arial", size=11)
    render_markdown_to_pdf(pdf, llm_report)


    for path, titolo in zip(
        [grafico1_path, grafico2_path, grafico3_path],
        ["Distribuzione Errori", "Errori per Sessione", "Dettaglio Sessione"]
    ):
        pdf.add_page()
        pdf.set_font("Arial", "B", 14)
        pdf.cell(0, 10, titolo, ln=True)
        pdf.image(path, x=10, w=190)

    pdf.add_page()
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "Log Ultima Sessione", ln=True)
    pdf.set_font("Arial", size=10)
    pdf.cell(80, 8, "Data/Ora", 1)
    pdf.cell(110, 8, "Azione", 1)
    pdf.ln()

    for _, row in session_df.iterrows():
        data = str(row['msboot'])
        azione = str(row['action_detail'])[:50]
        pdf.cell(80, 8, data, 1)
        pdf.cell(110, 8, azione, 1)
        pdf.ln()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
        pdf.output(f.name)
        return f.name
    
def generate_diagnostic_pdf(sn, modello, versione_sw, descrizione, alimentazione, circostanza, frequenza, allegati_nomi, suggerimento):
    pdf = PDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", size=12)

    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "Dati del Report Diagnostico", ln=True)

    def write_kv(label, value):
        pdf.set_font("Arial", "B", 11)
        pdf.cell(50, 8, f"{label}:", 0)
        pdf.set_font("Arial", "", 11)
        pdf.multi_cell(0, 8, value)

    write_kv("Serial Number (SN)", sn)
    write_kv("Modello", modello)
    write_kv("Versione SW", versione_sw)
    write_kv("Alimentazione", alimentazione)
    write_kv("Circostanza", circostanza)
    write_kv("Frequenza", frequenza)

    pdf.ln(5)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Descrizione del problema:", ln=True)
    pdf.set_font("Arial", "", 11)
    pdf.multi_cell(0, 8, descrizione)

    pdf.ln(5)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Suggerimento GPT:", ln=True)
    pdf.set_font("Arial", "", 11)
    render_markdown_to_pdf(pdf, suggerimento)

    if allegati_nomi:
        pdf.ln(5)
        pdf.set_font("Arial", "B", 12)
        pdf.cell(0, 10, "File allegati:", ln=True)
        pdf.set_font("Arial", "", 11)
        for nome in allegati_nomi:
            pdf.cell(0, 8, f"- {nome}", ln=True)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
        pdf.output(f.name)
        return f.name


def render_markdown_to_pdf(pdf, markdown_str):
    lines = markdown_str.split('\n')
    for line in lines:
        line = line.strip()

        # Titoli
        if line.startswith("### "):
            pdf.set_font("Arial", "B", 13)
            pdf.ln(5)
            pdf.cell(0, 8, line[4:], ln=True)
        elif line.startswith("## "):
            pdf.set_font("Arial", "B", 14)
            pdf.ln(6)
            pdf.cell(0, 9, line[3:], ln=True)
        elif line.startswith("# "):
            pdf.set_font("Arial", "B", 15)
            pdf.ln(8)
            pdf.cell(0, 10, line[2:], ln=True)
        # Elenchi puntati
        elif line.startswith("- "):
            pdf.set_font("Arial", "", 11)
            pdf.multi_cell(0, 7, f"- {line[2:]}")
        # Grassetto semplice (rimuove ** ma non imposta bold)
        elif "**" in line:
            line = line.replace("**", "")
            pdf.set_font("Arial", "", 11)
            pdf.multi_cell(0, 7, line)
        # Normale
        else:
            pdf.set_font("Arial", "", 11)
            pdf.multi_cell(0, 7, line)

    pdf.ln(5)


def show_diagnostic_form():
    st.title("🛠️ Form Diagnostico Assistenza")

    # === FORM ===
    st.subheader("📌 Informazioni Generali")
    sn = st.text_input("Serial Number (SN)")
    modello = st.text_input("Modello macchina")
    versione_sw = st.text_input("Versione Software")

    st.subheader("🐞 Dettagli Problema")
    descrizione = st.text_area("Descrizione del problema (o codice errore)", height=150)
    foto = st.file_uploader("📎 Allegati (foto, log, video)", accept_multiple_files=True)

    st.subheader("⚡ Tipo di alimentazione al momento dell’errore")
    alimentazione = st.radio("Tipo di alimentazione", ["Tensione di rete", "Batteria"])

    st.subheader("📅 Quando è avvenuto l’errore?")
    circostanza = st.selectbox("Circostanza", [
        "All'accensione", 
        "Entro 2 sec. da tasto Start", 
        "Entro 2 sec. da tasto su applicatori/robot",
        "Durante emissione", 
        "A fine terapia", 
        "Altro"
    ])

    st.subheader("🔁 Frequenza dell’errore")
    frequenza = st.selectbox("Frequenza", [
        "Molto raro (max 1 volta/mese)", 
        "Raro (1 volta/settimana)", 
        "Ricorrente (>1 volta/settimana)", 
        "Sistematico (ogni utilizzo o sempre stesso punto)"
    ])

    st.markdown("---")

    # === GPT SUGGERIMENTO ===
    st.subheader("🤖 Suggerimento automatico GPT")

    if "suggerimento_gpt" not in st.session_state:
        st.session_state.suggerimento_gpt = ""

    if st.button("🔍 Analizza e suggerisci azione"):
        if descrizione.strip():
            with st.spinner("Analisi in corso..."):
                prompt_txt = load_prompt_text()
                st.session_state.suggerimento_gpt = gpt_troubleshooting(descrizione, prompt_txt)
        else:
            st.warning("Devi inserire una descrizione per usare GPT.")

    if st.session_state.suggerimento_gpt:
        st.markdown("### ✅ Suggerimento GPT")
        st.text_area("Risposta GPT", st.session_state.suggerimento_gpt, height=200)

    suggerimento = st.session_state.suggerimento_gpt

    # === SALVATAGGIO PDF / ZIP ===
    if suggerimento:
        st.markdown("---")
        st.subheader("📥 Salva Report")

        # Prepara lista nomi file allegati
        foto_names = [f.name for f in foto] if foto else []

        # === GENERA PDF UNA VOLTA
        pdf_path = generate_diagnostic_pdf(
            sn, modello, versione_sw, descrizione,
            alimentazione, circostanza, frequenza,
            foto_names, suggerimento
        )

        with open(pdf_path, "rb") as f:
            st.download_button(
                label="📄 Scarica Report Diagnostico (PDF)",
                data=f,
                file_name="report_diagnostico.pdf",
                mime="application/pdf"
            )

        # === GENERA ZIP CON PDF + ALLEGATI
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_name = os.path.join(tmpdir, "report_diagnostico.pdf")
            os.rename(pdf_path, pdf_name)

            allegati_dir = os.path.join(tmpdir, "allegati")
            os.makedirs(allegati_dir, exist_ok=True)
            allegati_paths = []

            if foto:
                for f in foto:
                    path = os.path.join(allegati_dir, f.name)
                    with open(path, "wb") as out:
                        out.write(f.read())
                    allegati_paths.append(path)

            zip_path = os.path.join(tmpdir, "report_completo.zip")
            with zipfile.ZipFile(zip_path, "w") as zipf:
                zipf.write(pdf_name, arcname="report_diagnostico.pdf")
                for f in allegati_paths:
                    zipf.write(f, arcname=os.path.join("allegati", os.path.basename(f)))

            with open(zip_path, "rb") as f:
                st.download_button(
                    label="📦 Scarica ZIP completo (PDF + allegati)",
                    data=f,
                    file_name="report_completo.zip",
                    mime="application/zip"
                )



def load_prompt_text(path="ErrorCode_MHiMVet.txt"):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def gpt_troubleshooting(diagnosi, prompt_txt):
    messages = [
        {"role": "user", "content": f"""
Sei un tecnico ASA. Ti fornisco una tabella con errori e azioni consigliate.

Il tuo compito è:
1. Leggere la descrizione del problema
2. Confrontarla con la tabella
3. Restituire:

CODICE ERRORE: ...
AZIONE CONSIGLIATA: ...

TABELLA:
{prompt_txt}

DIAGNOSI:
{diagnosi}
"""}
    ]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", 
            messages=messages,
            temperature=0.3,
            max_tokens=800
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Errore nella generazione della risposta GPT: {e}"



if __name__ == "__main__":
    main()
