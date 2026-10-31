# Guida per gli agenti

## Ambito e progetto

Queste istruzioni valgono per tutto il repository. `tlapse` è una CLI Python che
acquisisce una telecamera RTSP via TCP e produce timelapse HEVC in MP4 frammentati
oppure sequenze PNG. Supporta media su finestra mobile, speedup configurabile,
rotazione a mezzanotte locale e aggiornamenti atomici dei video.

## Mappa del codice

- `src/tlapse/cli.py`: parsing e validazione di argomenti e variabili `TLAPSE_*`,
  configurazione del logging, entry point `main(argv=None)`.
- `src/tlapse/capture.py`: `FrameReceiver`, finestra mobile, campionamento,
  media dei frame, nomi senza sovrascritture, rotazione e gestione dei segnali.
- `src/tlapse/video.py`: `SafeVideoWriter`, coda limitata e worker, esecuzione
  FFmpeg, riallineamento dei frammenti MP4 e pubblicazione atomica.
- `src/tlapse/__main__.py`: avvio con `python -m tlapse`.
- `tests/test_cli.py`, `tests/test_capture.py`, `tests/test_video.py`: test
  `unittest` di configurazione, acquisizione e codifica/sicurezza.
- `tests/helpers.py`: telecamera e clock simulati con `run_timelapse`.
- `pyproject.toml`: metadati, dipendenze e comando `tlapse`; `requirements.txt`
  installa il progetto in modalità editable (`-e .`).
- `Dockerfile`, `compose.yaml`, `.dockerignore`: distribuzione container.
- `README.md`: installazione, configurazione e comportamento pubblico.

## Ambiente e comandi

