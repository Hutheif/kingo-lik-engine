# audio_processor.py
import os, hashlib, time
from pydub import AudioSegment
from pydub.effects import high_pass_filter, normalize

# Store (file_hash, session_id) pairs
# Same file is OK for different sessions — only blocks exact same session reprocessing
PROCESSED_PAIRS = set()

def get_audio_hash(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def is_duplicate(file_path: str, session_id: str = "unknown") -> bool:
    h    = get_audio_hash(file_path)
    pair = (h, session_id)
    if pair in PROCESSED_PAIRS:
        print(f"[AUDIO] Duplicate — already processed this file for session {session_id[-8:]}")
        return True
    PROCESSED_PAIRS.add(pair)
    return False

# FIX: added session_id parameter so output is named per-session
# instead of colliding as <input>_clean.wav for every session
def process_audio(input_path: str, session_id: str = "unknown") -> str:
    audio = AudioSegment.from_file(input_path)

    original_ms = len(audio)
    print(f"[AUDIO] Original: {original_ms/1000:.1f}s | "
          f"channels: {audio.channels} | "
          f"rate: {audio.frame_rate}Hz | "
          f"size: {os.path.getsize(input_path)} bytes")

    audio = high_pass_filter(audio, cutoff=300)
    audio = normalize(audio)
    audio = audio.set_frame_rate(16000).set_channels(1)
    audio = audio.set_sample_width(2)

    # Output named after session so each session gets its own clean file
    recordings_dir = os.path.dirname(input_path)
    out_path = os.path.join(recordings_dir, f"{session_id}_raw_clean.wav")

    audio.export(out_path, format="wav", parameters=["-acodec", "pcm_s16le"])

    prev_size = -1
    for _ in range(20):
        curr_size = os.path.getsize(out_path)
        if curr_size == prev_size and curr_size > 0:
            break
        prev_size = curr_size
        time.sleep(0.1)

    final_size   = os.path.getsize(out_path)
    cleaned_secs = len(AudioSegment.from_file(out_path)) / 1000
    print(f"[AUDIO] Cleaned: {cleaned_secs:.1f}s | {final_size} bytes → {out_path}")
    return out_path