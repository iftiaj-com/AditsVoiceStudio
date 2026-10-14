from core.model_cache import MODEL_CACHE

_WHISPER_KEY = ("whisper", "base")


class SubtitleCancelled(Exception):
    """Raised when the user cancels mid-transcription."""


def get_whisper_model():
    """Return the cached Whisper model, loading it on first use.

    The model stays resident between runs and is evicted automatically by
    MODEL_CACHE after 20 minutes of inactivity, so back-to-back generations
    skip the multi-second reload without permanently hogging VRAM.
    """
    def _load():
        from core.gpu import cuda_available, use_torch_cudnn
        use_torch_cudnn()                   # before ctranslate2 loads its own cudnn64_9.dll
        from faster_whisper import WhisperModel
        from core.paths import bundle_dir
        use_cuda = cuda_available()
        # Installed builds ship Systran/faster-whisper-base so subtitles work
        # offline; from source it comes from the Hugging Face cache.
        bundled = bundle_dir() / "models" / "whisper-base"
        # Base model, sized for ~4GB VRAM
        return WhisperModel(
            model_size_or_path=str(bundled) if (bundled / "model.bin").exists() else "base",
            device="cuda" if use_cuda else "cpu",
            compute_type="float16" if use_cuda else "int8",
        )
    return MODEL_CACHE.get(_WHISPER_KEY, _load)


def transcribe_file(path, on_segment, check_cancel=None, status_cb=None):
    """Transcribe an audio or video file, calling on_segment(start, end, text,
    duration) for each segment as Whisper finishes it. The language is
    detected. Returns faster-whisper's info (language, duration).

    Whisper decodes the file itself (PyAV); a file it cannot open goes through
    core.audio_io.load_segment(), which also tries libsndfile.
    Raises SubtitleCancelled when check_cancel() turns True.
    """
    import numpy as np

    def _cancelled():
        return check_cancel is not None and check_cancel()

    if status_cb:
        status_cb("Loading transcription model...")
    model = get_whisper_model()
    if _cancelled():
        raise SubtitleCancelled()
    if status_cb:
        status_cb("Reading the file...")
    try:
        segments, info = model.transcribe(str(path), vad_filter=True)
    except Exception:
        from core.audio_io import load_segment
        seg = load_segment(str(path)).set_frame_rate(16000).set_channels(1).set_sample_width(2)
        audio = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = model.transcribe(audio, vad_filter=True)
    if status_cb:
        status_cb("Transcribing...")
    for segment in segments:                # lazy: each step decodes one segment
        if _cancelled():
            raise SubtitleCancelled()
        MODEL_CACHE.touch(_WHISPER_KEY)     # a long file must not be evicted mid-run
        on_segment(segment.start, segment.end, segment.text, info.duration)
    return info


def unload_whisper_model():
    """Manually release the Whisper model (RAM + VRAM)."""
    MODEL_CACHE.clear(_WHISPER_KEY)

def format_timestamp(seconds: float) -> str:
    """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    msecs = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{msecs:03d}"

def group_words_into_phrases(words, max_chars=40, max_duration=3.0, max_gap=0.5):
    """
    Groups a list of faster-whisper word objects into phrases/sentences.
    Each word object has: word (str), start (float), end (float).
    """
    if not words:
        return []
        
    phrases = []
    current_words = []
    current_start = None
    
    for word_obj in words:
        word_text = word_obj.word
        word_start = word_obj.start
        word_end = word_obj.end
        
        word_clean = word_text.strip()
        if not word_clean:
            continue
            
        if current_start is None:
            current_start = word_start
            
        should_split = False
        
        if current_words:
            last_word_obj = current_words[-1]
            last_word_clean = last_word_obj["word"]
            
            # 1. Punctuation split
            if last_word_clean and last_word_clean[-1] in ('.', '?', '!'):
                should_split = True
            # 2. Significant pause split
            elif word_start - last_word_obj["end"] > max_gap:
                should_split = True
            else:
                current_text = " ".join([w["word"] for w in current_words]) + " " + word_clean
                # 3. Character limit split
                if len(current_text) > max_chars:
                    should_split = True
                # 4. Duration split
                elif word_end - current_start > max_duration:
                    should_split = True
                    
        if should_split and current_words:
            phrases.append({
                "start": current_start,
                "end": current_words[-1]["end"],
                "text": " ".join([w["word"] for w in current_words])
            })
            current_words = []
            current_start = word_start
            
        current_words.append({
            "word": word_clean,
            "start": word_start,
            "end": word_end
        })
        
    if current_words:
        phrases.append({
            "start": current_start,
            "end": current_words[-1]["end"],
            "text": " ".join([w["word"] for w in current_words])
        })
        
    return phrases

def generate_subtitles(audio_path: str, output_srt_path: str, status_cb=None,
                       segmentation="word", check_cancel=None):
    """
    Generate subtitles using faster-whisper.
    segmentation: "word" for word-level, "sentence" for sentence/phrase-level.
    check_cancel: optional callable returning True when the user cancelled;
                  raises SubtitleCancelled so no partial SRT is written.
    """
    def _cancelled():
        return check_cancel is not None and check_cancel()

    if status_cb:
        status_cb("Loading transcription model...")

    model = get_whisper_model()

    if status_cb:
        status_cb("Generating subtitles...")

    if _cancelled():
        raise SubtitleCancelled()

    # transcribe() returns a lazy generator — cancellation is checked between
    # segments, so a long transcription aborts within one segment's latency.
    segments, info = model.transcribe(audio_path, word_timestamps=True)

    all_words = []
    for segment in segments:
        if _cancelled():
            raise SubtitleCancelled()
        if segment.words:
            all_words.extend(segment.words)

    with open(output_srt_path, "w", encoding="utf-8") as f:
        if segmentation == "sentence" or segmentation == "phrase":
            phrases = group_words_into_phrases(all_words)
            for srt_index, phrase in enumerate(phrases, 1):
                start_str = format_timestamp(phrase["start"])
                end_str = format_timestamp(phrase["end"])
                f.write(f"{srt_index}\n")
                f.write(f"{start_str} --> {end_str}\n")
                f.write(f"{phrase['text']}\n\n")
        else:
            srt_index = 1
            for word in all_words:
                start_str = format_timestamp(word.start)
                end_str = format_timestamp(word.end)
                f.write(f"{srt_index}\n")
                f.write(f"{start_str} --> {end_str}\n")
                f.write(f"{word.word.strip()}\n\n")
                srt_index += 1

    # NOTE: the model intentionally stays cached — MODEL_CACHE evicts it
    # after 5 idle minutes, and unload_whisper_model() frees it on demand.

    if status_cb:
        status_cb("Subtitles generated successfully.")
