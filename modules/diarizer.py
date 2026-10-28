from __future__ import annotations

import dataclasses
import json
import logging
import math
import wave
from pathlib import Path
from typing import Any

import numpy as np

import config
from modules.contracts import TrackLabel, TranscriptSegment, atomic_write_text

logger = logging.getLogger(__name__)


class AudioFeatureExtractor:
    """Estrattore leggero di caratteristiche vocali e embedding acustici in puro numpy."""

    @staticmethod
    def load_wav_mono_16k(wav_path: Path) -> np.ndarray:
        """Legge un file WAV PCM mono a 16 kHz e restituisce un ndarray float32 in [-1.0, 1.0]."""
        if not wav_path.is_file():
            return np.empty(0, dtype=np.float32)
        try:
            with wave.open(str(wav_path), "rb") as wf:
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                framerate = wf.getframerate()
                n_frames = wf.getnframes()
                if n_frames <= 0:
                    return np.empty(0, dtype=np.float32)
                raw_bytes = wf.readframes(n_frames)

            if sampwidth == 2:
                samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            elif sampwidth == 4:
                samples = np.frombuffer(raw_bytes, dtype=np.int32).astype(np.float32) / 2147483648.0
            elif sampwidth == 1:
                samples = (np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
            else:
                return np.empty(0, dtype=np.float32)

            if n_channels > 1:
                samples = samples.reshape(-1, n_channels).mean(axis=1)

            if framerate != 16000 and len(samples) > 0:
                # Ricampionamento lineare di fallback se differente da 16 kHz
                duration = len(samples) / float(framerate)
                target_len = int(round(duration * 16000))
                indices = np.linspace(0, len(samples) - 1, target_len)
                samples = np.interp(indices, np.arange(len(samples)), samples).astype(np.float32)

            return samples.astype(np.float32)
        except Exception as exc:
            logger.warning("Impossibile caricare WAV %s: %s", wav_path, exc)
            return np.empty(0, dtype=np.float32)

    @classmethod
    def extract_log_mel_filterbank(
        cls,
        samples: np.ndarray,
        sample_rate: int = 16000,
        n_mels: int = 80,
    ) -> np.ndarray:
        """Calcola 80 log-Mel filterbank con pre-emphasis, finestra Hann e CMN."""
        if len(samples) < 400:
            return np.empty((0, n_mels), dtype=np.float32)

        # Pre-emphasis 0.97
        emphasized = np.append(samples[0], samples[1:] - 0.97 * samples[:-1])

        frame_length = 400  # 25 ms a 16 kHz
        hop_length = 160    # 10 ms a 16 kHz
        n_fft = 512

        num_frames = 1 + int(math.floor((len(emphasized) - frame_length) / hop_length))
        if num_frames <= 0:
            return np.empty((0, n_mels), dtype=np.float32)

        # Framing con finestra Hann
        window = np.hanning(frame_length).astype(np.float32)
        frames = np.empty((num_frames, frame_length), dtype=np.float32)
        for i in range(num_frames):
            start = i * hop_length
            frames[i] = emphasized[start : start + frame_length] * window

        # Spettro di potenza
        mag_frames = np.abs(np.fft.rfft(frames, n=n_fft))
        pow_frames = (1.0 / n_fft) * (mag_frames ** 2)

        # Banco di filtri Mel
        mel_filters = cls._create_mel_filterbank(sample_rate=sample_rate, n_fft=n_fft, n_mels=n_mels)
        mel_energies = np.dot(pow_frames, mel_filters.T)
        log_mel = np.log(np.maximum(mel_energies, 1e-10)).astype(np.float32)

        # Cepstral Mean Normalization (CMN)
        mean_vector = np.mean(log_mel, axis=0, keepdims=True)
        log_mel -= mean_vector
        return log_mel

    @classmethod
    def extract_acoustic_embedding(
        cls,
        samples: np.ndarray,
        sample_rate: int = 16000,
    ) -> np.ndarray | None:
        """Estrae un embedding acustico L2-normalizzato deterministico dai frame vocali attivi."""
        min_samples = int(sample_rate * 0.30)  # minimo 300 ms
        if len(samples) < min_samples:
            return None

        # Filtro frame vocali attivi tramite energia RMS
        frame_len = 320  # 20 ms
        num_blocks = len(samples) // frame_len
        if num_blocks < 5:
            return None

        blocks = samples[: num_blocks * frame_len].reshape(num_blocks, frame_len)
        block_energies = np.sum(blocks ** 2, axis=1) / frame_len
        max_energy = float(np.max(block_energies))
        if max_energy < 1e-5:
            return None  # Tratto quasi totalmente silenzioso

        threshold = max(1e-5, max_energy * 0.05, float(np.mean(block_energies)) * 0.20)
        active_indices = np.where(block_energies >= threshold)[0]
        if len(active_indices) < 3:
            return None

        active_samples = blocks[active_indices].ravel()
        if len(active_samples) < min_samples:
            return None

        # 1. Log-Mel filterbank e MFCC (primi 20 coefficienti DCT-II)
        log_mel = cls.extract_log_mel_filterbank(active_samples, sample_rate=sample_rate, n_mels=80)
        if len(log_mel) < 3:
            return None

        mfcc = cls._dct_ii(log_mel, n_mfcc=20)
        mfcc_mean = np.mean(mfcc, axis=0)
        mfcc_std = np.std(mfcc, axis=0)

        # 2. Stima frequenza fondamentale F0 e harmonic strength
        f0, harmonic_strength = cls._estimate_pitch_and_harmonics(active_samples, sample_rate=sample_rate)
        f0_norm = np.clip((f0 - 70.0) / 330.0, 0.0, 1.0) if f0 > 0 else 0.0

        # 3. Centroide spettrale, rolloff e energie di sotto-banda
        fft_mag = np.abs(np.fft.rfft(active_samples[:4096])) if len(active_samples) >= 4096 else np.abs(np.fft.rfft(active_samples))
        centroid, rolloff = cls._spectral_centroid_and_rolloff(fft_mag, sample_rate=sample_rate)
        subbands = cls._subband_energies(fft_mag, sample_rate=sample_rate)

        # 4. Composizione vettore di embedding
        features = np.concatenate(
            [
                mfcc_mean,                               # 20
                mfcc_std,                                # 20
                np.array([f0_norm, harmonic_strength], dtype=np.float32),  # 2
                np.array([centroid / 4000.0, rolloff / 8000.0], dtype=np.float32),  # 2
                subbands,                                # 4
            ]
        ).astype(np.float32)

        norm = float(np.linalg.norm(features))
        if norm <= 1e-12:
            return None
        return (features / norm).astype(np.float32)

    @staticmethod
    def _create_mel_filterbank(sample_rate: int = 16000, n_fft: int = 512, n_mels: int = 80) -> np.ndarray:
        low_mel = 0.0
        high_mel = 2595.0 * math.log10(1.0 + (sample_rate / 2.0) / 700.0)
        mel_points = np.linspace(low_mel, high_mel, n_mels + 2)
        hz_points = 700.0 * (10.0 ** (mel_points / 2595.0) - 1.0)
        bins = np.floor((n_fft + 1) * hz_points / sample_rate).astype(int)

        num_bins = n_fft // 2 + 1
        filters = np.zeros((n_mels, num_bins), dtype=np.float32)
        for i in range(n_mels):
            b_left, b_center, b_right = bins[i], bins[i + 1], bins[i + 2]
            if b_center > b_left:
                for j in range(b_left, min(b_center, num_bins)):
                    filters[i, j] = (j - b_left) / float(b_center - b_left)
            if b_right > b_center:
                for j in range(b_center, min(b_right, num_bins)):
                    filters[i, j] = (b_right - j) / float(b_right - b_center)
        return filters

    @staticmethod
    def _dct_ii(log_mel: np.ndarray, n_mfcc: int = 20) -> np.ndarray:
        num_frames, n_mels = log_mel.shape
        n = np.arange(n_mels, dtype=np.float32)
        k = np.arange(n_mfcc, dtype=np.float32)[:, None]
        basis = np.cos((np.pi * k * (2.0 * n + 1.0)) / (2.0 * n_mels))
        return np.dot(log_mel, basis.T).astype(np.float32)

    @staticmethod
    def _estimate_pitch_and_harmonics(samples: np.ndarray, sample_rate: int = 16000) -> tuple[float, float]:
        min_lag = max(1, int(sample_rate / 400.0))  # ~40
        max_lag = int(sample_rate / 70.0)           # ~228
        if len(samples) < max_lag * 2:
            return 0.0, 0.0

        mid = len(samples) // 2
        chunk = samples[mid - max_lag : mid + max_lag]
        corr = np.correlate(chunk, chunk, mode="full")
        corr_mid = len(corr) // 2
        corr_pos = corr[corr_mid:]
        if len(corr_pos) <= max_lag or corr_pos[0] <= 1e-8:
            return 0.0, 0.0

        slice_lags = corr_pos[min_lag : max_lag + 1]
        if len(slice_lags) == 0:
            return 0.0, 0.0

        best_idx = int(np.argmax(slice_lags))
        best_lag = min_lag + best_idx
        harmonic_strength = float(slice_lags[best_idx] / corr_pos[0])
        f0 = float(sample_rate / best_lag)
        return f0, float(np.clip(harmonic_strength, 0.0, 1.0))

    @staticmethod
    def _spectral_centroid_and_rolloff(fft_mag: np.ndarray, sample_rate: int = 16000) -> tuple[float, float]:
        freqs = np.linspace(0, sample_rate / 2.0, len(fft_mag), dtype=np.float32)
        total_energy = float(np.sum(fft_mag))
        if total_energy <= 1e-8:
            return 0.0, 0.0
        centroid = float(np.sum(freqs * fft_mag) / total_energy)
        cumsum = np.cumsum(fft_mag)
        cutoff = 0.85 * total_energy
        idx = int(np.searchsorted(cumsum, cutoff))
        rolloff = float(freqs[min(idx, len(freqs) - 1)])
        return centroid, rolloff

    @staticmethod
    def _subband_energies(fft_mag: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
        freqs = np.linspace(0, sample_rate / 2.0, len(fft_mag), dtype=np.float32)
        total = float(np.sum(fft_mag)) + 1e-8
        bands = [(0.0, 500.0), (500.0, 1500.0), (1500.0, 4000.0), (4000.0, 8000.0)]
        energies: list[float] = []
        for low, high in bands:
            mask = (freqs >= low) & (freqs < high)
            energies.append(float(np.sum(fft_mag[mask]) / total))
        return np.array(energies, dtype=np.float32)


class OnnxSpeakerEmbedder:
    """Embedder speaker basato su modello ONNX con fallback trasparente ad AudioFeatureExtractor."""

    def __init__(self, model_path: Path | str | None = None) -> None:
        self.model_path = Path(model_path) if model_path is not None else config.SPEAKER_EMBEDDING_MODEL_PATH
        self._session: Any = None
        self._input_name: str = ""
        self._init_session()

    def _init_session(self) -> None:
        if not self.model_path.is_file():
            return
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(self.model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            inputs = self._session.get_inputs()
            if inputs:
                self._input_name = inputs[0].name
            logger.info("Modello ONNX speaker embedding caricato: %s", self.model_path)
        except Exception as exc:
            logger.warning("Inizializzazione ONNX speaker embedder non riuscita (%s), uso fallback acustico.", exc)
            self._session = None

    def embed(self, samples: np.ndarray, sample_rate: int = 16000) -> np.ndarray | None:
        """Calcola l'embedding L2-normalizzato per i campioni vocali forniti."""
        if len(samples) < int(sample_rate * 0.30):
            return None

        if self._session is not None and self._input_name:
            try:
                log_mel = AudioFeatureExtractor.extract_log_mel_filterbank(samples, sample_rate=sample_rate)
                if len(log_mel) >= 10:
                    inp = np.expand_dims(log_mel, axis=0).astype(np.float32)
                    outputs = self._session.run(None, {self._input_name: inp})
                    raw_emb = np.squeeze(outputs[0]).astype(np.float32)
                    norm = float(np.linalg.norm(raw_emb))
                    if norm > 1e-12:
                        return (raw_emb / norm).astype(np.float32)
            except Exception as exc:
                logger.debug("Inferenza ONNX fallita (%s), uso fallback acustico.", exc)

        return AudioFeatureExtractor.extract_acoustic_embedding(samples, sample_rate=sample_rate)


class SessionSpeakerTracker:
    """Tracciatore cross-chunk dei parlanti sulla traccia [ALTRI] con persistenza dei centroidi."""

    def __init__(
        self,
        session_dir: Path | None = None,
        enabled: bool | None = None,
        similarity_threshold: float | None = None,
        max_speakers: int | None = None,
        min_segment_sec: float | None = None,
        embedder: OnnxSpeakerEmbedder | None = None,
    ) -> None:
        self.session_dir = session_dir
        self.enabled: bool = (
            enabled if enabled is not None else getattr(config, "DIARIZATION_ENABLED_DEFAULT", True)
        )
        self.similarity_threshold: float = (
            float(similarity_threshold)
            if similarity_threshold is not None
            else getattr(config, "DIARIZATION_SIMILARITY_THRESHOLD", 0.68)
        )
        self.max_speakers: int = (
            int(max_speakers)
            if max_speakers is not None
            else getattr(config, "DIARIZATION_MAX_SPEAKERS", 8)
        )
        self.min_segment_sec: float = (
            float(min_segment_sec)
            if min_segment_sec is not None
            else getattr(config, "DIARIZATION_MIN_SEGMENT_SEC", 0.75)
        )
        self.embedder = embedder if embedder is not None else OnnxSpeakerEmbedder()

        self._centroids: list[np.ndarray] = []
        self._durations: list[float] = []
        self._load_centroids()

    @property
    def centroids_path(self) -> Path | None:
        if self.session_dir is None:
            return None
        return self.session_dir / "transcripts" / getattr(config, "SPEAKER_CENTROIDS_FILENAME", "speaker_centroids.json")

    def _load_centroids(self) -> None:
        path = self.centroids_path
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_centroids = data.get("centroids", [])
            raw_durations = data.get("durations", [])
            loaded_c: list[np.ndarray] = []
            loaded_d: list[float] = []
            for c, d in zip(raw_centroids, raw_durations):
                arr = np.array(c, dtype=np.float32)
                norm = float(np.linalg.norm(arr))
                if norm > 1e-12:
                    loaded_c.append((arr / norm).astype(np.float32))
                    loaded_d.append(float(d))
            self._centroids = loaded_c
            self._durations = loaded_d
        except Exception as exc:
            logger.warning("Impossibile caricare centroidi parlanti da %s: %s", path, exc)

    def _save_centroids(self) -> None:
        path = self.centroids_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "centroids": [c.tolist() for c in self._centroids],
                "durations": [float(d) for d in self._durations],
            }
            atomic_write_text(path, json.dumps(payload, indent=2))
        except Exception as exc:
            logger.warning("Impossibile salvare centroidi parlanti in %s: %s", path, exc)

    def assign_speakers_to_segments(
        self,
        wav_path: Path,
        segments: list[TranscriptSegment],
        chunk_offset_sec: float = 0.0,
    ) -> list[TranscriptSegment]:
        """Assegna le etichette parlante ai segmenti della traccia ALTRI."""
        if not self.enabled or not segments:
            return segments

        audio = AudioFeatureExtractor.load_wav_mono_16k(wav_path)
        sample_rate = 16000

        # Identificazione degli indici dei segmenti OTHERS
        others_indices = [i for i, seg in enumerate(segments) if seg.track == TrackLabel.OTHERS]
        if not others_indices:
            return segments

        # Mappatura temporanea: indice_segmento -> speaker_idx
        assigned_speakers: dict[int, int] = {}

        if len(audio) > 0:
            for idx in others_indices:
                seg = segments[idx]
                duration = max(0.0, seg.end_sec - seg.start_sec)
                rel_start = max(0.0, seg.start_sec - chunk_offset_sec)
                rel_end = max(0.0, seg.end_sec - chunk_offset_sec)

                start_sample = int(rel_start * sample_rate)
                end_sample = int(rel_end * sample_rate)

                if start_sample >= len(audio) or end_sample <= start_sample:
                    continue

                seg_audio = audio[start_sample:end_sample]

                if duration >= self.min_segment_sec:
                    emb = self.embedder.embed(seg_audio, sample_rate=sample_rate)
                    if emb is not None:
                        spk_idx = self._match_or_add_centroid(emb, duration)
                        assigned_speakers[idx] = spk_idx

        # 1. Eredità dai segmenti OTHERS più vicini per i segmenti brevi o senza embedding
        for idx in others_indices:
            if idx in assigned_speakers:
                continue
            seg = segments[idx]
            midpoint = (seg.start_sec + seg.end_sec) / 2.0

            best_other_idx: int | None = None
            min_dist = float("inf")
            for known_idx in others_indices:
                if known_idx in assigned_speakers:
                    known_mid = (segments[known_idx].start_sec + segments[known_idx].end_sec) / 2.0
                    dist = abs(midpoint - known_mid)
                    if dist < min_dist:
                        min_dist = dist
                        best_other_idx = known_idx

            if best_other_idx is not None:
                assigned_speakers[idx] = assigned_speakers[best_other_idx]
            elif self._centroids:
                assigned_speakers[idx] = 0

        # 2. Smoothing anti-rimbalzo: sequenze A, B, A ravvicinate dove B dura < 1.2s -> B diventa A
        for pos in range(1, len(others_indices) - 1):
            prev_idx = others_indices[pos - 1]
            curr_idx = others_indices[pos]
            next_idx = others_indices[pos + 1]

            if curr_idx not in assigned_speakers or prev_idx not in assigned_speakers or next_idx not in assigned_speakers:
                continue

            spk_prev = assigned_speakers[prev_idx]
            spk_curr = assigned_speakers[curr_idx]
            spk_next = assigned_speakers[next_idx]

            if spk_prev == spk_next and spk_curr != spk_prev:
                dur_curr = segments[curr_idx].end_sec - segments[curr_idx].start_sec
                gap = segments[next_idx].start_sec - segments[prev_idx].end_sec
                if dur_curr < 1.2 or gap < 1.5:
                    assigned_speakers[curr_idx] = spk_prev

        # 3. Collasso singolo parlante: se il totale dei centroidi nella sessione è <= 1,
        # non mostriamo [INTERLOCUTORE 1], ma lasciamo speaker=None per visualizzare [ALTRI].
        total_speakers = len(self._centroids)
        result: list[TranscriptSegment] = []
        for i, seg in enumerate(segments):
            if seg.track == TrackLabel.OTHERS and total_speakers >= 2 and i in assigned_speakers:
                spk_idx = assigned_speakers[i]
                updated_seg = dataclasses.replace(seg, speaker=f"INTERLOCUTORE {spk_idx + 1}")
                result.append(updated_seg)
            else:
                result.append(seg)

        self._save_centroids()
        return result

    def _match_or_add_centroid(self, emb: np.ndarray, duration: float) -> int:
        """Confronta l'embedding con i centroidi noti o ne crea uno nuovo."""
        if not self._centroids:
            self._centroids.append(emb)
            self._durations.append(duration)
            return 0

        sims = [float(np.dot(emb, centroid)) for centroid in self._centroids]
        best_idx = int(np.argmax(sims))
        best_sim = sims[best_idx]

        if best_sim >= self.similarity_threshold or len(self._centroids) >= self.max_speakers:
            # Aggiornamento con media pesata sulla durata
            prev_dur = self._durations[best_idx]
            new_dur = prev_dur + duration
            weighted = (self._centroids[best_idx] * prev_dur + emb * duration) / max(1e-8, new_dur)
            norm = float(np.linalg.norm(weighted))
            if norm > 1e-12:
                self._centroids[best_idx] = (weighted / norm).astype(np.float32)
            self._durations[best_idx] = new_dur
            return best_idx

        # Nuovo parlante
        self._centroids.append(emb)
        self._durations.append(duration)
        return len(self._centroids) - 1
