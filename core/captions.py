"""
core/captions.py  –  The spoken text of a caption or text file

load_text(path) returns what the captions say, ready for the Text Input box:
cue numbers, timestamps, styling tags and sound notes such as [Music] are
removed, and cues are joined into sentences. A pause of PARAGRAPH_GAP seconds
or more after a finished sentence starts a new paragraph.

Formats: SRT, WebVTT, ASS/SSA, MicroDVD (.sub), SAMI, TTML/DFXP through
pysubs2, YouTube .sbv here, and plain .txt. A .txt that turns out to hold
captions is read as captions.

Files are read as UTF-8 (with or without BOM) or UTF-16 with a BOM, and as
cp1252 otherwise, so curly quotes from Windows editors survive.
"""

import codecs
import html
import os
import re

CAPTION_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa", ".sub", ".sbv", ".smi", ".sami", ".ttml", ".dfxp")
TEXT_EXTENSIONS = (".txt",)
IMPORT_EXTENSIONS = CAPTION_EXTENSIONS + TEXT_EXTENSIONS
_PYSUBS2_FORMATS = {".srt": "srt", ".vtt": "vtt", ".ass": "ass", ".ssa": "ssa", ".sub": "microdvd",
                    ".smi": "sami", ".sami": "sami", ".ttml": "ttml", ".dfxp": "ttml"}
PARAGRAPH_GAP = 2.0                 # seconds of silence that may start a paragraph
MAX_BYTES = 10 * 1024 * 1024
_SENTENCE_END = tuple('.!?…"\'”’')

_TAG = re.compile(r"<[^>]*>")                                # <v Roger>, <c.yellow>, <00:00:01.000>
_BRACKETS = re.compile(r"\[[^\]]*\]")                        # [Music], [APPLAUSE]
_SDH_PARENS = re.compile(r"\((?=[^)]*[A-Z])[^a-z)]*\)")      # (LAUGHS), (DOOR SLAMS); keeps (like this)
_SPEAKER = re.compile(r"^[A-Z][A-Z0-9 .'\-]{1,30}:\s+")      # "JOHN: ", "DR. SMITH: "
_DASH = re.compile(r"^[-–—]\s*")                             # dialogue dash at line start
_NOTES = re.compile(r"[♪♫]+")
_SBV_TIME = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,3}),(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,3})\s*$")


class CaptionError(ValueError):
    """The file cannot be read, or holds no text to speak."""


def decode(data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def clean_line(line: str) -> str:
    """One caption line without tags, sound notes, speaker labels or dashes."""
    line = html.unescape(_TAG.sub("", line))
    line = _NOTES.sub("", _SDH_PARENS.sub("", _BRACKETS.sub("", line)))
    line = _DASH.sub("", _SPEAKER.sub("", line.strip()))
    return " ".join(line.split())


def starts_paragraph(prev_text, prev_end, start) -> bool:
    """True when text starting at *start* follows a finished sentence after a long pause."""
    return (bool(prev_text) and prev_end is not None and start - prev_end >= PARAGRAPH_GAP
            and prev_text.rstrip().endswith(_SENTENCE_END))


def join_cues(cues) -> str:
    """[(start_s, end_s, text)] -> paragraphs of plain text."""
    paragraphs, current = [], []
    last_line, last_end = None, None
    for start, end, text in sorted(cues, key=lambda c: c[0]):
        lines = [l for l in (clean_line(x) for x in text.splitlines()) if l]
        if not lines:
            continue
        if current and starts_paragraph(current[-1], last_end, start):
            paragraphs.append(" ".join(current))
            current = []
        for line in lines:
            # Rolling captions (YouTube auto-captions) repeat the previous line.
            if line != last_line:
                current.append(line)
                last_line = line
        last_end = end if last_end is None else max(last_end, end)
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def clean_plain(text: str) -> str:
    """Plain text with tidy line ends and at most one blank line in a row."""
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _parse_pysubs2(text, fmt=None):
    import pysubs2
    subs = pysubs2.SSAFile.from_string(text, format_=fmt, fps=25.0)
    return [(ev.start / 1000.0, ev.end / 1000.0, ev.plaintext)
            for ev in subs if not ev.is_comment and not ev.is_drawing]


def _parse_sbv(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        m = _SBV_TIME.match(lines[0].strip()) if lines else None
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in m.groups())
        cues.append((h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0,
                     h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0, "\n".join(lines[1:])))
    if not cues:
        raise CaptionError("No captions found in this .sbv file.")
    return cues


def load_text(path) -> str:
    """The text to speak from a caption or text file. Raises CaptionError or OSError."""
    if os.path.getsize(path) > MAX_BYTES:
        raise CaptionError(f"This file is larger than {MAX_BYTES // (1024 * 1024)} MB. "
                           "Caption and text files are usually much smaller.")
    with open(path, "rb") as f:
        raw = decode(f.read())
    ext = os.path.splitext(path)[1].lower()
    if ext == ".sbv":
        text = join_cues(_parse_sbv(raw))
    elif ext in _PYSUBS2_FORMATS:
        try:
            cues = _parse_pysubs2(raw, _PYSUBS2_FORMATS[ext])
        except Exception as e:
            raise CaptionError(f"This does not look like a valid {ext} file ({e}).") from e
        text = join_cues(cues)
    else:
        # .txt or anything else: captions if pysubs2 recognises them, else plain text
        try:
            cues = _parse_pysubs2(raw)
        except Exception:
            cues = None
        text = join_cues(cues) if cues else clean_plain(raw)
    if not text.strip():
        raise CaptionError("No text to speak was found in this file.")
    return text
