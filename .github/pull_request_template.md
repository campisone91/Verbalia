## Descrizione delle Modifiche

<!-- Descrivi sinteticamente il problema risolto o la funzionalità introdotta -->

## Motivazione e Contesto

<!-- Spiega perché questa modifica è necessaria e collega eventuali Issue (#...) -->

## Tipologia di Modifica

- [ ] Bug fix (modifica retrocompatibile che corregge un problema)
- [ ] Nuova funzionalità (modifica retrocompatibile che aggiunge capacità)
- [ ] Refactoring / Test / CI (nessuna modifica ai contratti runtime congelati)
- [ ] Documentazione

## Checklist di Verifica Tecnica e Sicurezza

- [ ] `python -m ruff check .` termina senza errori
- [ ] `python -m mypy config.py bootstrap.py main.py modules` termina senza errori
- [ ] `python -m compileall -q .` termina senza errori
- [ ] `python -m pytest` passa interamente offline senza hardware reale
- [ ] `python main.py --dry-run` completa l'intero ciclo e valida il DOCX finale
- [ ] Nessun file audio (`.wav`, `.mp3`), transcript reale o documento `.docx` è stato committato
- [ ] I log non espongono transcript completi, prompt integrali o dati personali