Richiede Python **3.10+**, NumPy e `opencv-python-headless`. La modalità video
richiede FFmpeg con encoder `libx265`; i test video richiedono anche `ffprobe`.
Eseguire i comandi dalla radice del repository, in un ambiente virtuale:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p output
python -m tlapse --help
python -m unittest discover -s tests -t . -v
```

Test mirati:

```bash
python -m unittest tests.test_cli -v
python -m unittest tests.test_capture -v
python -m unittest tests.test_video -v
```

Non sono configurati formatter, linter o type checker: non introdurre strumenti
o dipendenze solo per uniformare una modifica. Non affidarsi a `.python-version`
per dedurre la versione Python minima; fa fede `pyproject.toml`.

## Convenzioni di modifica

- Mantenere il layout `src/`, le responsabilità dei moduli e lo stile esistente:
  indentazione a quattro spazi, nomi `snake_case`, classi `PascalCase`, docstring
  e messaggi del codice in inglese.
- Preferire libreria standard e dipendenze già presenti. Evitare refactoring
  estranei al compito e formattazioni massive.
- Aggiornare test e `README.md` quando cambia il comportamento pubblico. Per
  nuove opzioni verificare CLI, ambiente, validazione, precedenza e container.
- Non modificare manualmente `*.egg-info/`, cache, output o altri file generati.
- Controllare `git status` prima di intervenire e preservare modifiche esistenti.

## Contratti da preservare

### Configurazione e sicurezza

- Precedenza: **CLI > ambiente > default**. Un valore CLI valido deve poter
  sostituire anche un valore ambientale non valido.
- `--frames` e `--average-frames` usano `TLAPSE_AVG_FRAMES`; le altre variabili
  seguono i nomi delle opzioni con prefisso `TLAPSE_` e underscore.
- L'applicazione non carica `.env`. Il file è intenzionalmente tracciato e vuoto;
  i test verificano questa condizione. Compose definisce direttamente i parametri.
- La directory di output deve già esistere. Non sovrascrivere file con timestamp
  coincidenti: usare `output_path` e i suffissi numerici.
- Non inserire credenziali reali in codice, esempi, test o log. Usare URL fittizi
  e mantenere la redazione dell'URL nei messaggi dell'applicazione. Non avviare
  acquisizioni reali o servizi Compose per verifiche ordinarie.

### Acquisizione e temporizzazione

- Impostare le opzioni RTSP TCP prima dell'import di OpenCV e dell'apertura.
- Tenere separate ricezione, campionamento e codifica: in modalità video usano
  tre thread applicativi. La media non deve bloccare le letture della telecamera.
- Copiare i buffer della telecamera prima di conservarli; gli snapshot della
  finestra condividono riferimenti a frame che non devono più essere modificati.
- Calcolare la media solo al campionamento, senza impilare tutta la finestra:
  accumulatore `float64`, arrotondamento `np.rint`, uscita `uint8`. Con un solo
  frame evitare il calcolo floating point. Consentire finestre parziali.
- Usare il clock monotono per durata e campionamento e l'ora locale per nomi e
  rotazione. Intervallo = `speedup / fps`; i campioni persi ripetono un frame
  disponibile per preservare la durata, senza dipendere dagli FPS della sorgente.
- Dieci letture invalide consecutive fermano l'acquisizione; non c'è riconnessione
  automatica. Preservare propagazione degli errori e rilascio delle risorse.

### Video, concorrenza e persistenza

- Conservare la coda limitata e la propagazione degli errori del worker, anche
  quando la coda è piena. Non introdurre accumuli illimitati di frame.
- I batch sono dimensionati sul multiplo di FPS di un numero intero di secondi
  più vicino a 120 frame (125 a 25 FPS, 120 a 24 FPS), con gestione degli FPS
  frazionari. Mantenere timestamp continui e un solo I-frame all'inizio del batch.
- Inviare frame BGR a FFmpeg tramite pipe senza serializzare l'intero batch;
  drenare contemporaneamente stdout e stderr per evitare deadlock.
- Non ricodificare il video già pubblicato e non creare immagini intermedie in
  modalità video. Mantenere coerenti timescale e offset MP4 (`tfdt`, `mfhd`).
- Pubblicare gli aggiornamenti con file temporaneo nella directory di output,
  copia dei dati compressi, flush/fsync, `os.replace` e fsync della directory.
  Un errore prima della sostituzione deve lasciare intatto il video pubblicato.
- `close()` deve gestire batch parziali, attendere il worker ed essere idempotente.
  SIGINT/SIGTERM richiedono arresto ordinato, non interruzione del commit; ripristinare
  i gestori dei segnali. Codici di uscita: 0 successo, 1 errore, 130 interruzione.
- Ruotare a mezzanotte locale, finalizzando il video precedente senza perdere
  frame e senza creare un file vuoto per il giorno successivo. Le PNG non sono
  scritte atomicamente: non attribuire loro le garanzie dei video.

## Strategia di verifica

- Usare `run_timelapse` per test deterministici di scheduling e media, senza rete
  o attese di durata reale. Per concorrenza reale usare eventi e timeout limitati.
- Per modifiche al writer verificare anche con FFmpeg/ffprobe: numero di frame,
  timestamp, keyframe, batch parziali, FPS frazionari e continuità tra batch.
- Coprire errori del worker/encoder, conservazione del video pubblicato,
  interruzioni e rotazione quando si toccano questi percorsi.
- La classe dei test video viene saltata se mancano `ffmpeg` o `ffprobe`; la
  disponibilità di `libx265` non è verificata dal decoratore. Segnalare test
  saltati o prerequisiti mancanti, senza presentare la verifica come completa.

## Container

- L'immagine usa Alpine e pacchetti `apk` per Python, NumPy, OpenCV e FFmpeg:
  le wheel OpenCV PyPI richiedono glibc e non sono adatte a musl.
- Il container esegue `python3 -m tlapse`, con utente non root e output `/data`;
  mantenere `STOPSIGNAL SIGTERM` e tempo sufficiente per il flush allo stop.
- `.dockerignore` include solo istruzioni di build e sorgenti Python: eventuali
  nuove risorse runtime richiedono un aggiornamento esplicito del build context.
- Dopo modifiche ai sorgenti serve una ricostruzione dell'immagine; dopo modifiche
  alla configurazione serve `docker compose up`, non soltanto `restart`.
