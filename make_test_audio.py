# make_test_audio.py — generates test WAV files for all 7 languages
# Run once: python make_test_audio.py
# Requires: pip install gtts pydub

import os

os.makedirs("recordings", exist_ok=True)

# (filename, gtts_lang, text)
# Note: gtts supports sw (Kiswahili), en, so (Somali), ar (Arabic)
# For Kikuyu, Dholuo, Turkana — we use English pronunciation as gtts
# has no native support, but the keywords are still in the text for detection
samples = [
    ("test_sw_fire",
     "sw",
     "Haraka, moto mkubwa umeonekana kwenye kichaka karibu na hema za matibabu."),

    ("test_en_attack",
     "en",
     "Help, we are under attack. Armed soldiers are near the camp. People are bleeding."),

    ("test_so_water",
     "en",   # gtts has no Somali — keywords still detected in text
     "Biyo ma jiro halkan. Carruurta waxay u baahan yihiin gargaar degdeg ah."),

    ("test_ar_medical",
     "ar",
     "حريق كبير بالقرب من المخيم. نحتاج مساعدة عاجلة. هناك جرحى."),

    ("test_ki_fire",
     "en",   # gtts has no Kikuyu — record manually for best results
     "Mwaki munene uri thi ino. Tuheo thahu. Mundu ari na ndawa."),

    ("test_luo_flood",
     "en",   # gtts has no Dholuo
     "Mach maduong ni e dala. Kony koro. Pi onge. Kech malit."),

    ("test_tuk_emergency",
     "en",   # gtts has no Turkana
     "Apese ngosi lokwae. Tukoi akuj. Ngosi ekitoi."),
]

try:
    from gtts import gTTS
    from pydub import AudioSegment
except ImportError:
    print("Installing required packages...")
    os.system("pip install gtts pydub")
    from gtts import gTTS
    from pydub import AudioSegment

created = []
failed  = []

for filename, lang, text in samples:
    wav_path = os.path.join("recordings", f"{filename}.wav")
    mp3_path = wav_path.replace(".wav", ".mp3")

    if os.path.exists(wav_path):
        print(f"  EXISTS   {filename}.wav")
        created.append(filename)
        continue

    try:
        tts = gTTS(text=text, lang=lang, slow=False)
        tts.save(mp3_path)
        AudioSegment.from_mp3(mp3_path).export(wav_path, format="wav")
        os.remove(mp3_path)
        size = os.path.getsize(wav_path)
        print(f"  CREATED  {filename}.wav  ({size} bytes)")
        created.append(filename)
    except Exception as e:
        print(f"  FAILED   {filename}: {e}")
        failed.append(filename)

print(f"\nDone. {len(created)} created, {len(failed)} failed.")
print("\nTest each one:")
for name in created:
    print(f"  http://localhost:5000/test/file/{name}.wav")