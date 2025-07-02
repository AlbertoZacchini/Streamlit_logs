import pandas as pd

def generate_prompt_txt(excel_path, output_txt_path):
    df = pd.read_excel(excel_path)

    with open(output_txt_path, "w", encoding="utf-8") as f:
        for _, row in df.iterrows():
            codice = str(row["system"]).strip()
            messaggio = str(row["description"]).strip()
            azione = str(row["note"]).strip()

            if codice and messaggio and azione:
                f.write(f"[{codice}] {messaggio} -> {azione}\n")

if __name__ == "__main__":
    generate_prompt_txt("ErrorCode_MHiMVet.xlsx", "ErrorCode_MHiMVet.txt")
