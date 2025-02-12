import streamlit as st
import sqlite3
import pandas as pd
import tempfile
import numpy as np
import os
import webbrowser
from Crypto.Cipher import AES
from zipfile import ZipFile 
from funzioni import *

connections = []  # Inizializza la variabile connections come lista vuota

# Carica il file Excel per associare gli errori alla descrizione
file_path = 'laser_alarms.xlsx'
error_description_map=generate_error_map(file_path, sheet_name='Sheet1')

def main():
    st.title("ASA Log File Viewer")

    # Caricamento del file ZIP
    uploaded_file = st.file_uploader("Carica un file compresso (ZIP)", type=["zip"])

    if uploaded_file:
        # Creazione della directory temporanea
        with tempfile.TemporaryDirectory() as temp_dir:
            
            # Estrazione dei file dal file ZIP
            try:
                zip_path = os.path.join(temp_dir, "uploaded.zip")
                with open(zip_path, "wb") as f:
                    f.write(uploaded_file.read())

                with ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)

            except Exception as e:
                st.error(f"Errore durante l'estrazione del file ZIP: {e}")
                return

            # Decriptazione dei file estratti
            extracted_files = []
            for root, _, files in os.walk(temp_dir):
                for file_name in files:
                    if file_name.endswith(".db"):
                        file_path = os.path.join(root, file_name)
                        decrypted_path = os.path.join(temp_dir, f"{file_name}_decrypted.db")
                        
                        try:
                            with open(file_path, 'rb') as f:
                                encrypted_data = f.read()

                            cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
                            decrypted_data = cipher.decrypt(encrypted_data)

                            with open(decrypted_path, 'wb') as f:
                                f.write(decrypted_data)

                            extracted_files.append(decrypted_path)

                        except Exception as e:
                            st.error(f"Errore durante la decriptazione del file {file_name}: {e}")
                            continue

            # Identificazione dei file necessari
            mlslog_path = next((f for f in extracted_files if "mlslog" in f.lower()), None)
            registri_path = next((f for f in extracted_files if "registri" in f.lower()), None)

            if not mlslog_path or not registri_path:
                st.error("I file necessari (mlslog.db e registri.db) non sono stati trovati.")
                return

            # Connessione e lettura dei dati
            try:
                log1_df = load_table(mlslog_path, "log1")

                # Conversione corretta del timestamp di log1
                if 'msboot' in log1_df.columns:
                    log1_df['msboot'] = pd.to_datetime(log1_df['msboot'], unit='ms', origin='unix', errors='coerce')

                registro_conn = sqlite3.connect(registri_path)

                allarmi_df = preprocess_table(load_table_data(registro_conn, "ALLARMI"), "ALLARMI")
                manut_df = preprocess_table(load_table_data(registro_conn, "MANUT"), "MANUT")

                # Concatenazione dei dati
                combined_df = pd.concat([log1_df, allarmi_df, manut_df], ignore_index=True)

                print(combined_df)

                registro_conn.close()

            except Exception as e:
                st.error(f"Errore durante la lettura delle tabelle: {e}")
            
            try:

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

                combined_df['action_detail'] = combined_df.apply(lambda row: add_error_descriptions(row, error_description_map), axis=1)

                # Applica la trasformazione leggibile per i parametri
                combined_df['action_detail'] = combined_df['action_detail'].apply(parse_param_string)


                # Visualizza la tabella concatenata
                st.write(f"Visualizzando la tabella concatenata:")
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

        #DA COMMENTARE SE SI VUOLE RUNNARE SUL  NETWORK
        
    if "RUN_MAIN" not in os.environ:
        # Siamo nel primo avvio dello script
        os.environ["RUN_MAIN"] = "true"  # Imposta una variabile per evitare loop

        # Avvia Streamlit senza aprire il browser
        os.system(f"streamlit run streamlit_logs_compressed_copy.py --browser.serverAddress localhost")
    else:
        # Siamo nel processo avviato da Streamlit: apri il browser
        if "STREAMLIT_SERVER_PORT" in os.environ:
            url = f"http://localhost:{os.environ['STREAMLIT_SERVER_PORT']}"
            webbrowser.open_new(url) 
    
if __name__ == "__main__":
    main()
