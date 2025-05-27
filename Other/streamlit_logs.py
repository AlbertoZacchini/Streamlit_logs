import streamlit as st
import sqlite3
import pandas as pd
import tempfile
import altair as alt
from datetime import datetime, timedelta
import numpy as np
import os
import webbrowser
import sys

# Carica il file Excel per associare gli errori alla descrizione
file_path = 'laser_alarms.xlsx'
excel_data = pd.ExcelFile(file_path)
df_alarms = excel_data.parse('Sheet1')

# Creiamo un dizionario che mappa l'errore (numero) alla descrizione
error_description_map = dict(zip(df_alarms['Index'], df_alarms['Error Message']))

# Funzione principale
def main():
    st.title("ASA Log file viewer (M-Hi/M-Vet)")
    uploaded_files = st.file_uploader("Carica il file `mlslog.db` o `.ASA`", type=["db", "ASA"], accept_multiple_files=True)

    if uploaded_files:
        connections = []
        tables_dict = {}

        for uploaded_file in uploaded_files:
            # Crea un file temporaneo con estensione `.db` o `.ASA`
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_file.write(uploaded_file.read())
                temp_file_path = temp_file.name

            try:
                conn = sqlite3.connect(temp_file_path)
                connections.append(conn)

                tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table';", conn)
                for table_name in tables['name']:
                    if table_name not in tables_dict:
                        tables_dict[table_name] = []
                    tables_dict[table_name].append(conn)
            except Exception as e:
                st.error(f"Errore nella connessione al database {uploaded_file.name}: {e}")
                continue  # Salta questo file e passa al successivo

        if tables_dict:
            if "log1" in tables_dict:
                selected_table = "log1"  # Imposta automaticamente la tabella `log1`
                st.write(f"Caricamento automatico della tabella `{selected_table}`")
            else:
                st.warning("La tabella `log1` non è presente. Seleziona un'altra tabella.")
                selected_table = st.selectbox("Seleziona una tabella da visualizzare:", list(tables_dict.keys()))
        
            if selected_table:
                df_list = [load_table_data(conn, selected_table) for conn in connections]
                df = pd.concat(df_list, ignore_index=True)

                # Conversione corretta del timestamp di log1
                if 'msboot' in df.columns:
                    df['msboot'] = pd.to_datetime(df['msboot'], unit='ms', origin='unix', errors='coerce')

                # Caricamento dei dati da registro.db (tabelle ALLARMI e MANUT)
                registro_file = st.file_uploader("Carica il file `registro.db` o `.ASA`", type=["db", "ASA"], key="registro")
                if registro_file:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as temp_file:
                        temp_file.write(registro_file.read())
                        temp_file_path = temp_file.name

                    try:
                        registro_conn = sqlite3.connect(temp_file_path)

                        # Caricamento e preprocessing delle tabelle ALLARMI e MANUT
                        allarmi_df = preprocess_table(load_table_data(registro_conn, "ALLARMI"), "ALLARMI")
                        manut_df = preprocess_table(load_table_data(registro_conn, "MANUT"), "MANUT")

                        # Concatenazione dei dati
                        combined_df = pd.concat([df, allarmi_df, manut_df], ignore_index=True)

                        # Creazione di 'action_detail' durante la concatenazione

                        # Assicurati che 'detail' sia trattato come stringa e nel formato corretto per la mappatura
                        combined_df['detail'] = combined_df['detail'].astype(str)

                        # Durante la concatenazione dei dati, aggiorna il campo `action_detail` con la descrizione dell'errore
                        # Aggiorna il campo 'action_detail' per includere numero errore e descrizione
                        # Aggiorna 'action_detail' per includere il codice errore e la descrizione
                        combined_df['action_detail'] = combined_df.apply(
                            lambda x: (
                                f"{x['action']} - {x['detail']} - {error_description_map.get(f'E{int(x['detail']):02d}', 'Descrizione non disponibile')}"
                                if x['detail'].isdigit() else f"{x['action']} - {x['detail']}"
                            ),
                            axis=1
                        )
                       

                        # Colonna di debug per verificare la trasformazione della chiave e la ricerca nel dizionario
                        combined_df['debug_key'] = combined_df['detail'].apply(lambda x: f"E{int(x):02d}" if x.isdigit() else None)
                        combined_df['debug_description'] = combined_df['debug_key'].apply(lambda k: error_description_map.get(k, 'Descrizione non disponibile'))

                        # Applica la funzione su tutta la colonna 'action_detail'
                        combined_df['action_detail'] = combined_df.apply(add_error_descriptions, axis=1)

                        # Applica la trasformazione leggibile per i parametri
                        combined_df['action_detail'] = combined_df['action_detail'].apply(parse_param_string)


                        # Visualizza la tabella concatenata
                        st.write(f"Visualizzando la tabella concatenata: `{selected_table}`")
                        st.dataframe(combined_df)
                        


                        # Selezione azioni per visualizzazione grafico con expander
                        with st.expander("Seleziona Azioni da Visualizzare", expanded=False):
                            unique_actions = combined_df['action_detail'].unique().tolist()
                            actions_to_plot = st.multiselect("Seleziona le azioni da visualizzare nel grafico:", unique_actions, default=unique_actions)

                        # Filtra il DataFrame in base alle azioni selezionate
                        plot_df = combined_df[combined_df['action_detail'].isin(actions_to_plot)]

                        # Aggiungi il visualizzatore di aggiornamenti
                        visualize_updates(combined_df)

                        # Conteggio delle accensioni
                        power_on_count = combined_df[combined_df['detail'] == "app start"].shape[0]
                        st.metric("Numero di Sessioni", value=power_on_count, delta=None, help="Una sessione equivale ad una accensione/spegnimento. Ci possono essere più sessioni in una giornata")


                        # Calcola durata media delle sessioni
                        session_durations = calculate_session_durations(combined_df)
                        if session_durations:
                            avg_session_duration = np.mean(session_durations)
                            st.metric("Durata Media Sessione (minuti)", value=round(avg_session_duration, 2), help="Durata media di ogni sessione in minuti.")
                        else:
                            st.write("Non ci sono sessioni complete disponibili per calcolare la durata media.")

                        # Conteggio degli errori esclusi alcuni tipi
                        total_errors_count = count_filtered_errors(combined_df)
                        st.metric("Numero Totale di Errori (esclusi gli errori E36, E36 solved, E33, E35, E32)", value=total_errors_count, delta=None, help="Gli errori esclusi sono relativi al fungo d'emergenza, all'interlock e all'attacco/stacco della lente.")

                        # Visualizzazione del grafico degli errori
                        visualize_error_chart(plot_df, show_legend=st.checkbox("Mostra legenda", value=True))

                        # Visualizzazione panoramica degli errori nelle sessioni
                        filtered_df = combined_df[combined_df['action_detail'].isin(actions_to_plot)]
                        st.write("Panoramica degli errori nelle sessioni:")
                        visualize_session_errors_overview(filtered_df, actions_to_plot)

                        # Aggiungi il grafico delle sessioni con errori veri
                        sessions_with_errors = find_sessions_with_errors(combined_df)
                        session_selected = st.selectbox(
                            "Seleziona una sessione con errori",
                            options=sessions_with_errors.keys(),
                            format_func=lambda x: f"Sessione {x} (da {sessions_with_errors[x][0]} a {sessions_with_errors[x][1]})"
                        )

                        # Visualizza il grafico della sessione selezionata
                        session_df = combined_df[ 
                            (combined_df['msboot'] >= sessions_with_errors[session_selected][0]) & 
                            (combined_df['msboot'] <= sessions_with_errors[session_selected][1])
                        ]
                        visualize_session_chart(session_df, actions_to_plot)


                    except Exception as e:
                        st.error(f"Errore nella connessione al database di registro: {e}")

                # Chiusura delle connessioni
                for conn in connections:
                    conn.close()
    else:
        st.info("Carica uno o più file .db o .ASA per iniziare.")

    #DA COMMENTARE SE SI VUOLE RUNNARE SUL NETWORK
    
    if "RUN_MAIN" not in os.environ:
        # Siamo nel primo avvio dello script
        os.environ["RUN_MAIN"] = "true"  # Imposta una variabile per evitare loop

        # Avvia Streamlit senza aprire il browser
        os.system(f"streamlit run streamlit_logs.py --browser.serverAddress localhost")
    else:
        # Siamo nel processo avviato da Streamlit: apri il browser
        if "STREAMLIT_SERVER_PORT" in os.environ:
            url = f"http://localhost:{os.environ['STREAMLIT_SERVER_PORT']}"
            webbrowser.open_new(url) 
    

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
    st.write("Grafico della sessione selezionata:")

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

if __name__ == "__main__":
    main()
