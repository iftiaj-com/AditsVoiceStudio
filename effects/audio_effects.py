"""
effects/audio_effects.py
────────────────────────
All numpy-based audio DSP effects for Adits Voice Studio.

To add a new effect:
  1. Add a @staticmethod method below.
  2. Register it in effects/registry.py.
  3. The UI panel will pick it up automatically.
"""

import wave
import math
import random

import numpy as np
from pydub import AudioSegment

from core.audio_io import load_segment

import importlib
import importlib.util


class _OnFirstUse:
    """A module imported on first attribute access (librosa alone takes about 3 s)."""

    def __init__(self, name):
        self._name = name
        self._mod = None

    def __getattr__(self, attr):
        if self._mod is None:
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)


_LIBROSA_AVAILABLE = importlib.util.find_spec("librosa") is not None
librosa = _OnFirstUse("librosa")

_SCIPY_AVAILABLE = importlib.util.find_spec("scipy") is not None
signal = _OnFirstUse("scipy.signal")



class AudioEffects:
    """Pure numpy audio effects — no external DSP libraries required."""

    # ──────────────────────────────────────────────────────────────
    #  I/O HELPERS
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def load_wav_as_float(wav_path):
        """Load a WAV file as a float32 numpy array + sample rate."""
        audio = load_segment(wav_path)
        audio = audio.set_channels(1)
        sr = audio.frame_rate
        samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
        samples = samples / (2 ** 15)
        return samples, sr

    @staticmethod
    def save_float_as_wav(samples, sr, wav_path):
        """Save a float32 numpy array back to WAV."""
        samples = np.clip(samples, -1.0, 1.0)
        int_samples = (samples * (2 ** 15 - 1)).astype(np.int16)
        with wave.open(wav_path, 'w') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(int_samples.tobytes())

    # ──────────────────────────────────────────────────────────────
    #  UTILITY / SHARED PRIMITIVES
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def change_speed(samples, speed_factor):
        """
        Change playback speed WITHOUT affecting pitch (time-stretching).
        Uses librosa's phase-vocoder when available so the voice character
        (timbre, formants, pitch) is fully preserved at any speed.
        Falls back to naive resampling (which DOES shift pitch) if librosa
        is not installed.
        """
        if speed_factor == 1.0:
            return samples

        if _LIBROSA_AVAILABLE:
            # rate > 1 → faster, rate < 1 → slower  (matches speed_factor)
            stretched = librosa.effects.time_stretch(y=samples, rate=speed_factor)
            return stretched.astype(np.float32)

        # ── numpy fallback (OLA pitch-preserving time stretch) ──────
        try:
            return AudioEffects._time_stretch_ola(samples, speed_factor).astype(np.float32)
        except Exception:
            old_indices = np.arange(len(samples))
            new_length  = int(len(samples) / speed_factor)
            new_indices = np.linspace(0, len(samples) - 1, new_length)
            return np.interp(new_indices, old_indices, samples).astype(np.float32)

    @staticmethod
    def _time_stretch_ola(samples, rate, frame_size=512, hop_size=128):
        """Time-stretch audio using OLA (Overlap-Add) in pure NumPy."""
        if rate == 1.0 or len(samples) < frame_size:
            return samples

        # Analysis hop size
        ana_hop = int(hop_size * rate)
        if ana_hop <= 0:
            return samples

        num_samples = len(samples)
        # Hann window
        window = np.hanning(frame_size).astype(np.float32)

        # Output length
        out_len = int(num_samples / rate) + frame_size
        output = np.zeros(out_len, dtype=np.float32)
        weights = np.zeros(out_len, dtype=np.float32)

        ana_idx = 0.0
        syn_idx = 0

        while int(ana_idx) + frame_size < num_samples:
            frame = samples[int(ana_idx):int(ana_idx) + frame_size]
            output[syn_idx:syn_idx + frame_size] += frame * window
            weights[syn_idx:syn_idx + frame_size] += window

            ana_idx += ana_hop
            syn_idx += hop_size

        # Avoid division by zero
        mask = weights > 1e-5
        output[mask] /= weights[mask]
        
        return output[:syn_idx]

    @staticmethod
    def soft_clip(x, threshold=0.90):
        """Apply a soft-knee clipping function in pure NumPy to prevent clipping."""
        abs_x = np.abs(x)
        out = np.copy(x)
        mask = abs_x > threshold
        if np.any(mask):
            scale = threshold + (1.0 - threshold) * np.tanh((abs_x[mask] - threshold) / (1.0 - threshold))
            out[mask] = np.sign(x[mask]) * scale
        return out

    @staticmethod
    def pitch_shift(samples, sr, semitones=0):
        """
        Shift pitch independently of playback speed.
        Uses librosa's phase-vocoder when available for high quality.
        Falls back to a simple resampling trick otherwise.
        """
        if semitones == 0:
            return samples

        if _LIBROSA_AVAILABLE:
            shifted = librosa.effects.pitch_shift(
                y=samples, sr=sr, n_steps=semitones)
            return shifted.astype(np.float32)

        # ── numpy fallback (OLA pitch shift) ─────────────────────────
        factor  = 2.0 ** (semitones / 12.0)
        try:
            stretched = AudioEffects._time_stretch_ola(samples, 1.0 / factor)
            old_indices = np.arange(len(stretched))
            new_length = len(samples)
            new_indices = np.linspace(0, len(stretched) - 1, new_length)
            return np.interp(new_indices, old_indices, stretched).astype(np.float32)
        except Exception as e:
            # Basic fallback
            indices = np.arange(0, len(samples), factor)
            indices = indices[indices < len(samples)].astype(int)
            return samples[indices]

    @staticmethod
    def estimate_pitch(samples, sr):
        """Estimate the average fundamental frequency (F0) of speech samples using autocorrelation."""
        n = len(samples)
        if n < sr:
            return 120.0  # Fallback if too short
        
        # Analyze a 1-second segment in the middle
        start = n // 2 - sr // 2
        chunk = samples[start:start + sr]
        chunk = chunk - np.mean(chunk)
        
        # Autocorrelation
        corr = np.correlate(chunk, chunk, mode='full')
        corr = corr[len(corr)//2:]
        
        # Human pitch range: 50 Hz to 400 Hz
        min_lag = int(sr / 400)
        max_lag = int(sr / 50)
        
        if max_lag >= len(corr):
            return 120.0
            
        search_area = corr[min_lag:max_lag]
        if len(search_area) == 0:
            return 120.0
            
        peak_lag = np.argmax(search_area) + min_lag
        
        if corr[peak_lag] < 0.05 * corr[0]:
            return 120.0  # Weak peak fallback
            
        return float(sr) / peak_lag

    @staticmethod
    def normalize(samples, target_db=-3.0):
        """Normalize audio to a target dB level and remove DC offset."""
        samples = samples - np.mean(samples)
        mx = np.max(np.abs(samples))
        if mx > 0:
            target_amp = 10 ** (target_db / 20.0)
            samples = samples * (target_amp / mx)
        return samples

    @staticmethod
    def reverb(samples, sr, decay=0.3, delay_ms=40):
        """Simple comb-filter reverb."""
        delay_samples = int(sr * delay_ms / 1000)
        output = samples.copy()
        for i in range(delay_samples, len(output)):
            output[i] += decay * output[i - delay_samples]
        mx = np.max(np.abs(output))
        if mx > 0:
            output = output / mx * 0.9
        return output

    # ──────────────────────────────────────────────────────────────
    #  BACKGROUND AMBIANCE MIXER
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def add_environment_sound(samples, sr, environment="Airplane Cabin", volume=0.5):
        """Mix synthesized or file-based background ambiance into the audio."""
        import os
        n = len(samples)
        t = np.arange(n, dtype=np.float32) / sr
        bg_track = np.zeros(n, dtype=np.float32)

        # ── Noise Helper: Pink Noise with Optional Low-pass Filter ──
        def generate_pink_noise(length, cutoff_hz=None):
            white = np.random.normal(0, 1, length).astype(np.float32)
            fft   = np.fft.rfft(white)
            freqs = np.fft.rfftfreq(length, 1.0 / sr)
            freqs[0] = 1.0  # avoid div by zero
            # Pink noise power drops 3dB per octave (1/f power, 1/sqrt(f) amplitude)
            fft /= np.sqrt(freqs)
            if cutoff_hz is not None:
                # 2nd-order Butterworth-like low-pass filter in frequency-domain
                response = 1.0 / np.sqrt(1.0 + (freqs / cutoff_hz) ** 4)
                fft *= response
            pink = np.fft.irfft(fft, length)
            return pink / (np.max(np.abs(pink)) + 1e-8)

        if environment == "Airplane Cabin":
            # Deep cabin air hum (low-passed pink noise at 200 Hz for muffled cabin air flow)
            air_flow = generate_pink_noise(n, cutoff_hz=200.0)
            
            # Deep engine rumble (low-passed pink noise at 70 Hz for heavy cabin vibration)
            engine_rumble = generate_pink_noise(n, cutoff_hz=70.0)
            
            # Subdued turbine engine hum (low frequency harmonics around 75 Hz, 150 Hz, 225 Hz)
            engine_hum = (np.sin(2 * np.pi * 75 * t) * 0.4 + 
                          np.sin(2 * np.pi * 150 * t) * 0.2 + 
                          np.sin(2 * np.pi * 225 * t) * 0.1)
            
            # Combine components to build a realistic airplane cabin atmosphere
            bg_track = engine_rumble * 0.55 + air_flow * 0.35 + engine_hum * 0.10

        elif environment == "Nature/Forest":
            # Pink noise wind + intermittent crickets
            pink = generate_pink_noise(n)
            # Higher freq crickets (resonant chirps)
            chirp_carrier = np.sin(2 * np.pi * 4200 * t)
            # 3Hz amplitude modulation with a bias to make it 'chirpy'
            chirp_mod = np.clip(np.sin(2 * np.pi * 3 * t) - 0.7, 0, 1) * 10
            crickets = chirp_carrier * chirp_mod
            bg_track = pink * 0.3 + crickets * 0.15

        elif os.path.exists(environment):
            try:
                bg_audio = load_segment(environment).set_channels(1).set_frame_rate(sr)
                bg_samples = np.array(bg_audio.get_array_of_samples(), dtype=np.float32) / (2 ** 15)
                if len(bg_samples) < n:
                    repeats = int(np.ceil(n / len(bg_samples)))
                    bg_samples = np.tile(bg_samples, repeats)
                bg_track = bg_samples[:n]
            except Exception as e:
                print(f"Error loading custom ambiance: {e}")
                return samples
        else:
            return samples

        # ── Final Mixing ──────────────────────────────────────────
        # Normalize bg_track first
        mx_bg = np.max(np.abs(bg_track))
        if mx_bg > 0:
            bg_track = bg_track / mx_bg
        
        # Scale by volume (allow it to be louder — up to 0.8 at max volume)
        scale = volume * 0.8
        bg_track *= scale

        # Apply auto-ducking to the ambiance track based on speech envelope
        window_size = int(sr * 0.15)  # 150ms window
        if window_size > 0 and len(samples) > window_size:
            try:
                speech_abs = np.abs(samples)
                kernel = np.ones(window_size, dtype=np.float32) / window_size
                speech_env = np.convolve(speech_abs, kernel, mode='same')
                mx_env = np.max(speech_env)
                if mx_env > 0:
                    speech_env /= mx_env
                duck_depth = 0.50
                ducking_gain = 1.0 - (duck_depth * speech_env)
                bg_track *= ducking_gain
            except Exception as e:
                print(f"Ducking failed: {e}")

        # Mix with samples
        mix = samples + bg_track
        
        # Soft-clip instead of hard division to maintain speech presence and prevent distortion
        return AudioEffects.soft_clip(mix, threshold=0.90)

    @staticmethod
    def three_band_eq(samples, sr, low_gain=1.0, mid_gain=1.0, high_gain=1.0):
        """
        3-Band Graphic Equalizer using frequency-domain partitioning.
        low_gain, mid_gain, high_gain are linear factors (e.g. 1.0 = 0dB).
        """
        n = len(samples)
        if n == 0:
            return samples
            
        # Compute real FFT
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        
        # Define band limits
        # Bass: 0 - 250 Hz
        # Mids: 250 - 4000 Hz
        # Treble: 4000 Hz+
        low_mask = freqs < 250.0
        mid_mask = (freqs >= 250.0) & (freqs < 4000.0)
        high_mask = freqs >= 4000.0
        
        # Apply scaling factors
        fft_signal[low_mask]  *= low_gain
        fft_signal[mid_mask]  *= mid_gain
        fft_signal[high_mask] *= high_gain
        
        # Inverse FFT
        equalized = np.fft.irfft(fft_signal, n)
        
        # Normalize/clip output safely
        mx = np.max(np.abs(equalized))
        if mx > 1.0:
            equalized = equalized / mx * 0.95
            
        return equalized.astype(np.float32)

    # ──────────────────────────────────────────────────────────────
    #  EFFECTS
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _generate_synth_carrier(n_samples, sr, chord_progression=None):
        """
        Generate a lush polyphonic analog synthesizer carrier with electronic chord progressions.
        Replicates classic Daft Punk electro-funk vocoder pads (detuned saw waves + sub-bass).
        """
        t = np.arange(n_samples, dtype=np.float32) / sr
        total_duration = n_samples / sr

        # Classic Daft Punk progression in A minor / electro-funk:
        # 1. Am7   (A2, E3, A3, C4, G4)
        # 2. Fmaj7 (F2, C3, F3, A3, E4)
        # 3. Cmaj7 (C2, G2, C3, E3, B3)
        # 4. G7    (G2, D3, G3, B3, F4)
        if chord_progression is None:
            chord_progression = [
                [45, 52, 57, 60, 67],  # Am7
                [41, 48, 53, 57, 64],  # Fmaj7
                [36, 43, 48, 52, 59],  # Cmaj7
                [43, 50, 55, 59, 65],  # G7
            ]

        num_chords = len(chord_progression)
        chord_len = max(total_duration / num_chords, 1.6)

        carrier = np.zeros(n_samples, dtype=np.float32)
        detunes = [2.0 ** (-8.0 / 1200.0), 1.0, 2.0 ** (8.0 / 1200.0)]

        for idx, chord in enumerate(chord_progression):
            start_t = idx * chord_len
            end_t = min(start_t + chord_len, total_duration)
            if start_t >= total_duration:
                break

            start_idx = int(start_t * sr)
            end_idx = int(end_t * sr)
            if start_idx >= end_idx:
                continue

            chunk_t = t[start_idx:end_idx]
            chunk_sig = np.zeros_like(chunk_t)

            for note in chord:
                base_f = 440.0 * (2.0 ** ((note - 69.0) / 12.0))
                for d in detunes:
                    f = base_f * d
                    max_k = min(int(9000.0 / f), 32)
                    if max_k < 1:
                        continue
                    phase = 2 * np.pi * f * chunk_t
                    for k in range(1, max_k + 1):
                        chunk_sig += (1.0 / k) * np.sin(k * phase)

            # Sub-bass root octave foundation
            root_f = 440.0 * (2.0 ** ((chord[0] - 12 - 69.0) / 12.0))
            chunk_sig += 0.35 * np.sin(2 * np.pi * root_f * chunk_t)

            # Smooth crossfade at chord boundaries
            fade_len = min(int(sr * 0.04), (end_idx - start_idx) // 4)
            if fade_len > 0:
                chunk_sig[:fade_len] *= np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
                chunk_sig[-fade_len:] *= np.linspace(1.0, 0.0, fade_len, dtype=np.float32)

            carrier[start_idx:end_idx] += chunk_sig

        mx = np.max(np.abs(carrier))
        if mx > 0:
            carrier /= mx
        return carrier

    @staticmethod
    def _generate_daft_punk_carrier_v2(n_samples, sr, chord_progression=None):
        """
        Generate lush polyphonic French-Touch analog synthesizer carrier (Roland Juno / Moog style).
        Features the iconic Daft Punk 7th & 9th chord progression:
        F#m7 -> B9 -> Emaj9 -> C#m7 with detuned saws, pulse-width modulation & warm sub-bass.
        """
        t = np.arange(n_samples, dtype=np.float32) / sr
        total_duration = n_samples / sr

        if chord_progression is None:
            # Classic Daft Punk French-Touch progression (F#m7 - B9 - Emaj9 - C#m7)
            chord_progression = [
                [42, 49, 54, 57, 61, 64],  # F#m7  (F#2, C#3, F#3, A3, C#4, E4)
                [47, 54, 57, 61, 63, 66],  # B9    (B2, F#3, A3, C#4, D#4, F#4)
                [40, 47, 51, 54, 59, 63],  # Emaj9 (E2, B2, D#3, F#3, B3, D#4)
                [49, 56, 61, 64, 68],      # C#m7  (C#3, G#3, C#4, E4, G#4)
            ]

        num_chords = len(chord_progression)
        chord_len = max(total_duration / num_chords, 1.8)

        carrier = np.zeros(n_samples, dtype=np.float32)
        detunes = [2.0 ** (-9.0 / 1200.0), 1.0, 2.0 ** (9.0 / 1200.0)]

        for idx, chord in enumerate(chord_progression):
            start_t = idx * chord_len
            end_t = min(start_t + chord_len, total_duration)
            if start_t >= total_duration:
                break

            start_idx = int(start_t * sr)
            end_idx = int(end_t * sr)
            if start_idx >= end_idx:
                continue

            chunk_t = t[start_idx:end_idx]
            chunk_sig = np.zeros_like(chunk_t)

            for note in chord:
                base_f = 440.0 * (2.0 ** ((note - 69.0) / 12.0))
                for d in detunes:
                    f = base_f * d
                    phase = 2.0 * np.pi * f * chunk_t
                    max_k = min(int(8500.0 / f), 18)
                    for k in range(1, max_k + 1):
                        chunk_sig += (1.0 / k) * np.sin(k * phase)

            # Sub-bass root foundation
            root_f = 440.0 * (2.0 ** ((chord[0] - 12 - 69.0) / 12.0))
            chunk_sig += 0.45 * np.sin(2.0 * np.pi * root_f * chunk_t)

            # Smooth crossfade at chord boundaries
            fade_len = min(int(sr * 0.05), (end_idx - start_idx) // 4)
            if fade_len > 0:
                chunk_sig[:fade_len] *= np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
                chunk_sig[-fade_len:] *= np.linspace(1.0, 0.0, fade_len, dtype=np.float32)

            carrier[start_idx:end_idx] += chunk_sig

        mx = np.max(np.abs(carrier))
        if mx > 0:
            carrier /= mx
        return carrier

    @staticmethod
    def vocoder(samples, sr, num_bands=28, vocal_reinforce=0.22, sibilance_boost=0.25):
        """
        Polyphonic Synthesizer Vocoder — Daft Punk robotic electronic chords.
        Modulates a rich polyphonic analog synthesizer carrier (detuned saw chords)
        with the human vocal formant envelope:
          • Electronic chord progressions that speak lyrics and scripts
          • 28-channel overlapping Gaussian filterbank (80 Hz to 7600 Hz)
          • High-frequency sibilance extraction (>4200 Hz) for crisp 's' and 't' consonants
          • Vocal reinforcement pass blending natural speech presence so words are 100% intelligible
          • Analog warmth saturation and peak normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0:
            return samples

        # 1. Synthesize polyphonic analog synthesizer chord carrier
        carrier = AudioEffects._generate_synth_carrier(n, sr)

        # 2. Multiband Channel Vocoder Filterbank (Gaussian overlapping bands)
        band_edges = np.logspace(np.log10(80.0), np.log10(min(sr / 2 - 100.0, 7600.0)), num_bands + 1)

        fft_speech = np.fft.rfft(samples)
        fft_carrier = np.fft.rfft(carrier)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        output = np.zeros(n, dtype=np.float32)
        env_win_size = max(int(sr * 0.012), 1)
        env_kernel = np.ones(env_win_size, dtype=np.float32) / env_win_size

        for i in range(num_bands):
            f_low = band_edges[i]
            f_high = band_edges[i + 1]
            f_mid = (f_low + f_high) / 2.0
            bw = (f_high - f_low) * 1.25

            # Smooth Gaussian bandpass weighting
            band_weight = np.exp(-0.5 * ((freqs - f_mid) / (bw / 2.0)) ** 2)

            # Extract speech formant energy in this band
            band_speech_fft = fft_speech * band_weight
            band_speech_time = np.fft.irfft(band_speech_fft, n)

            # Envelope follower
            env = np.abs(band_speech_time)
            env_smoothed = np.convolve(env, env_kernel, mode='same')

            # Modulate filtered carrier in the same band
            band_carrier_fft = fft_carrier * band_weight
            band_carrier_time = np.fft.irfft(band_carrier_fft, n)

            output += env_smoothed * band_carrier_time

        # 3. High-Frequency Sibilance & Consonant Intelligibility Pass
        sibilance_mask = freqs > 4200.0
        if np.any(sibilance_mask):
            fft_sib = np.zeros_like(fft_speech)
            fft_sib[sibilance_mask] = fft_speech[sibilance_mask]
            sibilance_audio = np.fft.irfft(fft_sib, n)
            output += sibilance_audio * sibilance_boost

        # 4. Vocal Reinforcement Pass (crystal-clear speech articulation)
        if vocal_reinforce > 0:
            fft_reinf = np.copy(fft_speech)
            reinf_mask = (freqs >= 250.0) & (freqs <= 4500.0)
            fft_reinf[~reinf_mask] *= 0.1
            reinf_audio = np.fft.irfft(fft_reinf, n)
            output += reinf_audio * vocal_reinforce

        # 5. Analog Warmth Saturation & Peak Normalization
        output = np.tanh(output * 1.8)

        mx = np.max(np.abs(output))
        if mx > 0:
            output = (output / mx) * 0.912

        return output.astype(np.float32)

    @staticmethod
    def vocoder2(
        samples,
        sr,
        num_bands=36,
        vocal_reinforce=0.28,
        sibilance_boost=0.30,
        warmth=1.4
    ):
        """
        Vocoder 2 Voice Effect — Daft Punk polyphonic synthesizer vocoder with robotic chord
        progressions and vocal reinforcement. Uses the voice to modulate an analog synthesizer
        carrier, creating rich electronic chords that speak lyrics with crystal-clear intelligibility.
          • Polyphonic French-Touch synthesizer chords (F#m7 - B9 - Emaj9 - C#m7)
          • High-density 36-band logarithmic Mel-scale filterbank (90 Hz to 8200 Hz)
          • Analog RC envelope follower (fast transient attack & warm vowel decay)
          • Vocal reinforcement pass blending natural speech formants (220 Hz - 4800 Hz)
          • Sibilance exciter pass (>4000 Hz) for sharp 's', 't', 'k' consonant diction
          • Soft-knee analog warmth saturation and peak normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        # 1. Synthesize Daft Punk polyphonic analog chord carrier
        carrier = AudioEffects._generate_daft_punk_carrier_v2(n, sr)

        # 2. Multiband Channel Vocoder Filterbank (36 Gaussian overlapping bands)
        band_edges = np.logspace(np.log10(90.0), np.log10(min(sr / 2.0 - 100.0, 8200.0)), num_bands + 1)

        fft_speech = np.fft.rfft(samples)
        fft_carrier = np.fft.rfft(carrier)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        output = np.zeros(n, dtype=np.float32)

        # Analog RC envelope follower filter coefficients
        alpha = float(np.exp(-1.0 / (sr * 0.008)))
        b = [1.0 - alpha]
        a = [1.0, -alpha]

        for i in range(num_bands):
            f_low = band_edges[i]
            f_high = band_edges[i + 1]
            f_mid = (f_low + f_high) / 2.0
            bw = (f_high - f_low) * 1.25

            # Smooth Gaussian bandpass weighting
            band_weight = np.exp(-0.5 * ((freqs - f_mid) / (bw / 2.0)) ** 2)

            # Speech formant band
            band_speech_time = np.fft.irfft(fft_speech * band_weight, n)
            # Carrier band
            band_carrier_time = np.fft.irfft(fft_carrier * band_weight, n)

            # Fast analog RC envelope follower
            rectified = np.abs(band_speech_time)
            if _SCIPY_AVAILABLE:
                env = signal.lfilter(b, a, rectified)
            else:
                env_win = max(int(sr * 0.008), 1)
                kernel = np.ones(env_win, dtype=np.float32) / env_win
                env = np.convolve(rectified, kernel, mode='same')

            output += env * band_carrier_time

        # 3. High-Frequency Sibilance Pass (>4000 Hz) for Consonant Intelligibility
        sibilance_mask = freqs > 4000.0
        if np.any(sibilance_mask):
            fft_sib = np.zeros_like(fft_speech)
            fft_sib[sibilance_mask] = fft_speech[sibilance_mask]
            sibilance_audio = np.fft.irfft(fft_sib, n)
            output += sibilance_audio * sibilance_boost

        # 4. Vocal Reinforcement Pass (crystal-clear speech articulation)
        if vocal_reinforce > 0:
            fft_reinf = np.copy(fft_speech)
            reinf_mask = (freqs >= 220.0) & (freqs <= 4800.0)
            fft_reinf[~reinf_mask] *= 0.05
            reinf_audio = np.fft.irfft(fft_reinf, n)
            output += reinf_audio * vocal_reinforce

        # 5. Soft-Knee Analog Saturation & Output Normalization
        output = np.tanh(output * warmth)

        mx = np.max(np.abs(output))
        if mx > 0:
            output = (output / mx) * 0.912

        return output.astype(np.float32)

    daft_punk_vocoder = vocoder2
    synth_voice = vocoder2

    @staticmethod
    def glitch(samples, sr, chunk_ms=30, repeat_prob=0.15, max_repeats=4):
        """Glitch — randomly repeats small audio chunks."""
        chunk_size = int(sr * chunk_ms / 1000)
        output = []
        i = 0
        while i < len(samples):
            chunk = samples[i:i + chunk_size]
            output.append(chunk)
            if random.random() < repeat_prob:
                repeats = random.randint(1, max_repeats)
                for _ in range(repeats):
                    output.append(chunk)
            i += chunk_size
        return np.concatenate(output).astype(np.float32)

    @staticmethod
    def ring_modulator(samples, sr, freq=30.0, mix=0.7):
        """Ring modulation — Dalek-like choppy effect."""
        t = np.arange(len(samples), dtype=np.float32) / sr
        modulator = np.sin(2 * np.pi * freq * t)
        modulated = samples * modulator
        return ((1 - mix) * samples + mix * modulated).astype(np.float32)

    @staticmethod
    def pilot_radio(samples, sr, drive=1.5, presence=4.0, ptt_click=True):
        """
        Airline Pilot Intercom — authentic cockpit VHF transmission & headset boom mic voice.
        Acoustically modeled after 'enter in a relationship - pilot.mp3':
          • Strict aviation VHF / ICAO bandpass envelope (300 Hz – 3100 Hz)
          • Steep high-pass attenuation below 260 Hz eliminating cabin rumble & plosives
          • Headset boom mic presence peak at 2700 Hz (+5.5 dB) for cockpit cutting power
          • Close-mic proximity body warmth at 400 Hz (+2.5 dB)
          • Steep low-pass cutoff above 3100 Hz (-50 dB stopband)
          • Non-linear aviation preamp saturation (soft overdrive with odd & even harmonics)
          • Aircraft ALC (Automatic Level Control) dynamic compression
          • Cockpit ambient bed: 60 Hz/120 Hz avionics power inverter hum & intercom air
          • Authentic PTT (Push-To-Talk) mic key clicks at transmission start and finish
          • Soft limiter preventing digital clipping, normalized to -0.9 dBFS (0.90).
        """
        n = len(samples)
        if n == 0:
            return samples

        t = np.arange(n, dtype=np.float32) / sr

        # 1. Warm Aviation Mic Preamp Saturation (diode/transformer soft overdrive)
        driven = samples * drive
        sat = np.tanh(driven * 1.35) + 0.07 * (driven ** 2) * np.sign(driven)
        sat = sat - np.mean(sat)

        # 2. Dynamic Compression / Aircraft ALC (Automatic Level Control) BEFORE final filter
        threshold = 0.16
        ratio = 0.28
        abs_s = np.abs(sat)
        compressed = np.copy(abs_s)
        mask = abs_s > threshold
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        leveled = np.sign(sat) * compressed * 1.5

        # 3. Spectral Shaping (Frequency Domain) - Applied AFTER distortion/compression
        fft_signal = np.fft.rfft(leveled)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # A. Steep Aviation High-pass cutoff below 300 Hz (ICAO standard)
        hp_transition = (freqs >= 250.0) & (freqs < 320.0)
        if np.any(hp_transition):
            t_norm = (320.0 - freqs[hp_transition]) / 70.0
            gain_db[hp_transition] -= 38.0 * t_norm

        hp_stop = freqs < 250.0
        if np.any(hp_stop):
            gain_db[hp_stop] -= 48.0

        # B. Close-Mic Proximity Body at 380-420 Hz (+2.5 dB)
        gain_db += 2.5 * np.exp(-0.5 * ((freqs - 400.0) / 75.0) ** 2)

        # C. Headset Boom Mic Intelligibility & Projection Boost at 2700 Hz (+5.5 dB)
        gain_db += presence * np.exp(-0.5 * ((freqs - 2700.0) / 450.0) ** 2)

        # D. Steep Aviation Low-pass cutoff above 3100 Hz
        lp_transition = (freqs >= 3050.0) & (freqs < 3350.0)
        if np.any(lp_transition):
            t_norm = (freqs[lp_transition] - 3050.0) / 300.0
            gain_db[lp_transition] -= 48.0 * t_norm

        lp_stop = freqs >= 3350.0
        if np.any(lp_stop):
            gain_db[lp_stop] -= 55.0

        # Apply frequency filter
        fft_signal *= 10.0 ** (gain_db / 20.0)
        filtered = np.fft.irfft(fft_signal, n)

        # 4. Cockpit Background Ambience & Avionics Hum
        hum_60 = np.sin(2 * np.pi * 60.0 * t) * 0.003
        hum_120 = np.sin(2 * np.pi * 120.0 * t) * 0.002

        # Subtle cockpit intercom noise floor (filtered between 280 Hz and 3100 Hz)
        white = np.random.normal(0, 1, n).astype(np.float32)
        fft_noise = np.fft.rfft(white)
        noise_gain = np.zeros_like(freqs)
        noise_gain[freqs < 280.0] = -40.0
        noise_gain[freqs > 3100.0] = -45.0
        fft_noise *= 10.0 ** (noise_gain / 20.0)
        band_noise = np.fft.irfft(fft_noise, n)
        band_noise = band_noise / (np.max(np.abs(band_noise)) + 1e-8)

        win = max(int(sr * 0.05), 1)
        env = np.convolve(np.abs(filtered), np.ones(win) / win, mode='same')
        env_norm = env / (np.max(env) + 1e-8)
        cockpit_bed = (hum_60 + hum_120) + (band_noise * 0.003 * (0.35 + 0.65 * env_norm))

        output = filtered + cockpit_bed

        # 5. PTT (Push-To-Talk) Mic Click Transients (bandpassed to 1000-2800 Hz)
        if ptt_click and n > int(sr * 0.15):
            click_len = int(sr * 0.025)
            click_t = np.arange(click_len, dtype=np.float32) / sr
            click_pop = np.sin(2 * np.pi * 1800 * click_t) * np.exp(-click_t * 120) * 0.22
            click_pop += np.random.normal(0, 0.06, click_len) * np.exp(-click_t * 100)
            output[:click_len] += click_pop.astype(np.float32)

            end_len = int(sr * 0.030)
            end_t = np.arange(end_len, dtype=np.float32) / sr
            end_pop = np.sin(2 * np.pi * 1200 * end_t) * np.exp(-end_t * 90) * 0.18
            end_pop += np.random.normal(0, 0.05, end_len) * np.exp(-end_t * 80)
            output[-end_len:] += end_pop.astype(np.float32)

        # 6. Peak Limiter & Output Normalization
        abs_out = np.abs(output)
        over = abs_out > 0.85
        if np.any(over):
            output[over] = np.sign(output[over]) * (0.85 + 0.10 * np.tanh((abs_out[over] - 0.85) / 0.10))

        peak = np.max(np.abs(output))
        if peak > 0:
            output = (output / peak) * 0.90

        return output.astype(np.float32)

    pilot = pilot_radio

    @staticmethod
    def telephone(samples, sr):
        """Vintage telephone — 300–3400 Hz + carbon mic saturation + line hum."""
        n = len(samples)
        t = np.arange(n, dtype=np.float32) / sr
        samples = np.tanh(samples * 2.5)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        mask = (freqs >= 300) & (freqs <= 3400)
        fft_signal = fft_signal * mask
        nasal_mask = (freqs >= 1000) & (freqs <= 2500)
        fft_signal[nasal_mask] *= 1.5
        samples = np.fft.irfft(fft_signal, n)
        samples += np.sin(2 * np.pi * 60 * t) * 0.003
        samples += np.random.normal(0, 0.005, n).astype(np.float32)
        samples = np.clip(samples, -0.6, 0.6)
        mx = np.max(np.abs(samples))
        if mx > 0:
            samples = samples / mx * 0.85
        return samples.astype(np.float32)

    @staticmethod
    def radio(samples, sr, drive=1.5, cabinet_resonance=3.0, presence_boost=2.0, static_level=0.002):
        """
        Radio Voice — authentic AM / communications radio broadcast voice.
        Acoustically modeled after 'Radio-voice.mp3':
          • Bandpass transmission frequency envelope (280 Hz – 3600 Hz)
          • High-pass sub-bass cut below 280 Hz (clean, punchy, zero low-end rumble)
          • Radio speaker cabinet body resonance at 400 Hz (+3.0 dB) for tabletop enclosure feel
          • Radio speaker presence & cone resonance at 2200 Hz (+2.0 dB) for clear projection
          • Steep low-pass rolloff above 3500 Hz (-42 dB/decade) cutting modern sizzle
          • Warm asymmetric analog saturation (tanh + quadratic drive) modeling tube/transistor RF stage
          • Dynamic broadcast leveling keeping speech articulate, dense, and upfront
          • Subtle modulated radio carrier static floor for authentic over-the-air transmission feel
          • Transparent peak limiter and normalization to -0.9 dBFS (0.90).
        """
        n = len(samples)
        if n == 0:
            return samples

        # 1. Warm harmonic saturation (modeling radio transmitter/receiver amplifier drive)
        driven = samples * drive
        saturated = np.tanh(driven) + 0.05 * (driven ** 2) * np.sign(driven)
        saturated = saturated - np.mean(saturated)

        # 2. Parametric Spectral Shaping in Frequency Domain (Zero-phase, smooth curves)
        fft_signal = np.fft.rfft(saturated)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # A. High-pass filter below 280 Hz (smooth rolloff down to 50 Hz)
        hp_mask = freqs < 280.0
        if np.any(hp_mask):
            f_ratio = np.clip(freqs[hp_mask] / 280.0, 1e-4, 1.0)
            gain_db[hp_mask] += 30.0 * np.log10(f_ratio)

        sub_mask = freqs < 120.0
        if np.any(sub_mask):
            gain_db[sub_mask] -= 15.0

        # B. Radio Speaker Cabinet Body Resonance at 400 Hz (+3.0 dB, width 90 Hz)
        gain_db += cabinet_resonance * np.exp(-0.5 * ((freqs - 400.0) / 90.0) ** 2)

        # C. Radio Speaker Presence & Cone Resonance at 2200 Hz (+2.0 dB, width 450 Hz)
        gain_db += presence_boost * np.exp(-0.5 * ((freqs - 2200.0) / 450.0) ** 2)

        # D. Steep Low-pass Filter above 3500 Hz (authentic radio bandwidth cutoff)
        lp_mask = freqs > 3500.0
        if np.any(lp_mask):
            f_ratio = freqs[lp_mask] / 3500.0
            gain_db[lp_mask] -= 42.0 * np.log10(f_ratio)

        ultra_high = freqs > 5500.0
        if np.any(ultra_high):
            gain_db[ultra_high] -= 25.0

        # Apply EQ
        fft_signal *= 10.0 ** (gain_db / 20.0)
        filtered = np.fft.irfft(fft_signal, n)

        # 3. Broadcast Dynamic Leveler / Compressor
        threshold = 0.18
        ratio = 0.35
        abs_f = np.abs(filtered)
        compressed = np.copy(abs_f)
        mask = abs_f > threshold
        compressed[mask] = threshold + (abs_f[mask] - threshold) * ratio
        leveled = np.sign(filtered) * compressed * 1.45

        # 4. Subtle Radio AM Carrier / Static Texture
        if static_level > 0:
            white = np.random.normal(0, 1, n).astype(np.float32)
            fft_noise = np.fft.rfft(white)
            noise_gain = np.zeros_like(freqs)
            noise_gain[freqs < 280.0] = -35.0
            noise_gain[freqs > 3500.0] = -45.0
            fft_noise *= 10.0 ** (noise_gain / 20.0)
            band_noise = np.fft.irfft(fft_noise, n)
            band_noise = band_noise / (np.max(np.abs(band_noise)) + 1e-8)

            # Envelope follower: static breathes subtly with speech
            win = max(int(sr * 0.04), 1)
            env = np.convolve(np.abs(leveled), np.ones(win) / win, mode='same')
            env_norm = env / (np.max(env) + 1e-8)
            dynamic_noise = band_noise * static_level * (0.35 + 0.65 * env_norm)
            leveled += dynamic_noise

        # 5. Peak Limiter & Output Normalization
        abs_out = np.abs(leveled)
        over = abs_out > 0.82
        if np.any(over):
            leveled[over] = np.sign(leveled[over]) * (0.82 + 0.12 * np.tanh((abs_out[over] - 0.82) / 0.12))

        peak = np.max(np.abs(leveled))
        if peak > 0:
            leveled = (leveled / peak) * 0.90

        return leveled.astype(np.float32)

    radio_voice = radio

    @staticmethod
    def hyperpop(samples, sr):
        """Fast, high-pitched, glitchy pop sound."""
        fast = AudioEffects.change_speed(samples, 1.4)
        return np.clip(fast * 1.5, -1.0, 1.0)

    @staticmethod
    def melancholic(samples, sr):
        """Slow, intimate, slightly muffled sound."""
        slow = AudioEffects.change_speed(samples, 0.85)
        window = np.ones(5) / 5
        muffled = np.convolve(slow, window, mode='same')
        return (muffled * 0.85).astype(np.float32)

    @staticmethod
    def natgeo_narrator(samples, sr):
        """
        NatGeo Narrator — prestigious, deep, resonant documentary storytelling voice.
        Acoustically modeled after 'net geo voice.mp3':
          • 100% natural, anti-robotic neural audio preservation (zero phase vocoders)
          • Deep documentary chest fundamental at 110 Hz (+2.8 dB) matching warm male core
          • Storyteller proximity body at 220 Hz (+2.0 dB) for MKH 416 / U87 intimacy
          • Acoustic mud scoop at 500 Hz (-2.2 dB) eliminating boxy room reflections
          • Prestigious dialogue articulation at 3000 Hz (+1.8 dB) for refined consonant projection
          • Natural studio air shelf above 7000 Hz (+1.2 dB) for gentle breath detail
          • Smooth broadcast leveler and transparent limiter normalized to -1.0 dBFS (0.891).
        """
        n = len(samples)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        gain_db = np.zeros_like(freqs)

        # 1. Gentle sub-bass cut below 55 Hz (preserves full male fundamental down to 75 Hz)
        hp_mask = freqs < 55.0
        if np.any(hp_mask):
            rolloff = np.clip((freqs[hp_mask] - 20.0) / 35.0, 0.0, 1.0)
            gain_db[hp_mask] += (1.0 - np.sin(rolloff * np.pi / 2.0)) * -22.0

        # 2. Documentary Chest Fundamental at 110 Hz (+2.8 dB, width 35 Hz)
        gain_db += 2.8 * np.exp(-0.5 * ((freqs - 110.0) / 35.0) ** 2)

        # 3. Storyteller Proximity Body at 220 Hz (+2.0 dB, width 70 Hz)
        gain_db += 2.0 * np.exp(-0.5 * ((freqs - 220.0) / 70.0) ** 2)

        # 4. Acoustic Mud Scoop at 500 Hz (-2.2 dB, width 120 Hz)
        gain_db -= 2.2 * np.exp(-0.5 * ((freqs - 500.0) / 120.0) ** 2)

        # 5. Prestigious Articulation at 3000 Hz (+1.8 dB, width 750 Hz)
        gain_db += 1.8 * np.exp(-0.5 * ((freqs - 3000.0) / 750.0) ** 2)

        # 6. Natural Studio Air Shelf above 7000 Hz (+1.2 dB)
        shelf_mask = freqs > 7000.0
        if np.any(shelf_mask):
            shelf_t = np.clip((freqs[shelf_mask] - 7000.0) / 3500.0, 0.0, 1.0)
            gain_db[shelf_mask] += 1.2 * (0.5 - 0.5 * np.cos(shelf_t * np.pi))

        # Apply smooth linear gain curve
        fft_signal *= 10.0 ** (gain_db / 20.0)
        samples = np.fft.irfft(fft_signal, n)

        # 7. Smooth Documentary Broadcast Leveler
        threshold = 0.26
        ratio = 0.50
        abs_s = np.abs(samples)
        mask = abs_s > threshold
        compressed = np.copy(abs_s)
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        samples = np.sign(samples) * compressed * 1.16

        # Stage B: Transparent soft-knee limiter for peaks above 0.75
        abs_s = np.abs(samples)
        over = abs_s > 0.75
        if np.any(over):
            soft_x = 0.75 + 0.18 * np.tanh((abs_s[over] - 0.75) / 0.18)
            samples[over] = np.sign(samples[over]) * soft_x

        # Stage C: Peak normalization to -1.0 dBFS (0.891)
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = (samples / peak) * 0.891

        return samples.astype(np.float32)

    @staticmethod
    def seductive_male(samples, sr):
        """
        Seductive (M) — intimate, deep, velvety masculine voice with close-mic warmth.
        Acoustically modeled after 'Seductive-male.mp3':
          • 100% natural, anti-robotic neural audio preservation (zero phase vocoders)
          • Deep velvety masculine chest fundamental at 90 Hz (+3.2 dB)
          • Intimate proximity body at 185 Hz (+2.2 dB)
          • Smooth acoustic mud scoop at 650 Hz (-2.5 dB) eliminating boxiness
          • Soft whisper & consonant articulation at 3600 Hz (+1.8 dB)
          • Silky breath air sheen above 7500 Hz (+1.5 dB)
          • Transparent broadcast dialogue leveler and soft limiter normalized to -1.0 dBFS (0.891).
        """
        n = len(samples)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        gain_db = np.zeros_like(freqs)

        # 1. Gentle sub-bass cut below 50 Hz (preserves full male fundamental down to 70 Hz)
        hp_mask = freqs < 50.0
        if np.any(hp_mask):
            rolloff = np.clip((freqs[hp_mask] - 20.0) / 30.0, 0.0, 1.0)
            gain_db[hp_mask] += (1.0 - np.sin(rolloff * np.pi / 2.0)) * -22.0

        # 2. Deep Velvety Fundamental at 90 Hz (+3.2 dB, width 26 Hz)
        gain_db += 3.2 * np.exp(-0.5 * ((freqs - 90.0) / 26.0) ** 2)

        # 3. Intimate Proximity Body at 185 Hz (+2.2 dB, width 50 Hz)
        gain_db += 2.2 * np.exp(-0.5 * ((freqs - 185.0) / 50.0) ** 2)

        # 4. Smooth Acoustic Mud Scoop at 650 Hz (-2.5 dB, width 140 Hz)
        gain_db -= 2.5 * np.exp(-0.5 * ((freqs - 650.0) / 140.0) ** 2)

        # 5. Soft Whisper & Consonant Articulation at 3600 Hz (+1.8 dB, width 800 Hz)
        gain_db += 1.8 * np.exp(-0.5 * ((freqs - 3600.0) / 800.0) ** 2)

        # 6. Silky Breath Air Sheen above 7500 Hz (+1.5 dB)
        shelf_mask = freqs > 7500.0
        if np.any(shelf_mask):
            shelf_t = np.clip((freqs[shelf_mask] - 7500.0) / 3500.0, 0.0, 1.0)
            gain_db[shelf_mask] += 1.5 * (0.5 - 0.5 * np.cos(shelf_t * np.pi))

        # Apply smooth linear gain curve
        fft_signal *= 10.0 ** (gain_db / 20.0)
        samples = np.fft.irfft(fft_signal, n)

        # 7. Transparent Broadcast Dialogue Leveler (Preserves natural breathing and intimate dynamics)
        threshold = 0.24
        ratio = 0.46
        abs_s = np.abs(samples)
        mask = abs_s > threshold
        compressed = np.copy(abs_s)
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        samples = np.sign(samples) * compressed * 1.25

        # Stage B: Transparent soft-knee limiter for peaks above 0.75
        abs_s = np.abs(samples)
        over = abs_s > 0.75
        if np.any(over):
            soft_x = 0.75 + 0.18 * np.tanh((abs_s[over] - 0.75) / 0.18)
            samples[over] = np.sign(samples[over]) * soft_x

        # Stage C: Peak normalization to -1.0 dBFS (0.891)
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = (samples / peak) * 0.891

        return samples.astype(np.float32)

    @staticmethod
    def seductive_female(samples, sr):
        """
        Seductive (F) — intimate, warm, breathy sensual female voice with close-mic intimacy.
        Acoustically modeled after 'Seductive-female.mp3':
          • 100% natural, anti-robotic neural audio preservation (zero phase vocoders)
          • Sub-bass cutoff below 75 Hz (removes rumble, preserves 140Hz+ female fundamental)
          • Sultry Alto/Mezzo fundamental warmth at 195 Hz (+2.8 dB, width 38 Hz)
          • Intimate proximity body at 320 Hz (+1.8 dB, width 60 Hz)
          • Acoustic mud & nasal scoop at 800 Hz (-2.8 dB, width 180 Hz)
          • Breathy whisper & lip diction presence bell at 4800 Hz (+2.5 dB, width 900 Hz)
          • Silky breath air sheen above 8000 Hz (+2.0 dB)
          • Smooth broadcast dialogue leveler and soft limiter normalized to -1.0 dBFS (0.891).
        """
        n = len(samples)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        gain_db = np.zeros_like(freqs)

        # 1. High-pass filter below 75 Hz (smooth cosine rolloff down to 25 Hz)
        hp_mask = freqs < 75.0
        if np.any(hp_mask):
            rolloff = np.clip((freqs[hp_mask] - 25.0) / 50.0, 0.0, 1.0)
            gain_db[hp_mask] += (1.0 - np.sin(rolloff * np.pi / 2.0)) * -24.0

        # 2. Sensual Alto / Mezzo Fundamental Bell at 195 Hz (+2.8 dB, width 38 Hz)
        gain_db += 2.8 * np.exp(-0.5 * ((freqs - 195.0) / 38.0) ** 2)

        # 3. Intimate Proximity Body at 320 Hz (+1.8 dB, width 60 Hz)
        gain_db += 1.8 * np.exp(-0.5 * ((freqs - 320.0) / 60.0) ** 2)

        # 4. Acoustic Mud & Nasal Scoop at 800 Hz (-2.8 dB, width 180 Hz)
        gain_db -= 2.8 * np.exp(-0.5 * ((freqs - 800.0) / 180.0) ** 2)

        # 5. Breathy Whisper & Lip Diction Bell at 4800 Hz (+2.5 dB, width 900 Hz)
        gain_db += 2.5 * np.exp(-0.5 * ((freqs - 4800.0) / 900.0) ** 2)

        # 6. Silky Breath Air Sheen Shelf above 8000 Hz (+2.0 dB)
        shelf_mask = freqs > 8000.0
        if np.any(shelf_mask):
            shelf_t = np.clip((freqs[shelf_mask] - 8000.0) / 4000.0, 0.0, 1.0)
            gain_db[shelf_mask] += 2.0 * (0.5 - 0.5 * np.cos(shelf_t * np.pi))

        # Apply smooth linear gain curve
        fft_signal *= 10.0 ** (gain_db / 20.0)
        samples = np.fft.irfft(fft_signal, n)

        # 7. Transparent Broadcast Dialogue Leveler (Preserves intimate breathing and delicate micro-dynamics)
        threshold = 0.25
        ratio = 0.48
        abs_s = np.abs(samples)
        mask = abs_s > threshold
        compressed = np.copy(abs_s)
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        samples = np.sign(samples) * compressed * 1.22

        # Stage B: Transparent soft-knee limiter for peaks above 0.75
        abs_s = np.abs(samples)
        over = abs_s > 0.75
        if np.any(over):
            soft_x = 0.75 + 0.18 * np.tanh((abs_s[over] - 0.75) / 0.18)
            samples[over] = np.sign(samples[over]) * soft_x

        # Stage C: Peak normalization to -1.0 dBFS (0.891)
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = (samples / peak) * 0.891

        return samples.astype(np.float32)

    @staticmethod
    def seductive(samples, sr, gender="male"):
        """Sensual tone routing to gender-specific acoustic profiles."""
        if gender == "male":
            return AudioEffects.seductive_male(samples, sr)
        return AudioEffects.seductive_female(samples, sr)

    @staticmethod
    def saas_flash(samples, sr):
        """
        SaaS Flash — punchy, ultra-crisp marketing voice for social shorts.
        Acoustically modeled after 'Saas flash voice.mp3':
          • 100% natural, human vocal preservation (zero phase-vocoder or robotic artifacts)
          • Smooth parametric studio EQ (warm 180Hz chest body, 3.4kHz consonant clarity, 7kHz+ air)
          • Transparent broadcast leveling (preserves human emotional micro-dynamics)
          • Clean commercial normalization to -0.5 dBFS (0.944).
        """
        # 1. Smooth Parametric Studio EQ (Zero Gibbs ringing, smooth continuous curves)
        n = len(samples)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        gain_db = np.zeros_like(freqs)

        # High-pass filter below 70 Hz (smooth rolloff from 70 Hz down to 20 Hz)
        hp_mask = freqs < 70.0
        if np.any(hp_mask):
            rolloff = np.clip((freqs[hp_mask] - 20.0) / 50.0, 0.0, 1.0)
            gain_db[hp_mask] += (1.0 - np.sin(rolloff * np.pi / 2.0)) * -20.0

        # Bell 1: Warmth & Vocal Body at 180 Hz (+1.5 dB, wide musical Q)
        gain_db += 1.5 * np.exp(-0.5 * ((freqs - 180.0) / 70.0) ** 2)

        # Bell 2: Dip boxy room resonance at 450 Hz (-1.0 dB)
        gain_db -= 1.0 * np.exp(-0.5 * ((freqs - 450.0) / 90.0) ** 2)

        # Bell 3: Vocal Presence & Articulation at 3400 Hz (+2.5 dB, wide Q)
        # Gives that modern, articulate commercial pop without harshness
        gain_db += 2.5 * np.exp(-0.5 * ((freqs - 3400.0) / 900.0) ** 2)

        # High-shelf: Sparkle & Air above 7000 Hz (+2.0 dB)
        shelf_mask = freqs > 7000.0
        if np.any(shelf_mask):
            shelf_t = np.clip((freqs[shelf_mask] - 7000.0) / 3500.0, 0.0, 1.0)
            gain_db[shelf_mask] += 2.0 * (0.5 - 0.5 * np.cos(shelf_t * np.pi))

        # Apply smooth linear gain curve
        fft_signal *= 10.0 ** (gain_db / 20.0)
        samples = np.fft.irfft(fft_signal, n)

        # 2. Transparent Broadcast Leveler (Preserves natural human dynamics & emotion)
        threshold = 0.28
        ratio = 0.55
        abs_s = np.abs(samples)
        mask = abs_s > threshold
        compressed = np.copy(abs_s)
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        samples = np.sign(samples) * compressed * 1.18

        # Stage B: Transparent soft-saturation (prevents digital clipping on loud peaks)
        abs_s = np.abs(samples)
        over = abs_s > 0.75
        if np.any(over):
            soft_x = 0.75 + 0.20 * np.tanh((abs_s[over] - 0.75) / 0.20)
            samples[over] = np.sign(samples[over]) * soft_x

        # Stage C: Peak normalization to -0.5 dBFS (0.944)
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = (samples / peak) * 0.944

        return samples.astype(np.float32)

    @staticmethod
    def cinematic(samples, sr):
        """
        Cinematic — soft, soothing, intimate broadcast narration voice.
        Acoustically modeled after 'BLJ Voice over.mp3':
          • 100% natural, anti-robotic neural audio preservation (zero phase vocoders)
          • Deep soothing vocal warmth at 105 Hz (+2.2 dB) matching warm male fundamental
          • Intimate proximity body at 220 Hz (+1.8 dB)
          • Gentle boxy room mud cleanup at 450 Hz (-1.5 dB)
          • Sibilance / harshness softener at 3500 Hz (-1.5 dB) for velvety, relaxing tone
          • Silky breath & whisper air sheen above 7500 Hz (+1.6 dB)
          • Transparent soft-knee dialogue leveler, soft limiter, normalized to -1.0 dBFS (0.891).
        """
        n = len(samples)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        gain_db = np.zeros_like(freqs)

        # 1. Gentle sub-bass cut below 60 Hz (cleans sub-rumble, leaves low fundamental intact)
        hp_mask = freqs < 60.0
        if np.any(hp_mask):
            rolloff = np.clip((freqs[hp_mask] - 20.0) / 40.0, 0.0, 1.0)
            gain_db[hp_mask] += (1.0 - np.sin(rolloff * np.pi / 2.0)) * -22.0

        # 2. Deep Soothing Vocal Warmth at 105 Hz (+2.2 dB, width 35 Hz)
        gain_db += 2.2 * np.exp(-0.5 * ((freqs - 105.0) / 35.0) ** 2)

        # 3. Intimate Vocal Body & Proximity at 220 Hz (+1.8 dB, width 65 Hz)
        gain_db += 1.8 * np.exp(-0.5 * ((freqs - 220.0) / 65.0) ** 2)

        # 4. Boxy Mud Cleanup at 450 Hz (-1.5 dB, width 100 Hz)
        gain_db -= 1.5 * np.exp(-0.5 * ((freqs - 450.0) / 100.0) ** 2)

        # 5. Soothing Sibilance Softener at 3500 Hz (-1.5 dB, width 600 Hz)
        gain_db -= 1.5 * np.exp(-0.5 * ((freqs - 3500.0) / 600.0) ** 2)

        # 6. Silky Breath & Whisper Air Sheen above 7500 Hz (+1.6 dB)
        shelf_mask = freqs > 7500.0
        if np.any(shelf_mask):
            shelf_t = np.clip((freqs[shelf_mask] - 7500.0) / 3500.0, 0.0, 1.0)
            gain_db[shelf_mask] += 1.6 * (0.5 - 0.5 * np.cos(shelf_t * np.pi))

        # Apply smooth linear gain curve
        fft_signal *= 10.0 ** (gain_db / 20.0)
        samples = np.fft.irfft(fft_signal, n)

        # 7. Transparent Soft-Knee Dialogue Leveler (Preserves natural breathing and intimate dynamics)
        threshold = 0.26
        ratio = 0.52
        abs_s = np.abs(samples)
        mask = abs_s > threshold
        compressed = np.copy(abs_s)
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        samples = np.sign(samples) * compressed * 1.18

        # Stage B: Transparent soft-knee limiter for peaks above 0.75
        abs_s = np.abs(samples)
        over = abs_s > 0.75
        if np.any(over):
            soft_x = 0.75 + 0.18 * np.tanh((abs_s[over] - 0.75) / 0.18)
            samples[over] = np.sign(samples[over]) * soft_x

        # Stage C: Peak normalization to -1.0 dBFS (0.891)
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = (samples / peak) * 0.891

        return samples.astype(np.float32)

    @staticmethod
    def arjun(samples, sr):
        """
        Arjun (Tech Reviewer) — warm, articulate, confident YouTube-style voice.
        Acoustically modeled after 'Arjun voice.mp3':
          • 100% natural, human voice preservation (zero phase vocoder or robotic phasiness)
          • Warm male chest resonance & proximity body at 125 Hz & 220 Hz (Shure SM7B broadcast tone)
          • Mud cleanup dip at 420 Hz
          • YouTuber presence & crisp consonant definition at 3.2 kHz
          • Studio condenser air sheen above 7 kHz
          • Transparent broadcast leveling normalized to -0.8 dBFS (0.912).
        """
        n = len(samples)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        gain_db = np.zeros_like(freqs)

        # 1. Gentle sub-bass cut below 65 Hz (preserves full male fundamental down to 80 Hz)
        hp_mask = freqs < 65.0
        if np.any(hp_mask):
            rolloff = np.clip((freqs[hp_mask] - 20.0) / 45.0, 0.0, 1.0)
            gain_db[hp_mask] += (1.0 - np.sin(rolloff * np.pi / 2.0)) * -22.0

        # 2. Key Male Chest Fundamental at 125 Hz (+2.2 dB, matching Arjun's 120Hz core)
        gain_db += 2.2 * np.exp(-0.5 * ((freqs - 125.0) / 40.0) ** 2)

        # 3. Proximity Vocal Body at 220 Hz (+1.5 dB)
        gain_db += 1.5 * np.exp(-0.5 * ((freqs - 220.0) / 70.0) ** 2)

        # 4. Clean boxy mid resonance at 420 Hz (-1.2 dB)
        gain_db -= 1.2 * np.exp(-0.5 * ((freqs - 420.0) / 80.0) ** 2)

        # 5. YouTuber Presence & Articulation at 3200 Hz (+2.8 dB, wide Q)
        gain_db += 2.8 * np.exp(-0.5 * ((freqs - 3200.0) / 900.0) ** 2)

        # 6. Air & Sparkle shelf above 7000 Hz (+1.8 dB)
        shelf_mask = freqs > 7000.0
        if np.any(shelf_mask):
            shelf_t = np.clip((freqs[shelf_mask] - 7000.0) / 3500.0, 0.0, 1.0)
            gain_db[shelf_mask] += 1.8 * (0.5 - 0.5 * np.cos(shelf_t * np.pi))

        # Apply smooth linear gain curve
        fft_signal *= 10.0 ** (gain_db / 20.0)
        samples = np.fft.irfft(fft_signal, n)

        # 7. Transparent Broadcast Leveler (Brings dialogue in-your-face, preserves micro-dynamics)
        threshold = 0.26
        ratio = 0.50
        abs_s = np.abs(samples)
        mask = abs_s > threshold
        compressed = np.copy(abs_s)
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        samples = np.sign(samples) * compressed * 1.22

        # Stage B: Transparent soft-knee limiter for peaks above 0.75
        abs_s = np.abs(samples)
        over = abs_s > 0.75
        if np.any(over):
            soft_x = 0.75 + 0.20 * np.tanh((abs_s[over] - 0.75) / 0.20)
            samples[over] = np.sign(samples[over]) * soft_x

        # Stage C: Peak normalization to -0.8 dBFS (0.912)
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = (samples / peak) * 0.912

        return samples.astype(np.float32)

    @staticmethod
    def dramatic_ads(samples, sr):
        """
        Dramatic Ads — authoritative, punchy commercial voice with high broadcast sizzle.
        Acoustically modeled after 'Blink voice ad.mp3':
          • 100% natural, anti-robotic neural audio preservation (zero phase vocoders)
          • Deep commercial announcer chest fundamental at 100 Hz (+2.8 dB) matching ~99 Hz core
          • Thick vocal body & proximity at 200 Hz (+2.0 dB)
          • Clean separation scoop at 480 Hz (-2.0 dB) eliminating boxy room mud
          • Punchy dialogue projection at 2800 Hz (+2.0 dB)
          • High commercial sizzle exciter shelf above 5500 Hz (+2.5 dB)
          • Punchy broadcast compressor and transparent limiter normalized to -0.85 dBFS (0.907).
        """
        n = len(samples)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        gain_db = np.zeros_like(freqs)

        # 1. Gentle sub-bass cut below 60 Hz (preserves full male fundamental down to 80 Hz)
        hp_mask = freqs < 60.0
        if np.any(hp_mask):
            rolloff = np.clip((freqs[hp_mask] - 20.0) / 40.0, 0.0, 1.0)
            gain_db[hp_mask] += (1.0 - np.sin(rolloff * np.pi / 2.0)) * -22.0

        # 2. Commercial Announcer Fundamental at 100 Hz (+2.8 dB, width 30 Hz)
        gain_db += 2.8 * np.exp(-0.5 * ((freqs - 100.0) / 30.0) ** 2)

        # 3. Thick Vocal Body & Proximity at 200 Hz (+2.0 dB, width 60 Hz)
        gain_db += 2.0 * np.exp(-0.5 * ((freqs - 200.0) / 60.0) ** 2)

        # 4. Clean Separation Scoop at 480 Hz (-2.0 dB, width 110 Hz)
        gain_db -= 2.0 * np.exp(-0.5 * ((freqs - 480.0) / 110.0) ** 2)

        # 5. Punchy Dialogue Projection at 2800 Hz (+2.0 dB, width 700 Hz)
        gain_db += 2.0 * np.exp(-0.5 * ((freqs - 2800.0) / 700.0) ** 2)

        # 6. Commercial Sizzle Exciter Shelf above 5500 Hz (+2.5 dB)
        shelf_mask = freqs > 5500.0
        if np.any(shelf_mask):
            shelf_t = np.clip((freqs[shelf_mask] - 5500.0) / 3500.0, 0.0, 1.0)
            gain_db[shelf_mask] += 2.5 * (0.5 - 0.5 * np.cos(shelf_t * np.pi))

        # Apply smooth linear gain curve
        fft_signal *= 10.0 ** (gain_db / 20.0)
        samples = np.fft.irfft(fft_signal, n)

        # 7. Punchy Commercial Broadcast Compressor
        threshold = 0.24
        ratio = 0.45
        abs_s = np.abs(samples)
        mask = abs_s > threshold
        compressed = np.copy(abs_s)
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        samples = np.sign(samples) * compressed * 1.28

        # Stage B: Transparent soft-knee limiter for peaks above 0.75
        abs_s = np.abs(samples)
        over = abs_s > 0.75
        if np.any(over):
            soft_x = 0.75 + 0.18 * np.tanh((abs_s[over] - 0.75) / 0.18)
            samples[over] = np.sign(samples[over]) * soft_x

        # Stage C: Peak normalization to -0.85 dBFS (0.907)
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = (samples / peak) * 0.907

        return samples.astype(np.float32)

    @staticmethod
    def techy(samples, sr):
        """
        Techy — calm, articulate, deep developer tutorial voice for code explanations.
        Acoustically modeled after 'Voiceover - what this function do.mp3':
          • 100% natural, anti-robotic neural audio preservation (zero phase vocoders)
          • Deep developer chest fundamental at 98 Hz (+3.0 dB) matching ~95 Hz core
          • Warm proximity body at 190 Hz (+1.8 dB)
          • Clean acoustic scoop at 520 Hz (-2.2 dB) eliminating boxy room reflections
          • Code & syntax articulation boost at 3.2 kHz (+2.2 dB)
          • Transparent high-end air sheen above 6.5 kHz (+1.5 dB)
          • Transparent broadcast dialogue leveler and soft limiter normalized to -1.0 dBFS (0.891).
        """
        n = len(samples)
        fft_signal = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        gain_db = np.zeros_like(freqs)

        # 1. Gentle sub-bass cut below 55 Hz (preserves full male fundamental down to 75 Hz)
        hp_mask = freqs < 55.0
        if np.any(hp_mask):
            rolloff = np.clip((freqs[hp_mask] - 20.0) / 35.0, 0.0, 1.0)
            gain_db[hp_mask] += (1.0 - np.sin(rolloff * np.pi / 2.0)) * -22.0

        # 2. Deep Developer Fundamental at 98 Hz (+3.0 dB, width 28 Hz)
        gain_db += 3.0 * np.exp(-0.5 * ((freqs - 98.0) / 28.0) ** 2)

        # 3. Proximity Vocal Body at 190 Hz (+1.8 dB, width 55 Hz)
        gain_db += 1.8 * np.exp(-0.5 * ((freqs - 190.0) / 55.0) ** 2)

        # 4. Clean Acoustic Scoop at 520 Hz (-2.2 dB, width 120 Hz)
        gain_db -= 2.2 * np.exp(-0.5 * ((freqs - 520.0) / 120.0) ** 2)

        # 5. Code & Technical Syntax Articulation at 3200 Hz (+2.2 dB, width 800 Hz)
        gain_db += 2.2 * np.exp(-0.5 * ((freqs - 3200.0) / 800.0) ** 2)

        # 6. Top-End Air Sheen above 6500 Hz (+1.5 dB)
        shelf_mask = freqs > 6500.0
        if np.any(shelf_mask):
            shelf_t = np.clip((freqs[shelf_mask] - 6500.0) / 3500.0, 0.0, 1.0)
            gain_db[shelf_mask] += 1.5 * (0.5 - 0.5 * np.cos(shelf_t * np.pi))

        # Apply smooth linear gain curve
        fft_signal *= 10.0 ** (gain_db / 20.0)
        samples = np.fft.irfft(fft_signal, n)

        # 7. Transparent Broadcast Dialogue Leveler
        threshold = 0.25
        ratio = 0.48
        abs_s = np.abs(samples)
        mask = abs_s > threshold
        compressed = np.copy(abs_s)
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        samples = np.sign(samples) * compressed * 1.25

        # Stage B: Transparent soft-knee limiter for peaks above 0.75
        abs_s = np.abs(samples)
        over = abs_s > 0.75
        if np.any(over):
            soft_x = 0.75 + 0.18 * np.tanh((abs_s[over] - 0.75) / 0.18)
            samples[over] = np.sign(samples[over]) * soft_x

        # Stage C: Peak normalization to -1.0 dBFS (0.891)
        peak = np.max(np.abs(samples))
        if peak > 0:
            samples = (samples / peak) * 0.891

        return samples.astype(np.float32)

    # ──────────────────────────────────────────────────────────────
    #  ROBOTIC (HARD AUTO-TUNE / EXTREME PITCH QUANTIZATION)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _pure_numpy_pitch_track(samples, sr, hop=128, frame_len=1024, fmin=70.0, fmax=500.0):
        """Pure NumPy sliding-window autocorrelation pitch tracking fallback."""
        n = len(samples)
        n_frames = max(1, (n - frame_len) // hop)
        f0 = np.zeros(n_frames, dtype=np.float32)
        rms = np.zeros(n_frames, dtype=np.float32)
        zcr = np.zeros(n_frames, dtype=np.float32)

        min_lag = max(1, int(sr / fmax))
        max_lag = min(frame_len - 1, int(sr / fmin))

        for i in range(n_frames):
            start = i * hop
            frame = samples[start:start + frame_len]
            rms[i] = np.sqrt(np.mean(frame ** 2))
            signs = np.sign(frame)
            zcr[i] = np.sum(np.abs(signs[1:] - signs[:-1]) > 0) / (2.0 * len(frame))

            frame_zc = frame - np.mean(frame)
            corr = np.correlate(frame_zc, frame_zc, mode='full')[frame_len - 1:]
            if corr[0] > 1e-6 and max_lag > min_lag:
                search = corr[min_lag:max_lag]
                peak_idx = np.argmax(search) + min_lag
                if corr[peak_idx] > 0.25 * corr[0]:
                    f0[i] = float(sr) / peak_idx
                else:
                    f0[i] = 120.0
            else:
                f0[i] = 120.0

        return f0, rms, zcr

    @staticmethod
    def _extract_constrained_epochs(samples, sr, f0_interp, voiced_interp):
        """
        Epoch extraction with search-window constraint:
        Each successive pitch mark p_{k+1} is found by searching around p_k + T_0
        in the range [p_k + 0.75*T_0, p_k + 1.25*T_0].
        Guarantees zero octave doubling and regular glottal epochs.
        """
        n = len(samples)
        pitch_marks = []

        if _SCIPY_AVAILABLE:
            sos = signal.butter(2, [60.0, 600.0], btype='bandpass', fs=sr, output='sos')
            filtered = signal.sosfiltfilt(sos, samples)
        else:
            fft = np.fft.rfft(samples)
            freqs = np.fft.rfftfreq(n, 1.0 / sr)
            bp_mask = (freqs >= 60.0) & (freqs <= 600.0)
            fft[~bp_mask] *= 0.05
            filtered = np.fft.irfft(fft, n)

        idx = 0
        first_chunk = np.abs(filtered[:min(n, int(sr * 0.05))])
        if len(first_chunk) > 0:
            idx = int(np.argmax(first_chunk))
        pitch_marks.append(idx)

        while idx < n:
            is_v = voiced_interp[idx] if idx < n else False
            f = f0_interp[idx] if idx < n else 150.0

            if is_v and f > 50.0:
                T0 = sr / f
                min_d = int(round(0.75 * T0))
                max_d = int(round(1.25 * T0))

                w_start = idx + min_d
                w_end = min(n, idx + max_d + 1)

                if w_start < n and w_end > w_start:
                    search_region = filtered[w_start:w_end]
                    best_offset = np.argmax(search_region)
                    next_mark = w_start + best_offset
                else:
                    next_mark = idx + int(round(T0))
            else:
                next_mark = idx + int(sr * 0.010)

            if next_mark <= idx:
                next_mark = idx + int(sr * 0.005)

            pitch_marks.append(next_mark)
            idx = next_mark

        return np.array(pitch_marks, dtype=int)

    @staticmethod
    def robotic(samples, sr, scale_notes=None, presence_boost=2.5):
        """
        Robotic Voice Effect — extreme pitch quantization with zero retune speed.
        Creates the synthetic, jumping pitch effect popular in modern urban and pop music:
          • Extreme chromatic pitch quantization with 0 ms retune speed (instantaneous snapping)
          • Time-Domain Pitch-Synchronous Overlap-Add (TD-PSOLA) preserving vocal formants & vowels
          • Constrained glottal epoch tracking eliminating octave-doubling artifacts
          • Unvoiced consonant preservation keeping 's', 't', 'k', 'p' crisp & intelligible
          • Modern pop/urban presence exciter at 4.2 kHz (+2.5 dB)
          • Soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < int(sr * 0.05):
            return samples

        hop = 128
        frame_len = 1024
        fmin = 70.0
        fmax = 500.0

        # ── 1. Pitch Tracking ─────────────────────────────────────────
        if _LIBROSA_AVAILABLE:
            try:
                f0 = librosa.yin(samples, fmin=fmin, fmax=fmax, sr=sr,
                                 frame_length=frame_len, hop_length=hop,
                                 trough_threshold=0.15)
                rms = librosa.feature.rms(y=samples, frame_length=frame_len,
                                          hop_length=hop, center=True)[0]
                zcr = librosa.feature.zero_crossing_rate(y=samples,
                                                        frame_length=frame_len,
                                                        hop_length=hop, center=True)[0]
            except Exception:
                f0, rms, zcr = AudioEffects._pure_numpy_pitch_track(samples, sr, hop, frame_len, fmin, fmax)
        else:
            f0, rms, zcr = AudioEffects._pure_numpy_pitch_track(samples, sr, hop, frame_len, fmin, fmax)

        n_frames = min(len(f0), len(rms), len(zcr))
        f0 = f0[:n_frames]
        rms = rms[:n_frames]
        zcr = zcr[:n_frames]

        # ── 2. Voiced / Unvoiced Detection ───────────────────────────
        rms_thresh = max(float(np.percentile(rms, 12)) * 1.3, 0.008)
        voiced = (rms > rms_thresh) & (zcr < 0.22) & (f0 > 75.0) & (f0 < 475.0)
        if _SCIPY_AVAILABLE:
            voiced = signal.medfilt(voiced.astype(np.float32), 3) > 0.5
        else:
            padded = np.pad(voiced.astype(np.float32), 1, mode='edge')
            voiced = np.median(np.lib.stride_tricks.sliding_window_view(padded, 3), axis=-1) > 0.5

        if np.sum(voiced) < 5:
            return samples

        frame_times = np.arange(n_frames) * hop
        sample_indices = np.arange(n)
        f0_interp = np.interp(sample_indices, frame_times, f0).astype(np.float32)
        voiced_interp = np.interp(sample_indices, frame_times, voiced.astype(float)) > 0.5

        # ── 3. Extreme Pitch Quantization (Zero Retune Speed) ─────────
        f0_target = np.copy(f0_interp)
        if scale_notes is not None and len(scale_notes) > 0:
            allowed = np.array(scale_notes) % 12
        else:
            allowed = np.arange(12)

        for i in range(n):
            if voiced_interp[i] and f0_interp[i] > fmin:
                midi = 69.0 + 12.0 * np.log2(f0_interp[i] / 440.0)
                base_oct = np.floor(midi / 12.0) * 12.0
                candidates = []
                for oct_shift in [-12, 0, 12]:
                    for note in allowed:
                        candidates.append(base_oct + oct_shift + note)
                candidates = np.array(candidates)
                closest_idx = np.argmin(np.abs(candidates - midi))
                midi_target = candidates[closest_idx]

                # ZERO retune speed: target frequency snaps instantaneously
                f0_target[i] = 440.0 * (2.0 ** ((midi_target - 69.0) / 12.0))

        # ── 4. Constrained Pitch Mark Extraction ─────────────────────
        pitch_marks = AudioEffects._extract_constrained_epochs(samples, sr, f0_interp, voiced_interp)
        if len(pitch_marks) < 2:
            return samples

        # ── 5. TD-PSOLA Formant-Preserving Overlap-Add Resynthesis ────
        out = np.zeros(n + int(sr * 0.1), dtype=np.float32)
        weights = np.zeros(len(out), dtype=np.float32)

        syn_pos = float(pitch_marks[0])

        while int(syn_pos) < n:
            pos_int = int(syn_pos)
            is_v = voiced_interp[pos_int] if pos_int < n else False

            k = np.argmin(np.abs(pitch_marks - pos_int))
            ana_pos = pitch_marks[k]

            if is_v:
                targ_f0 = f0_target[pos_int]
                syn_period = int(round(sr / targ_f0))
                syn_period = max(int(sr / 500), min(int(sr / 60), syn_period))
            else:
                if 0 < k < len(pitch_marks) - 1:
                    syn_period = int((pitch_marks[k + 1] - pitch_marks[k - 1]) / 2)
                else:
                    syn_period = int(sr * 0.010)
                syn_period = max(int(sr / 500), min(int(sr / 60), syn_period))

            win_len = 2 * syn_period + 1
            window = np.hanning(win_len).astype(np.float32)

            grain_start = ana_pos - syn_period
            grain_end = ana_pos + syn_period + 1

            orig_start = max(0, grain_start)
            orig_end = min(n, grain_end)

            w_start = orig_start - grain_start
            w_end = w_start + (orig_end - orig_start)

            if orig_end > orig_start:
                grain = samples[orig_start:orig_end] * window[w_start:w_end]
                out_start = pos_int - syn_period + (orig_start - grain_start)
                out_end = out_start + len(grain)

                if out_start >= 0 and out_end <= len(out):
                    out[out_start:out_end] += grain
                    weights[out_start:out_end] += window[w_start:w_end]

            syn_pos += syn_period

        valid = weights > 1e-4
        out[valid] /= weights[valid]

        # ── 6. Unvoiced Consonant Preservation Blend ─────────────────
        unvoiced_mask = ~voiced_interp
        if _SCIPY_AVAILABLE:
            unvoiced_smooth = signal.medfilt(unvoiced_mask.astype(np.float32), 15)
        else:
            win_size = 15
            padded = np.pad(unvoiced_mask.astype(np.float32), win_size // 2, mode='edge')
            unvoiced_smooth = np.convolve(padded, np.ones(win_size) / win_size, mode='valid')
        final_out = out[:n] * (1.0 - unvoiced_smooth) + samples * unvoiced_smooth

        # ── 7. Modern Pop / Urban Vocal Polish ───────────────────────
        fft_signal = np.fft.rfft(final_out)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # Gentle sub-bass cut below 65 Hz
        hp_mask = freqs < 65.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 18.0 * (1.0 - freqs[hp_mask] / 65.0)

        # Urban / Pop vocal presence exciter at 4.2 kHz (+2.5 dB)
        if presence_boost > 0:
            gain_db += presence_boost * np.exp(-0.5 * ((freqs - 4200.0) / 1100.0) ** 2)

        fft_signal *= 10.0 ** (gain_db / 20.0)
        polished = np.fft.irfft(fft_signal, n)

        # Soft-knee peak limiter
        abs_p = np.abs(polished)
        over = abs_p > 0.80
        if np.any(over):
            polished[over] = np.sign(polished[over]) * (0.80 + 0.12 * np.tanh((abs_p[over] - 0.80) / 0.12))

        # Peak normalization to -0.8 dBFS (0.912)
        mx = np.max(np.abs(polished))
        if mx > 0:
            polished = (polished / mx) * 0.912

        return polished.astype(np.float32)

    robotic_voice = robotic
    hard_tune = robotic

    # ──────────────────────────────────────────────────────────────
    #  DEMONIC (OCTAVE DOWN / DARK MENACING AD-LIB VOICE)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def demonic(samples, sr, semitones=-12, demonic_grit=1.4, whisper_layer=0.25, cavern_reverb=0.20):
        """
        Demonic Voice Effect — pitch down an octave for a dark, menacing demonic voice.
        Alters the overall pitch of the voice up or down independently of the tempo:
          • Deep octave-down pitch shift (default -12 semitones) with dual detuned chorusing
          • Asymmetric non-linear throat drive generating evil guttural harmonics and growl
          • Sub-bass abyssal resonance at 70 Hz (+4.0 dB) and chest growl at 140 Hz (+3.0 dB)
          • Eerie whisper sibilance preservation (>3.2 kHz) keeping words crisp and intelligible
          • Subterranean dark cavern reverberation for hellish acoustic space
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < int(sr * 0.05):
            return samples

        # 1. Pitch Shift independently of tempo (default -12 semitones / 1 octave down)
        if abs(semitones) > 0.01:
            pitched_main = AudioEffects.pitch_shift(samples, sr, semitones=semitones)
            # Subtle detuned sub-layer (-12.25 semitones) for the double-demon chorusing effect
            detuned_layer = AudioEffects.pitch_shift(samples, sr, semitones=semitones - 0.25)
            demon_core = 0.70 * pitched_main + 0.30 * detuned_layer
        else:
            demon_core = samples.copy()

        # 2. Eerie High-Frequency Sibilance & Whisper Layer
        # Blends high-passed original speech (>3.2 kHz) so consonants ('s', 't', 'k', 'p')
        # remain 100% intelligible while creating the terrifying "whispering demon within the beast" feel.
        if whisper_layer > 0:
            fft_orig = np.fft.rfft(samples)
            freqs = np.fft.rfftfreq(n, 1.0 / sr)
            whisper_mask = freqs > 3200.0
            fft_whisper = np.zeros_like(fft_orig)
            fft_whisper[whisper_mask] = fft_orig[whisper_mask]
            whisper_audio = np.fft.irfft(fft_whisper, n)
            demon_core += whisper_layer * whisper_audio

        # 3. Demonic Throat Drive & Harmonic Saturation (evil rasp / growl)
        if demonic_grit > 1.0:
            driven = demon_core * demonic_grit
            sat = np.tanh(driven) + 0.08 * (driven ** 2) * np.sign(driven)
            sat = sat - np.mean(sat)
        else:
            sat = demon_core

        # 4. Spectral Sculpting (Dark Demonic EQ)
        fft_signal = np.fft.rfft(sat)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # Sub-bass cutoff below 35 Hz (cleans sub-rumble)
        hp_mask = freqs < 35.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 24.0 * (1.0 - freqs[hp_mask] / 35.0)

        # Abyssal Sub-Bass Resonance at 70 Hz (+4.0 dB, width 28 Hz)
        gain_db += 4.0 * np.exp(-0.5 * ((freqs - 70.0) / 28.0) ** 2)

        # Guttural Chest Growl at 140 Hz (+3.0 dB, width 45 Hz)
        gain_db += 3.0 * np.exp(-0.5 * ((freqs - 140.0) / 45.0) ** 2)

        # Boxy Mud Scoop at 450 Hz (-2.5 dB, width 120 Hz)
        gain_db -= 2.5 * np.exp(-0.5 * ((freqs - 450.0) / 120.0) ** 2)

        # Menacing Throat Bite & Articulation at 2200 Hz (+2.2 dB, width 500 Hz)
        gain_db += 2.2 * np.exp(-0.5 * ((freqs - 2200.0) / 500.0) ** 2)

        # Dark Underworld Atmosphere (gentle high-frequency rolloff above 6000 Hz)
        lp_mask = freqs > 6000.0
        if np.any(lp_mask):
            gain_db[lp_mask] -= 3.5 * np.log10(np.clip(freqs[lp_mask] / 6000.0, 1.0, 4.0))

        fft_signal *= 10.0 ** (gain_db / 20.0)
        shaped = np.fft.irfft(fft_signal, n)

        # 5. Abyssal Cavern Reverb (Subterranean Void Ambiance)
        if cavern_reverb > 0:
            delay_1 = int(sr * 0.048)  # 48ms primary reflection
            delay_2 = int(sr * 0.082)  # 82ms cavern reflection
            rev = np.copy(shaped)
            for i in range(delay_1, len(rev)):
                rev[i] += 0.28 * rev[i - delay_1]
            for i in range(delay_2, len(rev)):
                rev[i] += 0.18 * rev[i - delay_2]
            # Low-pass filter the reverb wet path (dark warm cavern)
            fft_rev = np.fft.rfft(rev)
            rev_dark = np.zeros_like(freqs)
            rev_dark[freqs > 2500.0] = -12.0
            fft_rev *= 10.0 ** (rev_dark / 20.0)
            dark_rev = np.fft.irfft(fft_rev, n)
            output = (1.0 - cavern_reverb) * shaped + cavern_reverb * dark_rev
        else:
            output = shaped

        # 6. Peak Limiter & Output Normalization
        abs_out = np.abs(output)
        over = abs_out > 0.80
        if np.any(over):
            output[over] = np.sign(output[over]) * (0.80 + 0.12 * np.tanh((abs_out[over] - 0.80) / 0.12))

        peak = np.max(np.abs(output))
        if peak > 0:
            output = (output / peak) * 0.912

        return output.astype(np.float32)

    demonic_voice = demonic
    adlib_pitch = demonic

    # ──────────────────────────────────────────────────────────────
    #  DEMONIC 2 (SINGLE UNIFIED ENTITY / CLEAR WORD ARTICULATION)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_spectral_envelope_1d(mag, sr, smooth_hz=400.0):
        """Extract smooth vocal tract formant envelope using zero-phase filtering in pure NumPy."""
        n = len(mag)
        bin_hz = (sr / 2.0) / max(1, n)
        win_bins = int(smooth_hz / max(1e-4, bin_hz))
        if win_bins % 2 == 0:
            win_bins += 1
        win_bins = max(11, win_bins)

        log_mag = np.log(np.maximum(mag, 1e-6))
        win = np.hanning(win_bins)
        win /= np.sum(win)

        pad_len = win_bins // 2
        padded = np.pad(log_mag, pad_len, mode='edge')
        smoothed_log = np.convolve(padded, win, mode='valid')
        return np.exp(smoothed_log)

    @staticmethod
    def demonic2(samples, sr, semitones=-12, throat_drive=1.20, mud_scoop=3.5, bite=3.0):
        """
        Demonic 2 (Clear Articulation) — single unified demonic voice with crystal-clear word comprehension.
        Maintains 100% demonic octave-down depth while eliminating dual-voice / ghosting artifacts:
          • Pure single-entity voice: NO normal voice blended in (100% demonic pitch down an octave)
          • Spectral Formant Preservation: restores natural vocal tract vowel formants onto the pitch-shifted voice
          • Consonant Sibilance Gating: keeps unvoiced consonants ('s', 't', 'k', 'p') crisp and distinct
          • Guttural Throat Drive: adds evil vocal cord rasp directly to the single voice
          • Deep Anti-Mud Scoop: cuts out 420 Hz boxy room mud so words cut through cleanly
          • Articulation Bell at 2600 Hz (+3.0 dB) ensuring every syllable is clearly understood
          • Soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < int(sr * 0.05):
            return samples

        # 1. Detect Voiced vs Unvoiced frames
        hop = 256
        frame_len = 1024
        fmin = 70.0
        fmax = 500.0

        if _LIBROSA_AVAILABLE:
            try:
                f0 = librosa.yin(samples, fmin=fmin, fmax=fmax, sr=sr, frame_length=frame_len, hop_length=hop)
                rms = librosa.feature.rms(y=samples, frame_length=frame_len, hop_length=hop, center=True)[0]
                zcr = librosa.feature.zero_crossing_rate(y=samples, frame_length=frame_len, hop_length=hop, center=True)[0]
            except Exception:
                f0, rms, zcr = AudioEffects._pure_numpy_pitch_track(samples, sr, hop, frame_len, fmin, fmax)
        else:
            f0, rms, zcr = AudioEffects._pure_numpy_pitch_track(samples, sr, hop, frame_len, fmin, fmax)

        n_frames = min(len(f0), len(rms), len(zcr))
        f0 = f0[:n_frames]
        rms = rms[:n_frames]
        zcr = zcr[:n_frames]

        rms_thresh = max(float(np.percentile(rms, 15)) * 1.2, 0.008)
        voiced = (rms > rms_thresh) & (zcr < 0.22) & (f0 > 75.0) & (f0 < 475.0)
        if _SCIPY_AVAILABLE:
            voiced = signal.medfilt(voiced.astype(np.float32), 3) > 0.5
        else:
            padded = np.pad(voiced.astype(np.float32), 1, mode='edge')
            voiced = np.median(np.lib.stride_tricks.sliding_window_view(padded, 3), axis=-1) > 0.5

        frame_times = np.arange(n_frames) * hop
        sample_indices = np.arange(n)
        voiced_interp = np.interp(sample_indices, frame_times, voiced.astype(float)) > 0.5

        # 2. Pitch Shift: 100% of the speech signal is shifted down an octave (-12 semitones)
        # ZERO normal unpitched voice is mixed in — guarantees ONE SINGLE DEMONIC ENTITY
        pitched = AudioEffects.pitch_shift(samples, sr, semitones=semitones)

        # 3. Spectral Formant Preservation (Vocal Tract Resonances)
        # Aligns the pitched signal's vowel formant heights to the original human vocal tract envelope
        fft_orig = np.fft.rfft(samples)
        fft_pitch = np.fft.rfft(pitched)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        mag_orig = np.abs(fft_orig)
        mag_pitch = np.abs(fft_pitch)

        env_orig = AudioEffects._extract_spectral_envelope_1d(mag_orig, sr, smooth_hz=400.0)
        env_pitch = AudioEffects._extract_spectral_envelope_1d(mag_pitch, sr, smooth_hz=400.0)

        # Formant transfer ratio: restores vowel phonetic clarity
        formant_ratio = (env_orig + 1e-4) / (env_pitch + 1e-4)
        formant_ratio = np.clip(formant_ratio, 0.35, 3.5)

        # Apply formant restoration above 80 Hz to keep deep sub-bass demon pitch intact
        f_weight = np.clip((freqs - 80.0) / 120.0, 0.0, 1.0)
        applied_formant = 1.0 + f_weight * (formant_ratio - 1.0)

        fft_corrected = fft_pitch * applied_formant
        demon_voice = np.fft.irfft(fft_corrected, n)

        # 4. Consonant Preservation (Zero Sibilance Muffling)
        # Unvoiced phonemes ('s', 't', 'k', 'p') have no pitch and are kept natural
        unvoiced_mask = ~voiced_interp
        if _SCIPY_AVAILABLE:
            unvoiced_smooth = signal.medfilt(unvoiced_mask.astype(np.float32), 15)
        else:
            win_size = 15
            pad_unv = np.pad(unvoiced_mask.astype(np.float32), win_size // 2, mode='edge')
            unvoiced_smooth = np.convolve(pad_unv, np.ones(win_size) / win_size, mode='valid')

        # SINGLE UNIFIED VOICE:
        # 100% demon voice on vowels, crisp natural articulation on consonants
        unified = demon_voice * (1.0 - unvoiced_smooth) + samples * unvoiced_smooth

        # 5. Demonic Throat Saturation (Menacing rasp on the single voice)
        if throat_drive > 1.0:
            driven = unified * throat_drive
            sat = np.tanh(driven) + 0.04 * (driven ** 2) * np.sign(driven)
            sat = sat - np.mean(sat)
        else:
            sat = unified

        # 6. Demonic Parametric Clarity EQ
        fft_sig = np.fft.rfft(sat)
        gain_db = np.zeros_like(freqs)

        # Sub-bass filter below 40 Hz
        hp_mask = freqs < 40.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 22.0 * (1.0 - freqs[hp_mask] / 40.0)

        # Abyssal Sub-Bass Resonance at 75 Hz (+3.5 dB)
        gain_db += 3.5 * np.exp(-0.5 * ((freqs - 75.0) / 25.0) ** 2)

        # Deep Chest Growl at 135 Hz (+2.5 dB)
        gain_db += 2.5 * np.exp(-0.5 * ((freqs - 135.0) / 35.0) ** 2)

        # Anti-Mud Scoop at 420 Hz (-mud_scoop dB) — eliminates muffling and boxiness!
        gain_db -= mud_scoop * np.exp(-0.5 * ((freqs - 420.0) / 110.0) ** 2)

        # Consonant Articulation Bell at 2600 Hz (+bite dB) — brings word diction forward
        gain_db += bite * np.exp(-0.5 * ((freqs - 2600.0) / 600.0) ** 2)

        # Top-End Air Shelf above 6500 Hz (+1.8 dB)
        shelf_mask = freqs > 6500.0
        if np.any(shelf_mask):
            gain_db[shelf_mask] += 1.8 * np.clip((freqs[shelf_mask] - 6500.0) / 3000.0, 0.0, 1.0)

        fft_sig *= 10.0 ** (gain_db / 20.0)
        polished = np.fft.irfft(fft_sig, n)

        # 7. Soft Limiter & Normalization to -0.8 dBFS (0.912)
        abs_p = np.abs(polished)
        over = abs_p > 0.80
        if np.any(over):
            polished[over] = np.sign(polished[over]) * (0.80 + 0.12 * np.tanh((abs_p[over] - 0.80) / 0.12))

        mx = np.max(np.abs(polished))
        if mx > 0:
            polished = (polished / mx) * 0.912

        return polished.astype(np.float32)

    demonic_2 = demonic2
    demonic_clear = demonic2

    # ──────────────────────────────────────────────────────────────
    #  HARMONIZER (AUTOMATIC VOCAL CHORDS & MASSIVE CHOIR)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_modulated_delay(sig, sr, base_delay_ms, mod_depth_ms, mod_rate_hz, phase_offset=0.0):
        """
        Apply a fractional time-varying delay line with sinusoidal LFO modulation.
        Generates authentic Doppler micro-pitch chorusing and ensemble timing spread in pure NumPy.
        """
        n = len(sig)
        if n == 0:
            return sig
        t = np.arange(n, dtype=np.float32) / sr
        delay_ms = base_delay_ms + mod_depth_ms * np.sin(2.0 * np.pi * mod_rate_hz * t + phase_offset)
        delay_samples = delay_ms * (sr / 1000.0)
        indices = np.arange(n, dtype=np.float32) - delay_samples
        indices = np.clip(indices, 0.0, float(n - 1))
        idx_floor = indices.astype(int)
        idx_ceil = np.clip(idx_floor + 1, 0, n - 1)
        frac = indices - idx_floor
        return (1.0 - frac) * sig[idx_floor] + frac * sig[idx_ceil]

    @staticmethod
    def _choir_hall_reverb(sig, sr, wet=0.30, decay=0.45):
        """
        Lush multi-tap cathedral/concert hall reverberation with warm stone high-frequency damping.
        Creates expansive acoustic space specifically tailored for choral vocal sustains.
        """
        n = len(sig)
        if n == 0 or wet <= 0.0:
            return sig

        # Multi-tap prime-number reflections for smooth, dense diffusion
        taps = [
            int(sr * 0.029),  # 29ms early reflection
            int(sr * 0.043),  # 43ms
            int(sr * 0.067),  # 67ms
            int(sr * 0.097),  # 97ms
            int(sr * 0.139),  # 139ms late reflection
        ]
        gains = [0.34, 0.28, 0.22, 0.16, 0.12]

        rev = np.zeros(n, dtype=np.float32)
        for d, g in zip(taps, gains):
            if d < n:
                rev[d:] += g * sig[:-d]

        # Feedback tail
        tail_tap = int(sr * 0.115)
        if tail_tap < n:
            for i in range(tail_tap, n):
                rev[i] += decay * 0.38 * rev[i - tail_tap]

        # High-frequency damping above 3600 Hz on wet path (warm cathedral acoustics)
        fft_rev = np.fft.rfft(rev)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        damp_mask = freqs > 3600.0
        if np.any(damp_mask):
            ratio = freqs[damp_mask] / 3600.0
            damp_db = -12.0 * np.log10(ratio)
            fft_rev[damp_mask] *= 10.0 ** (damp_db / 20.0)
        damped_rev = np.fft.irfft(fft_rev, n)

        return (1.0 - wet) * sig + wet * damped_rev

    @staticmethod
    def harmonizer(
        samples,
        sr,
        chord_preset="choir",
        harmony_level=0.65,
        lead_level=0.85,
        choir_spread=1.0,
        hall_reverb=0.32
    ):
        """
        Harmonizer Voice Effect — duplicates the voice and generates automatic musical chords,
        turning a single voice into a massive, lush choir ensemble.
          • Multi-part vocal chord stacking:
              - Bass section (-12 semitones / 1 octave down)
              - Tenor lower fifth (-5 semitones / inverted 5th)
              - Alto major third (+4 semitones)
              - High fifth (+7 semitones)
              - Soprano shimmer (+12 semitones / 1 octave up)
          • Spectral formant preservation restoring natural human vocal tract resonances onto harmonies
          • Ensemble doubling with micro-timing delay spread (10-30ms) and Doppler pitch chorusing (±8 cents)
          • Consonant intelligibility preservation: unvoiced phonemes ('s', 't', 'k', 'p') stay crisp on lead voice
          • Cathedral concert hall reverberation with warm high-frequency damping
          • Choral parametric EQ (sub-bass cleanup, chest resonance, anti-mud scoop, angelic air sheen)
          • Soft-knee peak limiter and normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < int(sr * 0.05):
            return samples

        # ── 1. Voiced / Unvoiced Detection (Preserve Speech Articulation) ──────
        hop = 256
        frame_len = 1024
        fmin = 70.0
        fmax = 500.0

        if _LIBROSA_AVAILABLE:
            try:
                f0 = librosa.yin(samples, fmin=fmin, fmax=fmax, sr=sr, frame_length=frame_len, hop_length=hop)
                rms = librosa.feature.rms(y=samples, frame_length=frame_len, hop_length=hop, center=True)[0]
                zcr = librosa.feature.zero_crossing_rate(y=samples, frame_length=frame_len, hop_length=hop, center=True)[0]
            except Exception:
                f0, rms, zcr = AudioEffects._pure_numpy_pitch_track(samples, sr, hop, frame_len, fmin, fmax)
        else:
            f0, rms, zcr = AudioEffects._pure_numpy_pitch_track(samples, sr, hop, frame_len, fmin, fmax)

        n_frames = min(len(f0), len(rms), len(zcr))
        f0 = f0[:n_frames]
        rms = rms[:n_frames]
        zcr = zcr[:n_frames]

        rms_thresh = max(float(np.percentile(rms, 15)) * 1.2, 0.008)
        voiced = (rms > rms_thresh) & (zcr < 0.22) & (f0 > 75.0) & (f0 < 475.0)
        if _SCIPY_AVAILABLE:
            voiced = signal.medfilt(voiced.astype(np.float32), 3) > 0.5
        else:
            padded = np.pad(voiced.astype(np.float32), 1, mode='edge')
            voiced = np.median(np.lib.stride_tricks.sliding_window_view(padded, 3), axis=-1) > 0.5

        frame_times = np.arange(n_frames) * hop
        sample_indices = np.arange(n)
        voiced_interp = np.interp(sample_indices, frame_times, voiced.astype(float))

        # Smooth voiced curve for gentle consonant ducking on harmonies
        win_size = 25
        pad_unv = np.pad(voiced_interp.astype(np.float32), win_size // 2, mode='edge')
        voiced_smooth = np.convolve(pad_unv, np.ones(win_size, dtype=np.float32) / win_size, mode='valid')[:n]
        # On unvoiced frames ('s', 't', 'k', 'p'), duck the harmonies to 20% so consonants don't smudge
        harmony_voiced_gain = 0.20 + 0.80 * voiced_smooth

        # ── 2. Spectral Formant Extraction of Lead Voice ─────────────────────
        fft_orig = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        mag_orig = np.abs(fft_orig)
        env_orig = AudioEffects._extract_spectral_envelope_1d(mag_orig, sr, smooth_hz=350.0)

        # ── 3. Multi-Part Choral Chord Layer Generation ──────────────────────
        # Chord presets mapping
        if chord_preset == "octaves":
            chord_definitions = [
                {"semitones": -12, "gain": 0.50, "delay": 12.0, "depth": 0.9, "rate": 0.50},
                {"semitones":  12, "gain": 0.45, "delay": 24.0, "depth": 1.2, "rate": 0.85},
            ]
        elif chord_preset == "fifths":
            chord_definitions = [
                {"semitones": -12, "gain": 0.45, "delay": 10.0, "depth": 0.8, "rate": 0.50},
                {"semitones":  -5, "gain": 0.40, "delay": 16.0, "depth": 1.0, "rate": 0.65},
                {"semitones":   7, "gain": 0.50, "delay": 14.0, "depth": 1.1, "rate": 0.70},
                {"semitones":  12, "gain": 0.35, "delay": 26.0, "depth": 1.3, "rate": 0.95},
            ]
        else:
            # Full 5-part majestic choir stack
            chord_definitions = [
                {"semitones": -12, "gain": 0.42, "delay": 10.0, "depth": 0.8, "rate": 0.50},  # Bass
                {"semitones":  -5, "gain": 0.38, "delay": 18.0, "depth": 1.0, "rate": 0.62},  # Tenor lower 5th
                {"semitones":   4, "gain": 0.38, "delay": 22.0, "depth": 1.1, "rate": 0.78},  # Mid 3rd
                {"semitones":   7, "gain": 0.46, "delay": 14.0, "depth": 1.2, "rate": 0.68},  # Alto high 5th
                {"semitones":  12, "gain": 0.32, "delay": 28.0, "depth": 1.4, "rate": 1.02},  # Soprano shimmer
            ]

        harmony_sum = np.zeros(n, dtype=np.float32)

        for part in chord_definitions:
            semi = part["semitones"]
            base_gain = part["gain"]
            base_del = part["delay"] * choir_spread
            mod_dep = part["depth"] * choir_spread
            mod_r = part["rate"]

            # Pitch shift
            pitched = AudioEffects.pitch_shift(samples, sr, semitones=semi)
            if len(pitched) < n:
                pitched = np.pad(pitched, (0, n - len(pitched)))
            elif len(pitched) > n:
                pitched = pitched[:n]

            # Formant restoration (keeps vocal timbre human and warm)
            fft_p = np.fft.rfft(pitched)
            mag_p = np.abs(fft_p)
            env_p = AudioEffects._extract_spectral_envelope_1d(mag_p, sr, smooth_hz=350.0)
            formant_ratio = np.clip((env_orig + 1e-4) / (env_p + 1e-4), 0.35, 2.5)

            # Apply formant transfer above 120 Hz
            f_weight = np.clip((freqs - 120.0) / 250.0, 0.0, 0.70)
            applied_formant = 1.0 + f_weight * (formant_ratio - 1.0)
            fft_corrected = fft_p * applied_formant
            corrected_pitched = np.fft.irfft(fft_corrected, n)

            # Apply micro-timing delay and Doppler chorus detuning (human choir variation)
            modulated = AudioEffects._apply_modulated_delay(
                corrected_pitched, sr,
                base_delay_ms=base_del,
                mod_depth_ms=mod_dep,
                mod_rate_hz=mod_r,
                phase_offset=float(semi) * 0.45
            )

            # Apply voiced consonant protection
            modulated *= harmony_voiced_gain

            harmony_sum += base_gain * modulated

        # ── 4. Unison Choir Doublers (Thickening the Lead Voice) ─────────────
        # Generates two micro-delayed & detuned unison doubler voices
        doubler_1 = AudioEffects._apply_modulated_delay(
            samples, sr,
            base_delay_ms=13.0 * choir_spread,
            mod_depth_ms=1.2 * choir_spread,
            mod_rate_hz=0.55,
            phase_offset=0.0
        )
        doubler_2 = AudioEffects._apply_modulated_delay(
            samples, sr,
            base_delay_ms=21.0 * choir_spread,
            mod_depth_ms=1.5 * choir_spread,
            mod_rate_hz=0.85,
            phase_offset=np.pi / 2.0
        )

        # ── 5. Combine Lead + Doublers + Choral Harmonies ─────────────────────
        choir_mix = (
            lead_level * samples +
            0.28 * doubler_1 +
            0.28 * doubler_2 +
            harmony_level * harmony_sum
        )

        # ── 6. Choral Parametric Sculpting EQ ────────────────────────────────
        fft_mix = np.fft.rfft(choir_mix)
        gain_db = np.zeros_like(freqs)

        # A. Steep High-pass below 45 Hz (cleans sub-bass rumble from octave-down stacking)
        hp_mask = freqs < 45.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 24.0 * (1.0 - freqs[hp_mask] / 45.0)

        # B. Choral Chest Warmth at 140 Hz (+2.2 dB, width 40 Hz)
        gain_db += 2.2 * np.exp(-0.5 * ((freqs - 140.0) / 40.0) ** 2)

        # C. Anti-Mud Choral Scoop at 380 Hz (-2.5 dB, width 100 Hz)
        # Crucial for eliminating boxy acoustic mud when multiple voices sum together!
        gain_db -= 2.5 * np.exp(-0.5 * ((freqs - 380.0) / 100.0) ** 2)

        # D. Choral Vowel Bloom & Articulation at 2400 Hz (+1.8 dB, width 600 Hz)
        gain_db += 1.8 * np.exp(-0.5 * ((freqs - 2400.0) / 600.0) ** 2)

        # E. Angelic Cathedral Air Sheen above 8000 Hz (+2.5 dB)
        shelf_mask = freqs > 8000.0
        if np.any(shelf_mask):
            gain_db[shelf_mask] += 2.5 * np.clip((freqs[shelf_mask] - 8000.0) / 3500.0, 0.0, 1.0)

        fft_mix *= 10.0 ** (gain_db / 20.0)
        equalized = np.fft.irfft(fft_mix, n)

        # ── 7. Cathedral / Concert Hall Reverberation ────────────────────────
        if hall_reverb > 0.0:
            reverbed = AudioEffects._choir_hall_reverb(equalized, sr, wet=hall_reverb, decay=0.45)
        else:
            reverbed = equalized

        # ── 8. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_r = np.abs(reverbed)
        over = abs_r > 0.80
        if np.any(over):
            reverbed[over] = np.sign(reverbed[over]) * (0.80 + 0.12 * np.tanh((abs_r[over] - 0.80) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(reverbed))
        if mx > 0:
            reverbed = (reverbed / mx) * 0.912

        return reverbed.astype(np.float32)

    choir = harmonizer
    vocal_chords = harmonizer

    # ──────────────────────────────────────────────────────────────
    #  STUTTER (RHYTHMIC MICRO-SLICES, RAPID ROLLS & AMBIENT CLOUDS)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _generate_granular_cloud(samples, sr, grain_len_ms=45, hop_ms=16, min_jitter_ms=15, max_jitter_ms=95):
        """
        Synthesize an ambient granular particle cloud by scattering overlapping micro-grains in time.
        Creates a shimmering, dreamlike halo of vocal particles floating behind the speech.
        """
        n = len(samples)
        if n == 0:
            return samples

        grain_len = max(int(sr * grain_len_ms / 1000.0), 32)
        hop = max(int(sr * hop_ms / 1000.0), 16)
        min_j = int(sr * min_jitter_ms / 1000.0)
        max_j = max(int(sr * max_jitter_ms / 1000.0), min_j + 1)

        window = np.hanning(grain_len).astype(np.float32)
        cloud = np.zeros(n, dtype=np.float32)

        rng = np.random.RandomState(42)  # Deterministic seed for reproducible artistic texture

        for i in range(0, n - grain_len, hop):
            frame = samples[i:i + grain_len]
            # Skip near-silent frames to save compute and keep noise floor pristine
            if np.max(np.abs(frame)) < 0.015:
                continue

            grain = frame * window
            jitter = rng.randint(min_j, max_j)
            tgt = i + jitter

            if tgt + grain_len < n:
                cloud[tgt:tgt + grain_len] += grain * 0.45

        # Diffused ambient feedback shimmer
        diff_1 = int(sr * 0.038)
        diff_2 = int(sr * 0.071)
        if diff_1 < n:
            cloud[diff_1:] += 0.22 * cloud[:-diff_1]
        if diff_2 < n:
            cloud[diff_2:] += 0.15 * cloud[:-diff_2]

        # Soft high-frequency air sheen on the cloud
        fft_cloud = np.fft.rfft(cloud)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        cloud_eq = np.zeros_like(freqs)
        cloud_eq[freqs < 120.0] = -18.0  # Cut rumble
        cloud_eq[freqs > 6000.0] = 2.0   # Shimmer
        fft_cloud *= 10.0 ** (cloud_eq / 20.0)
        cloud = np.fft.irfft(fft_cloud, n)

        return cloud.astype(np.float32)

    @staticmethod
    def stutter(
        samples,
        sr,
        stutter_density=0.65,
        grain_cloud=0.28,
        accelerating_rolls=True,
        presence_boost=2.2
    ):
        """
        Stutter Voice Effect — slices the vocal into microscopic rhythmic snippets,
        rapid looping drill rolls, and lush ambient granular clouds.
          • Intelligent transient & onset detection for musical phrase-synced slicing
          • Microscopic rhythmic slicing (15ms - 50ms / 1/16th, 1/32nd, 1/64th notes)
          • Accelerating loop rolls ("machine gun / snare drill" stutter build-ups)
          • Smooth Tukey cosine crossfade windowing eliminating zero-crossing clicks
          • Ambient granular particle cloud scatter diffusing vocal micro-grains
          • 100% timeline-preserving architecture maintaining exact subtitle / SRT sync
          • Snappy modern presence exciter at 3.5 kHz (+2.2 dB) and sub-bass rumble cutoff
          • Soft-knee peak limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < int(sr * 0.1):
            return samples

        # ── 1. Energy Envelope & Syllable Onset Detection ─────────────────────
        win_size = int(sr * 0.015)  # 15ms window
        hop_size = int(sr * 0.005)  # 5ms hop
        if win_size <= 0 or hop_size <= 0:
            return samples

        num_frames = (n - win_size) // hop_size
        if num_frames < 4:
            return samples

        # Vectorized short-term energy computation
        pad_len = num_frames * hop_size + win_size
        padded_samples = samples[:pad_len]
        frames = np.lib.stride_tricks.sliding_window_view(padded_samples, win_size)[::hop_size][:num_frames]
        frame_energy = np.sqrt(np.mean(frames ** 2, axis=-1))

        # Energy flux (positive onset derivative)
        energy_flux = np.maximum(0.0, np.diff(frame_energy, prepend=frame_energy[0]))
        max_flux = np.max(energy_flux) if len(energy_flux) > 0 else 0.0

        # Candidate onsets with minimum spacing of ~220ms
        min_onset_dist = int(0.220 / (hop_size / sr))
        candidate_onsets = []
        flux_thresh = max_flux * 0.15

        i = 0
        while i < num_frames:
            if energy_flux[i] > flux_thresh and frame_energy[i] > 0.025:
                # Find local peak in neighborhood
                local_end = min(i + min_onset_dist // 3, num_frames)
                peak_idx = i + np.argmax(energy_flux[i:local_end])
                onset_sample = peak_idx * hop_size
                if onset_sample < n - int(sr * 0.25):
                    candidate_onsets.append(onset_sample)
                i += min_onset_dist
            else:
                i += 1

        # ── 2. Microscopic Rhythmic Slicing & Loop Roll Generation ───────────
        out = np.copy(samples)
        rng = np.random.RandomState(1337)

        # Helper: generate a windowed micro-slice with smooth edges
        def extract_windowed_slice(src, length):
            sub = src[:length].copy()
            fade = min(int(sr * 0.0025), length // 4)  # 2.5ms cosine fade
            if fade > 1:
                w_in = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fade, dtype=np.float32))
                w_out = 0.5 + 0.5 * np.cos(np.linspace(0.0, np.pi, fade, dtype=np.float32))
                sub[:fade] *= w_in
                sub[-fade:] *= w_out
            return sub

        for onset in candidate_onsets:
            if rng.random() > stutter_density:
                continue

            # Randomly select between accelerating roll, uniform micro-repeat, and rhythmic gate
            event_choice = rng.randint(0, 3)

            if event_choice == 0 and accelerating_rolls:
                # ── ACCELERATING MACHINE GUN ROLL ──
                # 3 stages: 40ms -> 20ms -> 10ms
                s1_dur = int(sr * 0.080)
                s2_dur = int(sr * 0.080)
                s3_dur = int(sr * 0.080)
                total_ev = s1_dur + s2_dur + s3_dur

                if onset + total_ev >= n:
                    continue

                roll_buffer = np.zeros(total_ev, dtype=np.float32)

                # Stage 1: 40ms slice looped
                slice_len1 = int(sr * 0.040)
                slice1 = extract_windowed_slice(samples[onset:onset + slice_len1], slice_len1)
                for k in range(0, s1_dur, slice_len1):
                    seg_len = min(slice_len1, s1_dur - k)
                    roll_buffer[k:k + seg_len] = slice1[:seg_len] * 0.85

                # Stage 2: 20ms slice looped with rising volume
                slice_len2 = int(sr * 0.020)
                slice2 = extract_windowed_slice(samples[onset:onset + slice_len2], slice_len2)
                for k in range(0, s2_dur, slice_len2):
                    seg_len = min(slice_len2, s2_dur - k)
                    roll_buffer[s1_dur + k:s1_dur + k + seg_len] = slice2[:seg_len] * 1.0

                # Stage 3: 10ms slice looped with snappy buzz
                slice_len3 = int(sr * 0.010)
                slice3 = extract_windowed_slice(samples[onset:onset + slice_len3], slice_len3)
                for k in range(0, s3_dur, slice_len3):
                    seg_len = min(slice_len3, s3_dur - k)
                    roll_buffer[s1_dur + s2_dur + k:s1_dur + s2_dur + k + seg_len] = slice3[:seg_len] * 1.15

                # Crossfade roll into the main track
                cf_len = min(int(sr * 0.005), 120)
                cf_in = np.linspace(0.0, 1.0, cf_len, dtype=np.float32)
                cf_out = np.linspace(1.0, 0.0, cf_len, dtype=np.float32)
                roll_buffer[:cf_len] *= cf_in
                roll_buffer[-cf_len:] *= cf_out

                out[onset:onset + total_ev] = out[onset:onset + total_ev] * 0.15 + roll_buffer * 0.85

            elif event_choice == 1:
                # ── UNIFORM RAPID LOOPING (1/32nd or 1/64th repeat) ──
                slice_dur_ms = rng.choice([25.0, 35.0, 48.0])
                slice_len = int(sr * slice_dur_ms / 1000.0)
                num_repeats = rng.choice([4, 6, 8])
                total_ev = slice_len * num_repeats

                if onset + total_ev >= n or slice_len < 32:
                    continue

                micro_slice = extract_windowed_slice(samples[onset:onset + slice_len], slice_len)
                repeat_buffer = np.tile(micro_slice, num_repeats)

                cf_len = min(int(sr * 0.004), len(repeat_buffer) // 4)
                if cf_len > 0:
                    repeat_buffer[:cf_len] *= np.linspace(0.0, 1.0, cf_len, dtype=np.float32)
                    repeat_buffer[-cf_len:] *= np.linspace(1.0, 0.0, cf_len, dtype=np.float32)

                out[onset:onset + total_ev] = out[onset:onset + total_ev] * 0.20 + repeat_buffer * 0.80

            else:
                # ── RHYTHMIC CHOP / GATING ──
                gate_dur = int(sr * 0.200)  # 200ms gating burst
                if onset + gate_dur >= n:
                    continue

                chunk = samples[onset:onset + gate_dur]
                t_gate = np.arange(gate_dur, dtype=np.float32) / sr
                gate_hz = rng.choice([24.0, 32.0, 40.0])
                # Smooth square wave with cosine edges (no clicks)
                sq = np.sin(2.0 * np.pi * gate_hz * t_gate)
                gate = np.clip(sq * 4.0, -1.0, 1.0) * 0.5 + 0.5
                gated_chunk = chunk * gate

                cf_len = min(int(sr * 0.004), gate_dur // 4)
                if cf_len > 0:
                    gated_chunk[:cf_len] *= np.linspace(0.0, 1.0, cf_len, dtype=np.float32)
                    gated_chunk[-cf_len:] *= np.linspace(1.0, 0.0, cf_len, dtype=np.float32)

                out[onset:onset + gate_dur] = out[onset:onset + gate_dur] * 0.25 + gated_chunk * 0.75

        # ── 3. Ambient Granular Cloud Scatter ────────────────────────────────
        if grain_cloud > 0.0:
            cloud = AudioEffects._generate_granular_cloud(samples, sr, grain_len_ms=50, hop_ms=18)
            final_mix = (1.0 - grain_cloud * 0.35) * out + grain_cloud * cloud
        else:
            final_mix = out

        # ── 4. Parametric Sculpting EQ & Electronic Presence ─────────────────
        fft_mix = np.fft.rfft(final_mix)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # High-pass filter below 55 Hz (eliminates sub-bass pops from gating transients)
        hp_mask = freqs < 55.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 24.0 * (1.0 - freqs[hp_mask] / 55.0)

        # Electronic stutter bite & presence at 3500 Hz (+2.2 dB)
        if presence_boost > 0.0:
            gain_db += presence_boost * np.exp(-0.5 * ((freqs - 3500.0) / 750.0) ** 2)

        # Top-end air shimmer at 8500 Hz (+1.8 dB)
        shelf_mask = freqs > 8500.0
        if np.any(shelf_mask):
            gain_db[shelf_mask] += 1.8 * np.clip((freqs[shelf_mask] - 8500.0) / 3500.0, 0.0, 1.0)

        fft_mix *= 10.0 ** (gain_db / 20.0)
        equalized = np.fft.irfft(fft_mix, n)

        # ── 5. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_e = np.abs(equalized)
        over = abs_e > 0.80
        if np.any(over):
            equalized[over] = np.sign(equalized[over]) * (0.80 + 0.12 * np.tanh((abs_e[over] - 0.80) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(equalized))
        if mx > 0:
            equalized = (equalized / mx) * 0.912

        return equalized.astype(np.float32)

    stutter_edit = stutter
    micro_stutter = stutter

    # ──────────────────────────────────────────────────────────────
    #  BITCRUSHER (HARSH 8-BIT RETRO VIDEO GAME & HYPERPOP VOCAL)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def bitcrusher(
        samples,
        sr,
        bit_depth=8,
        target_sr=6500,
        drive=1.35,
        wet=0.92,
        hyperpop_edge=True
    ):
        """
        Bitcrushing Voice Effect — drastically reduces the sample rate and bit depth
        of the audio, achieving a harsh, crunchy 8-bit retro video game aesthetic on a hyperpop vocal.
          • Pre-quantization saturation preventing quiet consonant dropouts
          • Zero-order sample-and-hold downsampler to retro clock rate (default ~6.5 kHz)
          • True 8-bit quantization (256 discrete amplitude steps) for authentic square-wave crunch
          • Specular Nyquist aliasing foldover mirrors modeling NES/Game Boy sound chips
          • Hyperpop vocal presence exciter at 3.5 kHz (+3.2 dB) cutting through the crunch
          • Ultrasonic anti-harshness smoothing above 7.8 kHz (warm CRT TV gaming timbre)
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        # ── 1. Pre-Drive & Harmonic Saturation ────────────────────────────────
        # Warms up low-level consonants ('s', 't', 'k') so they survive bit quantization
        driven = samples * drive
        driven = np.tanh(driven * 1.1)

        # ── 2. Sample-and-Hold Downsampler (Clock Decimation & Aliasing) ───────
        downsample_factor = max(int(round(sr / target_sr)), 1)
        if downsample_factor > 1:
            held = np.copy(driven)
            num_blocks = n // downsample_factor
            trimmed_len = num_blocks * downsample_factor
            if trimmed_len > 0:
                blocks = held[:trimmed_len].reshape(num_blocks, downsample_factor)
                blocks[:, :] = blocks[:, 0:1]
                held[:trimmed_len] = blocks.reshape(-1)
            if trimmed_len < n:
                held[trimmed_len:] = held[trimmed_len]
        else:
            held = driven

        # ── 3. Bit Depth Quantization (8-Bit Stepping) ────────────────────────
        # 8-bit signed integer has 256 discrete levels in [-1.0, 1.0]
        q_steps = 2.0 ** (bit_depth - 1)
        quantized = np.round(held * q_steps) / q_steps
        quantized = np.clip(quantized, -1.0, 1.0)

        # ── 4. Retro Gaming & Hyperpop Spectral Sculpting ─────────────────────
        fft_q = np.fft.rfft(quantized)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # High-pass filter below 60 Hz (cleans sub-bass DC thumps from bit stepping)
        hp_mask = freqs < 60.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 24.0 * (1.0 - freqs[hp_mask] / 60.0)

        # Hyperpop aggressive vocal presence at 3500 Hz (+3.2 dB, width 800 Hz)
        if hyperpop_edge:
            gain_db += 3.2 * np.exp(-0.5 * ((freqs - 3500.0) / 800.0) ** 2)

        # Retro CRT gaming cabinet rolloff above 7800 Hz (tames piercing ultrasound)
        lp_mask = freqs > 7800.0
        if np.any(lp_mask):
            ratio = np.clip(freqs[lp_mask] / 7800.0, 1.0, 4.0)
            gain_db[lp_mask] -= 18.0 * np.log10(ratio)

        fft_q *= 10.0 ** (gain_db / 20.0)
        crushed = np.fft.irfft(fft_q, n)

        # ── 5. Wet / Dry Blend (Intelligibility Anchor) ────────────────────────
        # Blends 92% crushed signal with 8% pristine original speech for 100% word comprehension
        blended = (1.0 - wet) * samples + wet * crushed

        # ── 6. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_b = np.abs(blended)
        over = abs_b > 0.80
        if np.any(over):
            blended[over] = np.sign(blended[over]) * (0.80 + 0.12 * np.tanh((abs_b[over] - 0.80) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(blended))
        if mx > 0:
            blended = (blended / mx) * 0.912

        return blended.astype(np.float32)

    bitcrush = bitcrusher
    retro_8bit = bitcrusher

    # ──────────────────────────────────────────────────────────────
    #  MEGAPHONE (STEEP FILTERS, NASAL MIDRANGE & INTIMATE LO-FI)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def megaphone(
        samples,
        sr,
        drive=1.45,
        nasal_resonance=7.0,
        horn_cutoff_low=480.0,
        horn_cutoff_high=3800.0
    ):
        """
        Megaphone Voice Effect — steep filters cut out all deep lows and crisp highs,
        leaving only a thin, nasal midrange to create an intimate, lo-fi vocal effect.
          • Steep high-pass brickwall below 480 Hz completely cutting chest lows & sub-bass
          • Steep low-pass cutoff above 3.8 kHz eliminating all crisp highs and modern breath air
          • Resonant nasal horn peak at 1.5 kHz (+7.0 dB) producing signature megaphone honk
          • Secondary cone projection bell at 2.8 kHz (+3.5 dB) keeping diction clear
          • Asymmetric lo-fi diaphragm saturation and intimate PA speaker compression
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        # ── 1. Lo-Fi Driver Diaphragm Saturation ──────────────────────────────
        driven = samples * drive
        sat = np.tanh(driven * 1.25) + 0.06 * (driven ** 2) * np.sign(driven)
        sat = sat - np.mean(sat)

        # ── 2. Intimate Handheld Public Address Compression ──────────────────
        threshold = 0.16
        ratio = 0.30
        abs_s = np.abs(sat)
        compressed = np.copy(abs_s)
        mask = abs_s > threshold
        compressed[mask] = threshold + (abs_s[mask] - threshold) * ratio
        leveled = np.sign(sat) * compressed * 1.40

        # ── 3. Steep Bandpass & Nasal Horn Resonance (Applied Post-Drive) ─────
        fft_sig = np.fft.rfft(leveled)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # A. Steep High-Pass below 480 Hz (completely strips all deep lows and chest warmth)
        hp_stop = freqs < (horn_cutoff_low - 120.0)
        if np.any(hp_stop):
            gain_db[hp_stop] -= 55.0

        hp_trans = (freqs >= (horn_cutoff_low - 120.0)) & (freqs < horn_cutoff_low)
        if np.any(hp_trans):
            t_norm = (horn_cutoff_low - freqs[hp_trans]) / 120.0
            gain_db[hp_trans] -= 50.0 * t_norm

        # B. Prominent Nasal Horn Resonance at 1500 Hz (+nasal_resonance dB)
        # Signature thin, honky, nasal megaphone squawk
        gain_db += nasal_resonance * np.exp(-0.5 * ((freqs - 1500.0) / 220.0) ** 2)

        # C. Secondary Horn Cone Projection at 2800 Hz (+3.5 dB)
        gain_db += 3.5 * np.exp(-0.5 * ((freqs - 2800.0) / 400.0) ** 2)

        # D. Steep Low-Pass above 3800 Hz (cuts out crisp highs, sizzle, and air)
        lp_trans = (freqs >= (horn_cutoff_high - 100.0)) & (freqs < (horn_cutoff_high + 400.0))
        if np.any(lp_trans):
            t_norm = (freqs[lp_trans] - (horn_cutoff_high - 100.0)) / 500.0
            gain_db[lp_trans] -= 48.0 * t_norm

        lp_stop = freqs >= (horn_cutoff_high + 400.0)
        if np.any(lp_stop):
            gain_db[lp_stop] -= 55.0

        fft_sig *= 10.0 ** (gain_db / 20.0)
        filtered = np.fft.irfft(fft_sig, n)

        # ── 4. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_l = np.abs(filtered)
        over = abs_l > 0.82
        if np.any(over):
            filtered[over] = np.sign(filtered[over]) * (0.82 + 0.12 * np.tanh((abs_l[over] - 0.82) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(filtered))
        if mx > 0:
            filtered = (filtered / mx) * 0.912

        return filtered.astype(np.float32)

    bullhorn = megaphone
    lofi_megaphone = megaphone

    # ──────────────────────────────────────────────────────────────
    #  DELAY (SHORT RHYTHMIC SLAPBACK ECHO & SPACE FILLER)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def delay(
        samples,
        sr,
        delay_ms=115.0,
        feedback=0.08,
        wet=0.42,
        dry=0.88
    ):
        """
        Delay Voice Effect — a single, very short rhythmic echo with little to no feedback.
        Fills in spaces between words and makes vocals sound tight, confident, and rhythmic.
          • Calibrated short rhythmic slapback timing (default ~115ms)
          • Single dominant repeat with near-zero feedback (0.08) eliminating acoustic clutter
          • Warm tape echo EQ: cuts sub-bass plosives (<160 Hz) & softens harsh sibilance (>4.8 kHz)
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        delay_samples = max(int(round(sr * delay_ms / 1000.0)), 1)
        if delay_samples >= n:
            return samples

        # ── 1. Tape Slap EQ on Delay Send ────────────────────────────────────
        # Soften highs and cut lows on the echo path so it sits warm behind the voice
        fft_send = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # High-pass below 160 Hz (prevents mud & thumping)
        hp_mask = freqs < 160.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 18.0 * (1.0 - freqs[hp_mask] / 160.0)

        # High shelf rolloff above 4800 Hz (tape warmth, softens sibilance)
        lp_mask = freqs > 4800.0
        if np.any(lp_mask):
            ratio = np.clip(freqs[lp_mask] / 4800.0, 1.0, 3.5)
            gain_db[lp_mask] -= 9.0 * np.log10(ratio)

        fft_send *= 10.0 ** (gain_db / 20.0)
        filtered_send = np.fft.irfft(fft_send, n)

        # ── 2. Single-Repeat Delay Line with Controlled Micro-Feedback ────────
        delayed = np.zeros(n, dtype=np.float32)
        # Primary single slap repeat
        delayed[delay_samples:] = filtered_send[:-delay_samples]

        # Subtle secondary repeat if feedback > 0
        if feedback > 0.01:
            second_delay = delay_samples * 2
            if second_delay < n:
                delayed[second_delay:] += feedback * filtered_send[:-second_delay]

        # ── 3. Combine Dry + Wet Slap ─────────────────────────────────────────
        mix = dry * samples + wet * delayed

        # ── 4. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_m = np.abs(mix)
        over = abs_m > 0.82
        if np.any(over):
            mix[over] = np.sign(mix[over]) * (0.82 + 0.12 * np.tanh((abs_m[over] - 0.82) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(mix))
        if mx > 0:
            mix = (mix / mx) * 0.912

        return mix.astype(np.float32)

    slapback = delay
    slap_delay = delay

    # ──────────────────────────────────────────────────────────────
    #  PLATE REVERB (BRIGHT METALLIC POP VOCAL DENSITY & TAIL)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def plate_reverb(
        samples,
        sr,
        decay=0.65,
        pre_delay_ms=22.0,
        brightness=0.75,
        wet=0.32,
        dry=0.88
    ):
        """
        Plate Reverb Voice Effect — emulates acoustic vibrations through a large metal plate (EMT 140).
        The gold standard for pop vocals, adding bright density without muddying the words.
          • Ultra-dense initial diffusion eliminating discrete room echo flutter
          • 22ms pre-delay decouples dry lead vocal from reverb onset for 100% diction clarity
          • High-pass send filtering below 280 Hz preventing boomy room mud accumulation
          • Extended high-frequency metal plate shimmer (>7.5 kHz) cutting through modern pop mixes
          • Multi-stage all-pass diffusion cascade and cross-coupled feedback tank
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        # ── 1. Pre-Delay Separation (Preserve Upfront Consonant Diction) ──────
        pre_samples = max(int(round(sr * pre_delay_ms / 1000.0)), 1)
        if pre_samples < n:
            pre_delayed = np.pad(samples, (pre_samples, 0))[:n]
        else:
            pre_delayed = samples

        # ── 2. Plate Send Conditioning EQ (Anti-Mud & High Sparkle) ───────────
        fft_send = np.fft.rfft(pre_delayed)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # High-pass rolloff below 280 Hz (eliminates chest resonance mud)
        hp_mask = freqs < 280.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 36.0 * (1.0 - freqs[hp_mask] / 280.0)
        sub_mask = freqs < 150.0
        if np.any(sub_mask):
            gain_db[sub_mask] -= 42.0

        # Metal plate driver brightness excitation at 7.0 kHz (+2.5 dB)
        gain_db += 2.5 * np.exp(-0.5 * ((freqs - 7000.0) / 2200.0) ** 2)

        fft_send *= 10.0 ** (gain_db / 20.0)
        driver_sig = np.fft.irfft(fft_send, n)

        # ── 3. Multi-Stage All-Pass Diffusers (Metal Plate Modal Density) ─────
        # Simulates rapid dispersion of vibration waves through steel
        diff_delays = [
            int(sr * 0.0048),  # 4.8ms
            int(sr * 0.0073),  # 7.3ms
            int(sr * 0.0116),  # 11.6ms
            int(sr * 0.0167),  # 16.7ms
        ]
        diff_g = 0.62

        diffused = np.copy(driver_sig)
        for d in diff_delays:
            if d >= n:
                continue
            # Pure numpy vectorized all-pass filter: y[i] = -g*x[i] + x[i-d] + g*y[i-d]
            y = np.zeros(n, dtype=np.float32)
            # Efficient block-wise recursive loop for short delay
            for i in range(d, n):
                y[i] = -diff_g * diffused[i] + diffused[i - d] + diff_g * y[i - d]
            diffused = y

        # ── 4. Cross-Coupled Plate Feedback Tank ──────────────────────────────
        # Metal plate pickups combine immediate surface wave vibration + circulating reflections
        tank = np.zeros(n, dtype=np.float32)
        tank += 0.38 * diffused  # Immediate metal plate surface pickup

        tank_delays = [
            int(sr * 0.018),  # 18ms
            int(sr * 0.034),  # 34ms
            int(sr * 0.056),  # 56ms
            int(sr * 0.082),  # 82ms
        ]
        tank_gains = [0.30, 0.24, 0.18, 0.12]

        for d, g in zip(tank_delays, tank_gains):
            if d < n:
                tank[d:] += g * diffused[:-d]

        # Feedback circulation
        fb_tap = int(sr * 0.075)
        if fb_tap < n:
            fb_gain = decay * 0.42
            for i in range(fb_tap, n):
                tank[i] += fb_gain * tank[i - fb_tap]

        # Frequency-dependent plate damping (preserves airy shimmer)
        fft_tank = np.fft.rfft(tank)
        damp_cutoff = 5500.0 + 3500.0 * np.clip(brightness, 0.0, 1.0)
        damp_mask = freqs > damp_cutoff
        if np.any(damp_mask):
            ratio = np.clip(freqs[damp_mask] / damp_cutoff, 1.0, 4.0)
            damp_db = -14.0 * np.log10(ratio)
            fft_tank[damp_mask] *= 10.0 ** (damp_db / 20.0)

        # Air boost on the plate tail
        air_mask = freqs > 6500.0
        if np.any(air_mask):
            fft_tank[air_mask] *= 10.0 ** (1.8 / 20.0)

        plate_tail = np.fft.irfft(fft_tank, n)

        # ── 5. Combine Dry Vocal + Lush Shimmering Plate Tail ─────────────────
        mix = dry * samples + wet * plate_tail

        # ── 6. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_m = np.abs(mix)
        over = abs_m > 0.82
        if np.any(over):
            mix[over] = np.sign(mix[over]) * (0.82 + 0.12 * np.tanh((abs_m[over] - 0.82) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(mix))
        if mx > 0:
            mix = (mix / mx) * 0.912

        return mix.astype(np.float32)

    plate = plate_reverb
    emt140 = plate_reverb

    # ──────────────────────────────────────────────────────────────
    #  SHIMMER REVERB (CELESTIAL PITCHED-UP OCTAVE SYNTH PAD)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def shimmer_reverb(
        samples,
        sr,
        decay=0.72,
        shimmer_mix=0.45,
        pre_delay_ms=24.0,
        pitch_shift_semitones=12,
        wet=0.35,
        dry=0.88
    ):
        """
        Shimmer Reverb Voice Effect — adds pitched-up octaves (+12 semitones) into the reverb tail,
        transforming the vocal into a celestial, atmospheric pad and letting sustained notes evolve
        into an angelic synthesizer pad.
          • Pitched-up octave (+12st) feedback loop creating ascending harmonic blooms (+1, +2 octaves)
          • Multi-tap prime delay diffusion network (31ms - 149ms) for wide celestial space
          • Pre-delay separation (24ms) keeping upfront vocal diction articulate
          • Sibilance-protected send filter (<5 kHz) preventing harsh consonant chirping
          • Low-cut filter (<180 Hz) maintaining a clean, mud-free ambient pad
          • Angelic high-shelf air shimmer (>8.5 kHz, +2.2 dB)
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        # ── 1. Pre-Delay Separation (Preserve Diction Before Bloom) ───────────
        pre_samples = max(int(round(sr * pre_delay_ms / 1000.0)), 1)
        if pre_samples < n:
            pre_delayed = np.pad(samples, (pre_samples, 0))[:n]
        else:
            pre_delayed = samples

        # ── 2. Send Filtering (Anti-Mud & Sibilance Protection) ───────────────
        fft_send = np.fft.rfft(pre_delayed)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # High-pass rolloff below 180 Hz (eliminates boomy mud in the pad)
        hp_mask = freqs < 180.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 32.0 * (1.0 - freqs[hp_mask] / 180.0)

        # High-frequency damping above 5000 Hz on the pitch send
        # Critical: prevents unvoiced consonants ('s', 't', 'k') from screeching when shifted up an octave!
        lp_mask = freqs > 5000.0
        if np.any(lp_mask):
            gain_db[lp_mask] -= 18.0 * np.log10(freqs[lp_mask] / 5000.0)

        fft_send *= 10.0 ** (gain_db / 20.0)
        conditioned_send = np.fft.irfft(fft_send, n)

        # ── 3. Base Reverb Diffusion Tank ─────────────────────────────────────
        base_taps = [
            int(sr * 0.031),  # 31ms
            int(sr * 0.053),  # 53ms
            int(sr * 0.079),  # 79ms
            int(sr * 0.113),  # 113ms
            int(sr * 0.149),  # 149ms
        ]
        base_gains = [0.35, 0.28, 0.22, 0.17, 0.12]

        base_rev = np.zeros(n, dtype=np.float32)
        for d, g in zip(base_taps, base_gains):
            if d < n:
                base_rev[d:] += g * conditioned_send[:-d]

        # Base feedback tail
        fb_base = int(sr * 0.095)
        if fb_base < n:
            b_decay = decay * 0.38
            for i in range(fb_base, n):
                base_rev[i] += b_decay * base_rev[i - fb_base]

        # ── 4. Pitched-Up Octave Shimmer Synth Pad (+12 semitones) ────────────
        # Phase-vocoder octave pitch shift
        shifted_octave = AudioEffects.pitch_shift(conditioned_send, sr, semitones=pitch_shift_semitones)
        if len(shifted_octave) < n:
            shifted_octave = np.pad(shifted_octave, (0, n - len(shifted_octave)))
        elif len(shifted_octave) > n:
            shifted_octave = shifted_octave[:n]

        # Apply gentle slow modulation (Doppler chorusing) to give the synth pad swirling motion
        t = np.arange(n, dtype=np.float32) / sr
        mod_delay_samples = (12.0 + 1.5 * np.sin(2.0 * np.pi * 0.75 * t)) * (sr / 1000.0)
        idx = np.clip(np.arange(n, dtype=np.float32) - mod_delay_samples, 0.0, float(n - 1))
        idx_f = idx.astype(int)
        idx_c = np.clip(idx_f + 1, 0, n - 1)
        frac = idx - idx_f
        swirling_octave = (1.0 - frac) * shifted_octave[idx_f] + frac * shifted_octave[idx_c]

        # Recirculating shimmer tank (creates the evolving ascending cascade)
        shimmer_tank = np.zeros(n, dtype=np.float32)
        shimmer_taps = [
            int(sr * 0.044),  # 44ms
            int(sr * 0.088),  # 88ms
            int(sr * 0.136),  # 136ms
            int(sr * 0.184),  # 184ms
        ]
        shimmer_gains = [0.36, 0.28, 0.20, 0.14]

        for d, g in zip(shimmer_taps, shimmer_gains):
            if d < n:
                shimmer_tank[d:] += g * swirling_octave[:-d]

        # Shimmer feedback circulation (blooms into celestial pad)
        shimmer_fb = int(sr * 0.125)
        if shimmer_fb < n:
            s_decay = decay * 0.44
            for i in range(shimmer_fb, n):
                shimmer_tank[i] += s_decay * shimmer_tank[i - shimmer_fb]

        # ── 5. Combine Reverb Bed + Angelic Shimmer Pad + EQ ───────────────────
        wet_pad = (1.0 - shimmer_mix) * base_rev + shimmer_mix * shimmer_tank

        fft_wet = np.fft.rfft(wet_pad)
        pad_gain = np.zeros_like(freqs)

        # High shelf boost for celestial air brilliance above 8.5 kHz
        air_mask = freqs > 8500.0
        if np.any(air_mask):
            pad_gain[air_mask] += 2.2 * np.clip((freqs[air_mask] - 8500.0) / 3500.0, 0.0, 1.0)

        fft_wet *= 10.0 ** (pad_gain / 20.0)
        celestial_pad = np.fft.irfft(fft_wet, n)

        # ── 6. Combine Dry Speech + Lush Shimmer Pad ──────────────────────────
        mix = dry * samples + wet * celestial_pad

        # ── 7. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_m = np.abs(mix)
        over = abs_m > 0.82
        if np.any(over):
            mix[over] = np.sign(mix[over]) * (0.82 + 0.12 * np.tanh((abs_m[over] - 0.82) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(mix))
        if mx > 0:
            mix = (mix / mx) * 0.912

        return mix.astype(np.float32)

    shimmer = shimmer_reverb
    celestial_reverb = shimmer_reverb

    # ──────────────────────────────────────────────────────────────
    #  REVERSE REVERB (GHOSTLY BACKWARD CRESCENDO & PRE-VOCAL SWOOSH)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def reverse_reverb(
        samples,
        sr,
        swell_duration_ms=650.0,
        wet=0.42,
        dry=0.88,
        phrase_swells=True
    ):
        """
        Reverse Reverb Voice Effect — the reverberation tail plays backward and builds up
        before the dry vocal hits, creating a ghostly anticipation and a dramatic swoosh
        right before the first word of the lead vocal.
          • Time-inverted reverberation pipeline (reverses phrase, applies dense hall decay, and flips back)
          • Automatic detection of initial vocal attack and major phrase onsets
          • Exponential crescendo envelope aligning the peak of the swoosh right as the first word strikes
          • Ghostly spectral shaping (low-cut <180 Hz, haunting vocal presence at 2.2 kHz)
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < int(sr * 0.1):
            return samples

        # ── 1. Find Initial Vocal Attack ──────────────────────────────────────
        win_size = int(sr * 0.010)
        hop_size = int(sr * 0.005)
        num_frames = (n - win_size) // hop_size
        if num_frames < 4:
            return samples

        frames = np.lib.stride_tricks.sliding_window_view(samples[:num_frames * hop_size + win_size], win_size)[::hop_size]
        energy = np.sqrt(np.mean(frames ** 2, axis=-1))

        # Detect first speech onset above silence
        silence_thresh = max(float(np.percentile(energy, 20)) * 1.5, 0.015)
        active_indices = np.where(energy > silence_thresh)[0]
        if len(active_indices) == 0:
            first_onset = 0
        else:
            first_onset = active_indices[0] * hop_size

        swell_len = max(int(round(sr * swell_duration_ms / 1000.0)), 128)

        # If vocal starts with less than swell_len of headroom, prepend lead-in silence so the swoosh can build up out of silence
        lead_headroom = first_onset
        if lead_headroom < swell_len:
            prepend_len = swell_len - lead_headroom
            extended_samples = np.pad(samples, (prepend_len, 0))
            first_onset += prepend_len
        else:
            extended_samples = np.copy(samples)

        total_n = len(extended_samples)

        # ── 2. Helper: Generate Reverse Reverb Swell from a Word Chunk ─────────
        def create_reverse_swell(source_chunk, length):
            chunk_rev = source_chunk[::-1]
            # Multi-tap dense diffusion on reversed chunk
            taps = [
                int(sr * 0.035),
                int(sr * 0.065),
                int(sr * 0.110),
                int(sr * 0.175),
                int(sr * 0.260),
            ]
            gains = [0.38, 0.30, 0.22, 0.16, 0.12]
            tail = np.zeros(length, dtype=np.float32)

            for d, g in zip(taps, gains):
                if d < length:
                    src_len = min(len(chunk_rev), length - d)
                    tail[d:d + src_len] += g * chunk_rev[:src_len]

            # Feedback diffusion
            fb_d = int(sr * 0.090)
            if fb_d < length:
                for i in range(fb_d, length):
                    tail[i] += 0.45 * tail[i - fb_d]

            # Flip backward: tail now becomes an ascending crescendo swoosh!
            swoosh = tail[::-1]

            # Exponential rise envelope: e^(3.5*(t/T - 1))
            t_norm = np.linspace(0.0, 1.0, length, dtype=np.float32)
            env = np.exp(3.5 * (t_norm - 1.0))
            return swoosh * env

        # ── 3. Generate Pre-Vocal Swoosh into the First Word ───────────────────
        swoosh_track = np.zeros(total_n, dtype=np.float32)

        # Extract first spoken word chunk (e.g. 500ms following first onset)
        word_chunk_len = min(int(sr * 0.500), total_n - first_onset)
        if word_chunk_len > 32:
            first_word = extended_samples[first_onset:first_onset + word_chunk_len]
            # Smooth window on first word chunk
            w_win = np.hanning(len(first_word)).astype(np.float32)
            w_win[:len(first_word) // 2] = 1.0  # Preserve sharp initial attack
            first_word_windowed = first_word * w_win

            initial_swoosh = create_reverse_swell(first_word_windowed, swell_len)

            start_pos = first_onset - swell_len
            if start_pos >= 0 and start_pos + swell_len <= total_n:
                swoosh_track[start_pos:start_pos + swell_len] += initial_swoosh

        # ── 4. Secondary Phrase Swells (Leading into Sentences/Phrases) ────────
        if phrase_swells:
            # Find subsequent onsets after long pauses (>350ms)
            pause_frames = int(0.350 / (hop_size / sr))
            min_gap_samples = int(sr * 1.5)  # Don't trigger more often than every 1.5s
            last_onset_pos = first_onset

            for k in range(len(energy) - pause_frames):
                frame_pos = k * hop_size
                if frame_pos < first_onset + int(sr * 1.0):
                    continue
                # Check if preceding window was quiet and current frame bursts into speech
                pre_silence = np.max(energy[max(0, k - pause_frames):k]) < silence_thresh * 0.8
                is_onset = energy[k] > silence_thresh * 1.8 and pre_silence

                if is_onset and (frame_pos - last_onset_pos > min_gap_samples):
                    target_onset = frame_pos
                    p_word_len = min(int(sr * 0.450), total_n - target_onset)
                    if p_word_len > 32 and target_onset - swell_len >= 0:
                        p_word = extended_samples[target_onset:target_onset + p_word_len]
                        p_swoosh = create_reverse_swell(p_word, swell_len)
                        swoosh_track[target_onset - swell_len:target_onset] += p_swoosh * 0.75
                        last_onset_pos = target_onset

        # ── 5. Ghostly Spectral Sculpting on the Swoosh Track ─────────────────
        fft_swoosh = np.fft.rfft(swoosh_track)
        freqs = np.fft.rfftfreq(total_n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # High-pass filter below 180 Hz (eliminates low-frequency mud)
        hp_mask = freqs < 180.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 32.0 * (1.0 - freqs[hp_mask] / 180.0)

        # Haunting vocal presence at 2200 Hz (+2.8 dB)
        gain_db += 2.8 * np.exp(-0.5 * ((freqs - 2200.0) / 600.0) ** 2)

        # Ethereal ghost breath shimmer at 7500 Hz (+2.0 dB)
        air_mask = freqs > 7500.0
        if np.any(air_mask):
            gain_db[air_mask] += 2.0 * np.clip((freqs[air_mask] - 7500.0) / 3000.0, 0.0, 1.0)

        fft_swoosh *= 10.0 ** (gain_db / 20.0)
        conditioned_swoosh = np.fft.irfft(fft_swoosh, total_n)

        # ── 6. Combine Dry Lead Vocal + Rising Ghostly Swoosh ─────────────────
        mix = dry * extended_samples + wet * conditioned_swoosh

        # ── 7. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_m = np.abs(mix)
        over = abs_m > 0.82
        if np.any(over):
            mix[over] = np.sign(mix[over]) * (0.82 + 0.12 * np.tanh((abs_m[over] - 0.82) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(mix))
        if mx > 0:
            mix = (mix / mx) * 0.912

        return mix.astype(np.float32)

    ghost_reverb = reverse_reverb
    pre_verb = reverse_reverb

    # ──────────────────────────────────────────────────────────────
    #  CHORUS (MULTI-VOICE ENSEMBLE & HARMONY THICKENER)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def chorus(
        samples,
        sr,
        voices=4,
        depth_ms=3.2,
        rate_hz=0.45,
        wet=0.55,
        dry=0.82
    ):
        """
        Chorus Voice Effect — multiplies the voice and constantly applies subtle pitch
        and timing variations, thickening a background harmony to sound like a large group.
          • Multi-voice parallel ensemble (4 independent voice clones with asynchronous base delays)
          • Continuous time-varying fractional delay Doppler pitch modulation (±8 to 18 cents)
          • Dual-incommensurate LFOs per voice to prevent repetitive cyclic phasing and comb-filtering
          • Vocal body & group thickening EQ (clean low-cut <140 Hz, warm chest boost around 550 Hz,
            anti-sibilance smoothing >6.5 kHz on ensemble voices)
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        t = np.arange(n, dtype=np.float64) / sr

        # ── 1. Define Multi-Voice Parameters ─────────────────────────────────
        # Base delays between 16ms and 40ms, with prime-ratio LFO rates and depths
        voice_configs = [
            {"base_ms": 16.5, "rate1": 0.38 * (rate_hz / 0.45), "rate2": 0.11, "depth_scale": 0.85, "phase": 0.0},
            {"base_ms": 24.0, "rate1": 0.53 * (rate_hz / 0.45), "rate2": 0.17, "depth_scale": 1.00, "phase": 1.8},
            {"base_ms": 31.5, "rate1": 0.29 * (rate_hz / 0.45), "rate2": 0.13, "depth_scale": 1.15, "phase": 3.7},
            {"base_ms": 39.0, "rate1": 0.71 * (rate_hz / 0.45), "rate2": 0.19, "depth_scale": 1.25, "phase": 5.2},
        ]

        num_v = min(max(voices, 1), 6)
        chorus_voices = np.zeros(n, dtype=np.float64)
        sample_grid = np.arange(n, dtype=np.float64)

        for i in range(num_v):
            cfg = voice_configs[i % len(voice_configs)]
            phase_shift = cfg["phase"] + (i // len(voice_configs)) * 1.5
            d_scale = cfg["depth_scale"] * (depth_ms / 1000.0)

            # Continuous time-varying delay = base + primary LFO + secondary slow drift
            lfo1 = np.sin(2.0 * np.pi * cfg["rate1"] * t + phase_shift)
            lfo2 = np.cos(2.0 * np.pi * cfg["rate2"] * t + phase_shift * 0.7)
            mod_delay_sec = (cfg["base_ms"] / 1000.0) + (0.75 * lfo1 + 0.25 * lfo2) * d_scale
            mod_delay_samples = mod_delay_sec * sr

            # Fractional delay lookup coordinates
            read_indices = sample_grid - mod_delay_samples

            # Linear interpolation lookup for fractional delay
            v_signal = np.interp(read_indices, sample_grid, samples, left=0.0, right=0.0)
            chorus_voices += v_signal

        # Normalize ensemble layer power relative to voice count
        chorus_voices = (chorus_voices / np.sqrt(num_v)).astype(np.float32)

        # ── 2. Vocal Body & Group Thickening EQ on Ensemble ──────────────────
        fft_ens = np.fft.rfft(chorus_voices)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # High-pass filter below 140 Hz (eliminates boomy mud from stacked voices)
        hp_mask = freqs < 140.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 24.0 * (1.0 - freqs[hp_mask] / 140.0)

        # Warm chest resonance & choir body at 550 Hz (+2.2 dB)
        gain_db += 2.2 * np.exp(-0.5 * ((freqs - 550.0) / 220.0) ** 2)

        # Anti-mud scoop at 360 Hz (-1.8 dB) to eliminate boxy acoustic buildup
        gain_db -= 1.8 * np.exp(-0.5 * ((freqs - 360.0) / 100.0) ** 2)

        # Gentle anti-sibilance shelving above 6500 Hz (-2.5 dB)
        lp_mask = freqs > 6500.0
        if np.any(lp_mask):
            ratio = np.clip((freqs[lp_mask] - 6500.0) / 4000.0, 0.0, 1.0)
            gain_db[lp_mask] -= 2.5 * ratio

        fft_ens *= 10.0 ** (gain_db / 20.0)
        thickened_ensemble = np.fft.irfft(fft_ens, n)

        # ── 3. Combine Lead Vocal + Thickened Ensemble ────────────────────────
        mix = dry * samples + wet * thickened_ensemble

        # ── 4. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_m = np.abs(mix)
        over = abs_m > 0.82
        if np.any(over):
            mix[over] = np.sign(mix[over]) * (0.82 + 0.12 * np.tanh((abs_m[over] - 0.82) / 0.12))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(mix))
        if mx > 0:
            mix = (mix / mx) * 0.912

        return mix.astype(np.float32)

    vocal_chorus = chorus
    ensemble = chorus

    # ──────────────────────────────────────────────────────────────
    #  CLIMAX (DRAMATIC SWEEPING METALLIC NOTCHES & VOCAL RISER)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def climax(
        samples,
        sr,
        sweep_rate_hz=0.42,
        depth=0.85,
        feedback=0.76,
        wet=0.68,
        dry=0.62,
        riser_build=True
    ):
        """
        Climax Voice Effect — creates moving frequency notches that sweep back and forth,
        resulting in a dramatic, sweeping, metallic sound. Applied heavily on transitional
        vocal risers for an intense, electrifying climax.
          • Moving frequency notch comb filter sweeping between 0.5ms (2.0 kHz) and 3.8ms (263 Hz)
          • High-resonance regenerative feedback (0.76) with analog BBD damping for cutting metallic bite
          • Dynamic transitional riser acceleration & tension buildup over vocal phrases
          • Sizzling metallic air boost (>4 kHz) and anti-mud sub-cut (<130 Hz)
          • Transparent soft-knee limiter and output normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        t = np.arange(n, dtype=np.float64) / sr
        total_dur = n / sr

        # ── 1. Sweeping LFO & Riser Build Trajectory ─────────────────────────
        if riser_build and total_dur > 1.0:
            # Gradually accelerate sweep frequency from base rate up to 1.85x base rate
            f_start = sweep_rate_hz * 0.75
            f_end = sweep_rate_hz * 1.85
            phase = 2.0 * np.pi * (f_start * t + 0.5 * ((f_end - f_start) / total_dur) * (t ** 2))
            # Resonant feedback intensifies towards phrase transitions and climax
            fb_envelope = np.clip(feedback * (0.88 + 0.22 * (t / total_dur)), 0.50, 0.86)
        else:
            phase = 2.0 * np.pi * sweep_rate_hz * t
            fb_envelope = np.full(n, feedback, dtype=np.float64)

        # Delay sweeps smoothly between 0.5ms and 3.8ms
        min_delay_ms = 0.50
        max_delay_ms = 0.50 + 3.30 * np.clip(depth, 0.2, 1.0)
        mid_delay_ms = 0.5 * (min_delay_ms + max_delay_ms)
        amp_delay_ms = 0.5 * (max_delay_ms - min_delay_ms)

        lfo = np.sin(phase)
        delay_ms_array = mid_delay_ms + amp_delay_ms * lfo
        delay_samples = np.clip(np.round(delay_ms_array * (sr / 1000.0)).astype(np.int32), 6, int(sr * 0.020))

        # ── 2. Resonant Sweeping Comb/Notch Line with Analog BBD Damping ─────
        y = np.zeros(n, dtype=np.float32)
        max_d = int(np.max(delay_samples)) + 10
        prev_fb = 0.0

        for i in range(max_d, n):
            delayed = y[i - delay_samples[i]]
            # 1-pole filter in feedback loop with bright high-frequency preservation
            fb_val = 0.88 * delayed + 0.12 * prev_fb
            prev_fb = delayed
            # Analog diode saturation in feedback loop to tame extreme runaway peaks & add metallic bite
            val = samples[i] + fb_envelope[i] * fb_val
            abs_v = abs(val)
            if abs_v > 1.2:
                val = np.sign(val) * (1.2 + 0.2 * np.tanh((abs_v - 1.2) / 0.2))
            y[i] = val

        # Deep notch subtraction: (dry - y) creates moving spectral nulls
        # while preserving resonant peaks
        notched = dry * samples - wet * y

        # ── 3. Spectral Sculpting for Sizzling Metallic Climax ───────────────
        fft_mix = np.fft.rfft(notched)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # High-pass filter below 130 Hz (strips low-end feedback rumble)
        hp_mask = freqs < 130.0
        if np.any(hp_mask):
            gain_db[hp_mask] -= 26.0 * (1.0 - freqs[hp_mask] / 130.0)

        # Intelligible speech formant boost at 1800 Hz (+1.8 dB)
        gain_db += 1.8 * np.exp(-0.5 * ((freqs - 1800.0) / 450.0) ** 2)

        # Sizzling metallic air shelf above 3500 Hz (+8.2 dB)
        shelf_mask = freqs > 3500.0
        if np.any(shelf_mask):
            ratio = np.clip((freqs[shelf_mask] - 3500.0) / 3500.0, 0.0, 1.0)
            gain_db[shelf_mask] += 8.2 * ratio

        fft_mix *= 10.0 ** (gain_db / 20.0)
        sculpted = np.fft.irfft(fft_mix, n)

        # ── 4. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_s = np.abs(sculpted)
        over = abs_s > 0.80
        if np.any(over):
            sculpted[over] = np.sign(sculpted[over]) * (0.80 + 0.14 * np.tanh((abs_s[over] - 0.80) / 0.14))

        # Peak normalize to -0.8 dBFS (0.912)
        mx = np.max(np.abs(sculpted))
        if mx > 0:
            sculpted = (sculpted / mx) * 0.912

        return sculpted.astype(np.float32)

    vocal_riser = climax
    flanger = climax
    jet_flanger = climax

    # ──────────────────────────────────────────────────────────────
    #  DARTH VADER (SITH BARITONE, HELMET CAVITY & MECHANICAL RESPIRATOR)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _synthesize_vader_breath(sr, breath_type='in'):
        """
        Synthesizes an authentic mechanical scuba regulator breath (intake or exhaust).
        Modeled after Ben Burtt's legendary Darth Vader breathing apparatus.
        """
        if breath_type == 'in':
            dur = 0.75
            t = np.arange(int(sr * dur), dtype=np.float32) / sr
            noise = np.random.randn(len(t)).astype(np.float32)
            f = np.fft.rfftfreq(len(t), 1.0 / sr)
            fft_n = np.fft.rfft(noise)
            w1 = np.exp(-0.5 * ((f - 450.0) / 70.0) ** 2)
            w2 = np.exp(-0.5 * ((f - 1400.0) / 180.0) ** 2)
            fft_n *= (0.75 * w1 + 0.45 * w2)
            breath = np.fft.irfft(fft_n, len(t))
            sin_in = np.maximum(np.sin(np.pi * np.linspace(0.0, 1.0, len(t), dtype=np.float32)), 0.0)
            env = sin_in ** 1.5
            breath *= env
        else:
            dur = 0.90
            t = np.arange(int(sr * dur), dtype=np.float32) / sr
            noise = np.random.randn(len(t)).astype(np.float32)
            f = np.fft.rfftfreq(len(t), 1.0 / sr)
            fft_n = np.fft.rfft(noise)
            w1 = np.exp(-0.5 * ((f - 300.0) / 60.0) ** 2)
            w2 = np.exp(-0.5 * ((f - 850.0) / 120.0) ** 2)
            fft_n *= (0.85 * w1 + 0.35 * w2)
            breath = np.fft.irfft(fft_n, len(t))
            sin_ex = np.maximum(np.sin(np.pi * np.linspace(0.0, 1.0, len(t), dtype=np.float32)), 0.0)
            env = (1.0 - np.linspace(0.0, 1.0, len(t), dtype=np.float32)) * sin_ex
            breath *= env

        mx = np.max(np.abs(breath))
        if mx > 0:
            breath /= mx
        return breath.astype(np.float32)

    @staticmethod
    def darth_vader(
        samples,
        sr,
        semitones=-5.0,
        helmet_res=0.38,
        regulator_mod=0.15,
        respirator_breath=True
    ):
        """
        Darth Vader Voice Effect — deep, intimidating Sith Lord baritone with physical
        fiberglass helmet enclosure comb reflections, measured mask formant peaks,
        subtle scuba regulator diaphragm micro-modulation, and iconic mechanical breathing.
          • -5 semitone phase-vocoded pitch shift (achieving the measured 63.3 Hz fundamental)
          • Helmet cavity comb filter reflections at 1.25ms, 2.55ms, and 3.80ms
          • Parametric mask formants: 130 Hz (+5.5 dB chest), 580 Hz (+4.0 dB throat),
            904 Hz (+4.5 dB face grill), 1205 Hz (+3.0 dB mechanical resonance)
          • Enclosed acoustic mask high-cut rolloff above 3.8 kHz (-18 dB)
          • Subtle mechanical scuba regulator flanging (0.8ms, 0.15 Hz)
          • Iconic mechanical scuba respirator breathing at opening and close
          • Soft-knee console saturation and peak normalization to -0.8 dBFS (0.912).
        """
        n = len(samples)
        if n == 0 or len(samples) < 32:
            return samples

        # ── 1. Pitch Shift Down into Deep Sub-Baritone Register (60-75 Hz) ────
        pitched = AudioEffects.pitch_shift(samples, sr, semitones=semitones)
        p_len = len(pitched)

        # ── 2. Helmet Mask Acoustic Chamber (Comb Filter Reflections) ─────────
        d1 = max(int(round(sr * 0.00125)), 1)
        d2 = max(int(round(sr * 0.00255)), 2)
        d3 = max(int(round(sr * 0.00380)), 3)

        helmet = np.copy(pitched)
        if d1 < p_len:
            helmet[d1:] += (helmet_res * 1.0) * pitched[:-d1]
        if d2 < p_len:
            helmet[d2:] += (helmet_res * 0.65) * pitched[:-d2]
        if d3 < p_len:
            helmet[d3:] += (helmet_res * 0.40) * pitched[:-d3]

        # ── 3. Subtle Scuba Regulator Micro-Modulation ───────────────────────
        if regulator_mod > 0.01:
            t = np.arange(p_len, dtype=np.float32) / sr
            mod_delay_samples = (0.0008 + 0.0004 * np.sin(2 * np.pi * 0.15 * t)) * sr
            read_idx = np.arange(p_len) - mod_delay_samples
            flanged = np.interp(read_idx, np.arange(p_len), helmet, left=0.0, right=0.0)
            helmet = (1.0 - regulator_mod) * helmet + regulator_mod * flanged

        # ── 4. Parametric Mask Formant Sculpting & Acoustic High-Cut ──────────
        fft_h = np.fft.rfft(helmet)
        freqs = np.fft.rfftfreq(p_len, 1.0 / sr)
        gain_db = np.zeros_like(freqs)

        # Peak 1: Massive chest sub-bass resonance at 130 Hz (+5.5 dB)
        gain_db += 5.5 * np.exp(-0.5 * ((freqs - 130.0) / 45.0) ** 2)

        # Peak 2: Mask throat cavity resonance at 580 Hz (+4.0 dB)
        gain_db += 4.0 * np.exp(-0.5 * ((freqs - 580.0) / 120.0) ** 2)

        # Peak 3: Nasal face-grill acoustic peak at 904 Hz (+4.5 dB)
        gain_db += 4.5 * np.exp(-0.5 * ((freqs - 904.0) / 160.0) ** 2)

        # Peak 4: Metallic throat edge at 1205 Hz (+3.0 dB)
        gain_db += 3.0 * np.exp(-0.5 * ((freqs - 1205.0) / 200.0) ** 2)

        # Enclosed mask acoustic high-cut above 3800 Hz (-18 dB)
        hc_mask = freqs > 3800.0
        if np.any(hc_mask):
            ratio = np.clip((freqs[hc_mask] - 3800.0) / 3000.0, 0.0, 1.0)
            gain_db[hc_mask] -= 18.0 * ratio

        fft_h *= 10.0 ** (gain_db / 20.0)
        sculpted = np.fft.irfft(fft_h, p_len)

        # ── 5. Analog Console & Diaphragm Saturation ─────────────────────────
        driven = np.tanh(sculpted * 1.55)

        # ── 6. Iconic Mechanical Respirator Breathing Layer ───────────────────
        if respirator_breath:
            inhale = AudioEffects._synthesize_vader_breath(sr, breath_type='in') * 0.38
            exhale = AudioEffects._synthesize_vader_breath(sr, breath_type='out') * 0.38
            pause_pad = np.zeros(int(sr * 0.15), dtype=np.float32)
            combined = np.concatenate([inhale, pause_pad, driven, pause_pad, exhale])
        else:
            combined = driven

        # ── 7. Soft-Knee Peak Limiter & Output Normalization ──────────────────
        abs_c = np.abs(combined)
        over = abs_c > 0.82
        if np.any(over):
            combined[over] = np.sign(combined[over]) * (0.82 + 0.12 * np.tanh((abs_c[over] - 0.82) / 0.12))

        mx = np.max(np.abs(combined))
        if mx > 0:
            combined = (combined / mx) * 0.912

        return combined.astype(np.float32)

    vader = darth_vader
    sith_lord = darth_vader





