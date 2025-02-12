import streamlit as st
import sqlite3
import pandas as pd
import tempfile
import altair as alt
import numpy as np
import os
from Crypto.Cipher import AES
from zipfile import ZipFile 
from dotenv import load_dotenv

# Carica le variabili dal file .env
load_dotenv()
AES_KEY = os.getenv("AES_KEY").encode("utf-8")  # Converti stringa a bytes
AES_IV = os.getenv("AES_IV").encode("utf-8")


def generate_error_map(file_path, sheet_name='Sheet1'):
    """
    Carica un file Excel, estrae i dati e crea una mappa degli errori.

    Args:
        file_path (str): Il percorso del file Excel da leggere.
        sheet_name (str): Il nome del foglio da cui estrarre i dati. Default è 'Sheet1'.

    Returns:
        dict: Una mappa (dizionario) che associa gli errori (Index) alle descrizioni (Error Message).
    """
    try:
        # Carica il file Excel e crea il DataFrame
        excel_data = pd.ExcelFile(file_path)
        df_alarms = excel_data.parse(sheet_name)

        # Crea il dizionario mappando l'indice degli errori con le descrizioni
        error_description_map = dict(zip(df_alarms['Index'], df_alarms['Error Message']))
        return error_description_map
    except Exception as e:
        print(f"Errore durante la generazione della mappa degli errori: {e}")
        return {}


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
        (~df['detail'].str.contains("E36|E36 solved|E33|E35|E32", case=False))  # Aggiunto E32
    ]
    return filtered_errors_df.shape[0]

def visualize_error_chart(plot_df, show_legend):
    error_df = plot_df[plot_df['action_detail'].str.startswith("ERROR") & 
                       ~plot_df['action_detail'].str.contains("E36|E36 solved|E33|E35|E32")]  # Aggiunto E32
    error_counts = error_df.groupby('action_detail').size().reset_index(name='count')
    
    if not error_counts.empty:
        error_chart = alt.Chart(error_counts).mark_bar().encode(
            x=alt.X('action_detail:N', title='Tipo di Errore'),
            y=alt.Y('count:Q', title='Conteggio degli Errori'),
            color=alt.Color('action_detail:N', legend=alt.Legend(orient='top') if show_legend else None),
            tooltip=['action_detail:N', 'count:Q']
        ).properties(width=900, height=400)
        st.altair_chart(error_chart, use_container_width=True)

def find_sessions_with_errors(df):
    start_events = df[df['detail'] == "app start"].sort_values(by='msboot')
    sessions_with_errors = {}
    
    for i, start_time in enumerate(start_events['msboot']):
        next_start_time = start_events['msboot'].iloc[i + 1] if i + 1 < len(start_events) else None
        session_end = df[(df['msboot'] > start_time) & (df['msboot'] < next_start_time)]['msboot'].max() if next_start_time else df[df['msboot'] > start_time]['msboot'].max()
        
        session_df = df[(df['msboot'] >= start_time) & (df['msboot'] <= session_end)]
        
        error_present = session_df['action_detail'].str.contains("ERROR", case=False)
        # Aggiungi la condizione per escludere anche E32
        has_real_error = any(~session_df['detail'].str.contains("E36|E36 solved|E33|E35|E32", case=False) & error_present)
        
        if has_real_error:
            sessions_with_errors[i + 1] = (start_time, session_end)
    
    return sessions_with_errors

def visualize_session_chart(session_df, actions_to_plot):
    st.write("Grafico della sessione seleziona:")

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
                          ~combined_df['action_detail'].str.contains("E36|E36 solved|E33|E35|E32")]  # Aggiunto E32

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

def add_error_descriptions(row,error_description_map):
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

def decrypt_file(input_file, key, iv):
    try:
        with open(input_file, 'rb') as f:
            encrypted_data = f.read()

        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted_data = cipher.decrypt(encrypted_data)

        # Scrivi in un file temporaneo persistente
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        temp_file.write(decrypted_data)
        temp_file.close()

        return temp_file.name
    except Exception as e:
        st.error(f"Errore durante la decrittazione: {e}")
        return None

def extract_and_decrypt(uploaded_file):
    try:
        extracted_files = []
        
        with ZipFile(uploaded_file, 'r') as z:
            z.extractall(tempfile.gettempdir())  # Salva i file estratti nella directory temporanea globale

        for root, _, files in os.walk(tempfile.gettempdir()):
            for file_name in files:
                if file_name.endswith(".db"):
                    file_path = os.path.join(root, file_name)

                    # Decripta e salva in un file temporaneo
                    decrypted_path = decrypt_file(file_path, AES_KEY, AES_IV)
                    if decrypted_path:
                        extracted_files.append(decrypted_path)

        return extracted_files
    except Exception as e:
        st.error(f"Errore durante l'estrazione e decrittazione: {e}")
        return []




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


print("Modulo funzioni.py importato correttamente.")
