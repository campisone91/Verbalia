# Changelog

Tutte le modifiche rilevanti a questo progetto sono documentate in questo file.

Il formato è basato su [Keep a Changelog](https://keepachangelog.com/it/1.1.0/),
e questo progetto aderisce al [Semantic Versioning](https://semver.org/lang/it/).

## [1.0.0] - 2026-10-28

### Added
- Registrazione concorrente all'elaborazione: a fine call la cattura si ferma subito e la sessione passa in una coda FIFO persistente servita da un solo worker (`modules/processing.py`), con retry e backoff (3 tentativi) e parcheggio dei job falliti.
- `SessionRuntime` e `SessionStageReporter` per sessione (`modules/session_runtime.py`): transizioni validate senza forzature, eventi su coda non bloccante consumati da un unico `EventWriter` (tray e storico).
- `WhisperEngine` condiviso: un solo modello e un solo thread, coda a priorità (prima i chunk del job in elaborazione), scaricamento dopo inattività.
- Policy di esecuzione `auto`: Whisper (CPU) e LLM in parallelo se il modello Ollama risulta su GPU (`/api/ps`), altrimenti sequenziale.
- Streaming delle risposte Ollama con avanzamento in token, statistiche tok/s nel log e chiamata annullabile allo shutdown.
- Stage `QUEUED`, eccezione `ShutdownRequested`, recovery automatica all'avvio delle sessioni interrotte (compreso il chunk aperto), lock `.owner.lock` per sessione, mutex di istanza singola.
- Impostazioni utente in `settings.json` (`modules/settings.py`) e storico SQLite `data/history.db` (`modules/job_registry.py`) con import una tantum da `app.log`.
- Cruscotto Tkinter `python main.py --dashboard` (Cruscotto, Storico, Impostazioni) e tray composita con badge dei job attivi.
- Prompt v2: resoconto dettagliato per sotto-argomento con target di lunghezza, blocchi MAP da 10 minuti, narrativa assemblata in modo deterministico, REDUCE gerarchico, trascrizione integrale in appendice al DOCX, output organizzato per giorno con migrazione dei file esistenti.
- Segnale "microfono in uso da Teams" nel watcher, split della sessione se durante il grace cambia il meeting Outlook in corso.

### Changed
- La call è considerata attiva con sessione audio Teams in stato Active anche in silenzio; i metadati Outlook vengono letti dopo l'avvio della registrazione, con priorità al meeting in corso e tolleranza di 10 minuti; l'agenda viene ripulita da link, ID riunione, passcode e numeri dial-in.
- Whisper resta su CPU con 6 thread; il tentativo CUDA avviene solo se CTranslate2 vede una GPU NVIDIA. Non viene più abbassata la priorità dell'intero processo: i thread di cattura e l'assembler hanno priorità superiore al normale, FFmpeg gira sotto la normale.
- `ended_at` corrisponde all'ultimo istante in cui la call era attiva (senza il grace period).

### Fixed
- Pipeline post-call che bloccava il main loop, i comandi della tray e il rilevamento della call successiva.
- Stato condiviso tra sessioni (`state.json` di una sessione aggiornato dagli errori di un'altra) e stage forzati fuori dalla state machine.
- Errori su un singolo chunk che portavano l'intera sessione in ERROR durante la registrazione.
- Logger fuori dalla gerarchia `teams_meeting_scribe`, session id dei log globale, test che scrivevano nel log di produzione.
- Scritture atomiche con nome temporaneo fisso e senza retry su `PermissionError`.
- Testo segnaposto del glossario usato come `initial_prompt` di Whisper.
- Overflow audio PortAudio non registrati; trascrizione persa a fine pipeline.

## [0.1.0] - 2026-10-23

### Added
- Bootstrap deterministico (`bootstrap.py`) con controllo versione Python e sistema operativo Windows, installazione dipendenze `pip`, protezione contro loop di riavvio (`TEAMS_SCRIBE_BOOTSTRAP_RESTARTED=1`), risoluzione e validazione di FFmpeg (PATH, `bin/ffmpeg.exe`, `winget`, download HTTPS), verifica e avvio locale di Ollama e creazione del marker `.env_ready.json`.
- Macchina a stati esplicita (`Stage`) con validazione formale delle transizioni (`ALLOWED_STAGE_TRANSITIONS`) e persistenza atomica su `state.json` tramite `StateManager`.
- Interfaccia System Tray nativa (`StageMonitorTray`) thread-safe con icone RGBA 64x64 generate dinamicamente via Pillow, notifiche non bloccanti e command queue verso l'orchestratore.
- Watcher euristico (`TeamsCallWatcher`) basato su confidence score multi-segnale (`psutil`, `pycaw`, debounce di avvio, grace period di 5 minuti, metadati opzionali da calendario Outlook via COM con `CoInitialize`/`CoUninitialize`).
- Registrazione dual-track resiliente (`DualTrackChunkRecorder`) con acquisizione microfono (`[IO]`) e WASAPI loopback (`[ALTRI]`), queue bounded anti-OOM, timeline monotona per traccia con compensazione controllata del silenzio, rotazione chunk di 15 minuti con overlap di 3 secondi, normalizzazione FFmpeg (`loudnorm` + `aresample` a 16 kHz mono PCM 16-bit), hot-swap delle periferiche e protezione anti-standby Windows (`SetThreadExecutionState`).
- Trascrizione rolling (`RollingTranscriber`) basata su `faster-whisper` con selezione automatica GPU CUDA (`large-v3-turbo`, `float16`) e fallback CPU (`medium`, `int8`), cancellazione dei WAV solo dopo verifica crittografica SHA-256 del transcript e rilascio esplicito della memoria prima della sintesi LLM.
- Pipeline di deduplicazione overlap e suddivisione temporale (`modules/transcript_utils.py`) con `SequenceMatcher` confinato alle finestre di confine chunk e preservazione rigorosa delle etichette `[IO]` e `[ALTRI]`.
- Sintesi gerarchica Map-Reduce (`OllamaMapReduceSummarizer`) verso Ollama locale (`llama3.1:8b`) con prompt versionati (`prompts/*.txt`), budget dinamico del contesto, cache idempotente dei blocchi MAP, generazione del verbale dettagliato e della bozza mail di recap, meccanismo di repair singolo e unload finale del modello (`keep_alive: 0`).
- Generatore e validatore Word (`MeetingDocxBuilder`, `validate_docx_file`) con scrittura atomica (`.tmp.docx` -> validazione -> `os.replace`), sanitizzazione filename Windows, box metadati, bozza mail, page break, verbale dettagliato e tabella Action Items.
- Orchestratore completo (`MeetingScribeApplication`) e interfaccia CLI (`main.py`) con supporto per `--auto`, `--setup-only`, `--record-now`, `--resume`, `--test-pipeline`, `--dry-run` e `--diagnostics`.
- Suite completa di test automatici offline (`pytest`), configurazione `ruff`, `mypy` e workflow GitHub Actions per CI e sicurezza.

### Changed
- Nessuna modifica rispetto a versioni precedenti (prima release pubblica `0.1.0`).

### Fixed
- Prevenzione della perdita di dati temporanei: il cleanup della directory di sessione avviene esclusivamente dopo la verifica strutturale positiva del file `.docx` in `output/`.

### Security
- Confine rigoroso delle operazioni di lettura e cancellazione file all'interno di `TEMP_DIR` con rifiuto di path esterni, path traversal, symlink o junction pericolose.
- Esclusione dal logging di contenuti sensibili (transcript integrali, prompt completi, risposte LLM, agenda e lista partecipanti).
- Verifica dell'avviso privacy e conferma esplicita dell'utente prima della prima registrazione locale.
