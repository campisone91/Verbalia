# Security Policy

## Segnalazione Responsabile di Vulnerabilità

La sicurezza e la riservatezza dei dati trattati localmente sono prioritarie per `teams_meeting_scribe`.

Se individui una vulnerabilità di sicurezza (ad esempio path traversal, esecuzione arbitraria di comandi, esposizione involontaria di dati nei log, problemi nel bootstrap o nella gestione dei file temporanei):

1. **Non aprire una Issue pubblica** contenente exploit dettagliati o dati reali.
2. Utilizza la funzionalità **GitHub Security Advisories** del repository (`Security` -> `Report a vulnerability`) per inviare una segnalazione privata ai maintainer.
3. Includi nella segnalazione:
   - versione dell'applicazione e versione di Windows/Python;
   - descrizione tecnica del problema e componente coinvolto;
   - passaggi minimi riproducibili con dati sintetici;
   - impatto stimato e, se disponibile, proposta di mitigazione.

---

## Divieto Assoluto di Condivisione di Dati Reali nelle Issue

Quando apri una segnalazione di bug o una discussione su GitHub:

- **NON allegare mai** file audio reali (`.wav`, `.mp3`, `.m4a`).
- **NON incollare mai** trascrizioni di riunioni reali (`chunk_*.txt`, `merged_transcript.txt`).
- **NON pubblicare mai** output intermedi LLM (`map_block_*.md`, `detailed_minutes.md`, `recap_email.md`) o documenti `.docx` contenenti conversazioni reali.
- **NON includere mai** nomi reali di colleghi/clienti, indirizzi email, estratti del calendario Outlook o percorsi utente completi non sanitizzati.

Utilizza sempre `python main.py --dry-run` o `python main.py --diagnostics` per raccogliere informazioni diagnostiche prive di dati sensibili.

---

## Modello di Sicurezza e Rischi Principali

### 1. Dati Audio e Trascrizioni Locali (`temp/`)
- Durante una sessione attiva, i frammenti audio (`audio/*.wav`) e le trascrizioni (`transcripts/*.txt`) risiedono nella cartella `temp/session_<id>/`.
- **Mitigazione implementata**:
  - I file `.wav` vengono eliminati progressivamente non appena il relativo transcript supera la validazione e il controllo SHA-256.
  - Al termine della sessione, dopo la validazione del file `.docx` finale, l'intera cartella di sessione viene rimossa.
  - In caso di errore (per consentire `--resume`), i file restano in `temp/`: l'utente è responsabile della protezione del proprio account Windows (BitLocker, permessi NTFS, blocco schermo).

### 2. Documenti Word Generati (`output/`)
- I file `.docx` finali contengono il verbale dettagliato e la bozza mail di recap della riunione in chiaro.
- **Mitigazione implementata**:
  - I file sono salvati esclusivamente in locale nella cartella `output/` (esclusa da Git tramite `.gitignore`).
  - Il parser Markdown per la generazione del DOCX accetta solo un sottoinsieme sicuro di costrutti testuali e ignora HTML, script o oggetti incorporati.

### 3. File di Log (`logs/`)
- **Mitigazione implementata**:
  - Per impostazione predefinita (`LOG_TRANSCRIPT_CONTENT = False`), i log registrano esclusivamente metadati tecnici: stage, conteggi, durate, codici di errore e hash SHA-256.
  - Non vengono mai scritti nei log i testi delle trascrizioni, i prompt completi inviati a Ollama, le risposte integrali del modello né l'elenco dei partecipanti Outlook.

### 4. Dipendenze Esterne e Download di FFmpeg
- Il bootstrap può installare pacchetti tramite `pip` o scaricare FFmpeg tramite `winget` o archivio HTTPS.
- **Mitigazione implementata**:
  - I download diretti utilizzano esclusivamente HTTPS con timeout esplicito e file temporanei `.part`.
  - È supportata la verifica opzionale dell'hash SHA-256 (`FFMPEG_DOWNLOAD_SHA256` in `config.py`).
  - L'estrazione dell'archivio ZIP di FFmpeg verifica ogni membro contro attacchi di *Zip Slip* (path traversal) prima dell'estrazione in directory temporanea isolata.

### 5. Protezione contro Path Traversal e Cleanup Distruttivo
- **Mitigazione implementata**:
  - Prima di qualunque operazione di lettura di sessione o cancellazione (`cleanup_session_files`), `StateManager` risolve i percorsi canonici (`Path.resolve()`), verifica la discendenza stretta da `TEMP_DIR` e rifiuta symlink, junction esterne o percorsi che puntano fuori dalla directory temporanea dell'applicazione.
  - In modalità `--test-pipeline <audio_file>`, il file audio di origine viene solo letto e copiato nella sessione temporanea; un controllo esplicito finale verifica che il file originale non sia stato rimosso o alterato.
