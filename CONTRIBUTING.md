# Guida alla Contribuzione (`CONTRIBUTING.md`)

Grazie per l'interesse nel contribuire a **Verbalia**! Questo documento definisce le regole tecniche, lo stile del codice e le norme di sicurezza/privacy obbligatorie per ogni contributo.

---

## 1. Configurazione dell'Ambiente di Sviluppo

Il progetto richiede **Windows 10/11 (64-bit)** e **Python 3.10+**. Per lo sviluppo e l'esecuzione dei test non sono richiesti né Microsoft Teams, né Outlook, né una GPU fisica.

```powershell
# 1. Clona il repository
git clone https://github.com/<owner>/Verbalia.git
cd Verbalia

# 2. Crea e attiva un virtual environment dedicato
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. Aggiorna pip e installa le dipendenze di runtime e sviluppo
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

---

## 2. Regole di Riservatezza e Dati di Test (OBBLIGATORIO)

Per tutelare la privacy e la sicurezza:

- **Divieto di audio reale**: è severamente vietato committare file `.wav`, `.mp3`, `.m4a` o altri campioni registrati da conversazioni reali.
- **Divieto di transcript reali**: non includere mai trascrizioni reali, verbali reali o nomi/email di persone reali nei test, nelle fixture o nei commenti.
- **Fixture sintetiche**: tutti i test devono generare dati audio (es. onde sinusoidali o silenzio PCM generati programmaticamente in `tmp_path`) e trascrizioni sintetiche fittizie.
- **Nessun dato sensibile**: non inserire mai token, credenziali, path assoluti della propria postazione o dati aziendali nel codice o nei messaggi di commit.

---

## 3. Compatibilità Windows e Standard Architetturali

- **Bootstrap-first**: `main.py` deve sempre invocare `ensure_environment()` prima di importare qualsiasi modulo applicativo (`modules.*`) o libreria di terze parti.
- **Scritture atomiche**: ogni modifica a `state.json`, transcript, blocchi MAP/REDUCE e file `.docx` deve passare per file temporanei + flush/fsync + `os.replace`.
- **Thread e COM**:
  - ogni thread deve avere un nome esplicito e controllare gli eventi di arresto (`shutdown_event`);
  - le chiamate COM (Outlook / `pycaw`) devono inizializzare `pythoncom.CoInitialize()` nel thread corrente e rilasciarlo in un blocco `finally` con `pythoncom.CoUninitialize()`.
- **Sottoprocessi**: su Windows usare sempre `windows_creation_flags()` (`CREATE_NO_WINDOW`) e timeout espliciti per evitare finestre console impreviste o processi bloccati.

---

## 4. Branch Naming e Stile dei Commit

### Naming dei Branch
Usa prefissi descrittivi:
- `feat/<descrizione-breve>` per nuove funzionalità;
- `fix/<descrizione-breve>` per correzioni di bug;
- `test/<descrizione-breve>` per aggiunta o miglioramento dei test;
- `docs/<descrizione-breve>` per aggiornamenti alla documentazione;
- `refactor/<descrizione-breve>` per miglioramenti strutturali senza cambi di contratto.

### Stile dei Messaggi di Commit
Segui il formato *Conventional Commits* sintetico e chiaro:
- `feat(recorder): add bounded queue silence compensation metrics`
- `fix(docx): handle windows reserved device filenames`
- `test(state): verify path traversal rejection on cleanup`

---

## 5. Controlli di Qualità Locali (Lint, Typecheck, Test)

Prima di aprire una Pull Request, assicurati che tutti i seguenti comandi terminino con **exit code 0**:

```powershell
# 1. Linting con Ruff
python -m ruff check .

# 2. Type checking statico con Mypy
python -m mypy config.py bootstrap.py main.py modules

# 3. Verifica compilazione bytecode
python -m compileall -q .

# 4. Esecuzione completa della test suite offline con coverage
python -m pytest --cov=modules
```

I test ordinari (`pytest`) devono poter girare interamente offline senza microfono, senza speaker, senza Teams, senza Outlook e senza server Ollama attivo. Se aggiungi un test che richiede periferiche reali, contrassegnalo con `@pytest.mark.hardware`.

---

## 6. Procedura per le Pull Request

1. Verifica che i contratti congelati in `config.py` e `modules/contracts.py` siano rispettati.
2. Aggiungi o aggiorna i test unitari/integrazione relativi alla modifica.
3. Esegui `python main.py --dry-run` per verificare l'integrità dell'intera pipeline sintetica end-to-end.
4. Compila integralmente il template della Pull Request indicando motivazione, modifiche effettuate e test eseguiti.
