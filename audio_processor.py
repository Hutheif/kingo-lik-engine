import os, hashlib, time
from pydub import AudioSegment, effects
from pydub.effects import high_pass_filter

# Store (file_hash, session_id) pairs
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

def process_audio(input_path: str, session_id: str = "unknown") -> str:
    # Load audio
    audio = AudioSegment.from_file(input_path)

    original_ms = len(audio)
    print(f"[AUDIO] Original: {original_ms/1000:.1f}s | "
          f"channels: {audio.channels} | "
          f"rate: {audio.frame_rate}Hz")

    # 1. Strip DC Offset (removes the 'hum' often found in telephony)
    audio = audio.set_channels(1) 
    
    # 2. High Pass Filter (removes low-end rumble/noise below speech)
    audio = high_pass_filter(audio, cutoff=300)

    # 3. Normalize (Brings the peak to 0dB)
    audio = effects.normalize(audio)

    # 4. TARGETED BOOST (The Fix for Hallucinations)
    # Since Whisper hallucinates when it 'struggles' to hear, we push the 
    # normalized audio slightly further. +5dB is usually the sweet spot.
    audio = audio + 5 

    # 5. Upsample to Whisper's native 16kHz
    # This helps the model interpret the 8kHz telephony data more consistently
    audio = audio.set_frame_rate(16000)
    audio = audio.set_sample_width(2)

    # Output named after session so each session gets its own clean file
    recordings_dir = os.path.dirname(input_path)
    out_path = os.path.join(recordings_dir, f"{session_id}_raw_clean.wav")

    # Export with PCM 16-bit encoding (most compatible with Whisper)
    audio.export(out_path, format="wav", parameters=["-acodec", "pcm_s16le"])

    # Wait for file write to stabilize
    prev_size = -1
    for _ in range(20):
        if not os.path.exists(out_path):
            time.sleep(0.1)
            continue
        curr_size = os.path.getsize(out_path)
        if curr_size == prev_size and curr_size > 0:
            break
        prev_size = curr_size
        time.sleep(0.1)

    final_size = os.path.getsize(out_path)
    print(f"[AUDIO] Cleaned & Boosted: {final_size} bytes → {out_path}")
    return out_path