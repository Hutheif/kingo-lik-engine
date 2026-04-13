# hybrid_engine.py — smart validation + optional parallel mode
import threading, time, os
from schema_validator import validate_and_normalise

# Set to True to run both engines in parallel and compare
# Set to False for sequential with validation (recommended for production)
PARALLEL_MODE = False

def translate_with_confidence(audio_path: str, session_id: str,
                               cloud_fn, local_fn) -> dict:
    """
    Smart hybrid translation with result validation.
    Implements Option B by default, Option A if PARALLEL_MODE is True.
    """
    if PARALLEL_MODE:
        return _parallel_translate(audio_path, session_id, cloud_fn, local_fn)
    else:
        return _validated_translate(audio_path, session_id, cloud_fn, local_fn)


def _validated_translate(audio_path: str, session_id: str,
                          cloud_fn, local_fn) -> dict:
    """
    Option B — cloud first, validate result, fall back if suspicious.
    Falls back on exceptions AND on bad quality results.
    """
    cloud_result = None
    local_result = None

    # Step 1 — try cloud
    try:
        raw = cloud_fn(audio_path, session_id)
        cloud_result = validate_and_normalise(raw, engine="cloud")
        print(f"[HYBRID] Cloud returned — validating quality...")

        # Step 2 — validate the result
        issues = _detect_quality_issues(cloud_result, audio_path)

        if not issues:
            print(f"[HYBRID] Cloud result passed validation")
            cloud_result["validation"] = "passed"
            return cloud_result
        else:
            print(f"[HYBRID] Cloud result suspicious: {issues}")
            print(f"[HYBRID] Running local engine to verify...")

    except Exception as e:
        print(f"[HYBRID] Cloud failed with exception: {e}")

    # Step 3 — run local (either as fallback or verification)
    try:
        raw = local_fn(audio_path, session_id)
        local_result = validate_and_normalise(raw, engine="local")

        if cloud_result:
            # Both ran — pick the better one
            winner = _pick_better_result(cloud_result, local_result)
            print(f"[HYBRID] Picked {winner['engine']} over "
                  f"{('local' if winner['engine']=='cloud' else 'cloud')}")
            return winner
        else:
            # Cloud completely failed — use local
            local_result["validation"] = "cloud_failed"
            return local_result

    except Exception as e:
        print(f"[HYBRID] Local also failed: {e}")
        if cloud_result:
            # Local failed but cloud ran — use cloud despite issues
            cloud_result["validation"] = "used_despite_issues"
            return cloud_result
        raise Exception(f"Both engines failed: {e}")


def _parallel_translate(audio_path: str, session_id: str,
                         cloud_fn, local_fn) -> dict:
    """
    Option A — run both engines simultaneously, compare and pick best.
    Uses threading to run in parallel — total time = max(cloud, local)
    instead of cloud + local.
    """
    cloud_box = [None]
    local_box  = [None]
    cloud_err  = [None]
    local_err  = [None]

    def run_cloud():
        try:
            raw = cloud_fn(audio_path, session_id)
            cloud_box[0] = validate_and_normalise(raw, engine="cloud")
        except Exception as e:
            cloud_err[0] = e
            print(f"[PARALLEL] Cloud failed: {e}")

    def run_local():
        try:
            raw = local_fn(audio_path, session_id)
            local_box[0] = validate_and_normalise(raw, engine="local")
        except Exception as e:
            local_err[0] = e
            print(f"[PARALLEL] Local failed: {e}")

    # Launch both simultaneously
    t_cloud = threading.Thread(target=run_cloud)
    t_local = threading.Thread(target=run_local)
    t_cloud.start()
    t_local.start()

    # Wait for both to finish (max 60s)
    t_cloud.join(timeout=60)
    t_local.join(timeout=60)

    cloud_result = cloud_box[0]
    local_result  = local_box[0]

    if cloud_result and local_result:
        winner = _pick_better_result(cloud_result, local_result)
        loser  = local_result if winner["engine"] == "cloud" else cloud_result
        winner["parallel_comparison"] = {
            "other_engine":      loser["engine"],
            "other_transcript":  loser.get("transcript", "")[:100],
            "other_confidence":  loser.get("confidence", ""),
            "agreement":         _measure_agreement(cloud_result, local_result)
        }
        print(f"[PARALLEL] Both ran — picked {winner['engine']} "
              f"(agreement: {winner['parallel_comparison']['agreement']}%)")
        return winner
    elif cloud_result:
        print(f"[PARALLEL] Only cloud succeeded")
        return cloud_result
    elif local_result:
        print(f"[PARALLEL] Only local succeeded")
        return local_result
    else:
        raise Exception("Both engines failed in parallel mode")


def _detect_quality_issues(result: dict, audio_path: str) -> list:
    """
    Detects signs that a translation result is suspicious.
    Returns list of issue strings — empty list means result is good.
    """
    issues = []
    transcript   = result.get("transcript", "")
    translation  = result.get("translation", "")
    confidence   = result.get("confidence", "medium")
    detected_lang = result.get("detected_language", "")

    # Issue 1 — no translation happened (identical to transcript)
    if transcript and translation:
        if transcript.strip().lower() == translation.strip().lower():
            issues.append("translation identical to transcript — no translation occurred")

    # Issue 2 — very short transcript relative to audio length
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(audio_path)
        duration_seconds = len(audio) / 1000
        words = len(transcript.split())
        words_per_second = words / duration_seconds if duration_seconds > 0 else 0

        # Normal speech: 2-4 words/second
        # If less than 0.3 words/second, transcript is suspiciously short
        if duration_seconds > 3 and words_per_second < 0.3:
            issues.append(f"transcript too short: {words} words for "
                          f"{duration_seconds:.1f}s audio "
                          f"({words_per_second:.1f} words/sec)")
    except Exception:
        pass

    # Issue 3 — explicit low confidence
    if confidence == "low":
        issues.append("engine reported low confidence")

    # Issue 4 — empty transcript or translation
    if not transcript or len(transcript.strip()) < 3:
        issues.append("transcript is empty or too short")

    if not translation or len(translation.strip()) < 3:
        issues.append("translation is empty or too short")

    # Issue 5 — error message in translation field
    if translation.startswith("Error:"):
        issues.append("translation contains error message")

    return issues


def _pick_better_result(cloud: dict, local: dict) -> dict:
    """
    Scores both results and returns the higher quality one.
    Scoring criteria: confidence, transcript length, translation quality.
    """
    cloud_score = _score_result(cloud)
    local_score  = _score_result(local)

    print(f"[HYBRID] Scores — Cloud: {cloud_score} | Local: {local_score}")

    if cloud_score >= local_score:
        cloud["score"] = cloud_score
        cloud["compared_to_local"] = local_score
        return cloud
    else:
        local["score"] = local_score
        local["compared_to_cloud"] = cloud_score
        return local


def _score_result(result: dict) -> int:
    """
    Scores a translation result 0-100.
    Higher = better quality.
    """
    score = 0
    transcript  = result.get("transcript", "")
    translation = result.get("translation", "")
    confidence  = result.get("confidence", "low")
    keywords    = result.get("urgent_keywords", [])

    # Confidence score (0-30)
    conf_scores = {"high": 30, "medium": 20, "low": 5, "none": 0}
    score += conf_scores.get(confidence, 0)

    # Transcript length score (0-20)
    words = len(transcript.split())
    score += min(words * 2, 20)

    # Translation differs from transcript (0-20)
    # If they differ, actual translation happened
    if transcript and translation:
        if transcript.strip().lower() != translation.strip().lower():
            score += 20

    # Translation length score (0-15)
    trans_words = len(translation.split())
    score += min(trans_words, 15)

    # Urgent keywords detected (0-15)
    # If keywords found, model understood the content
    score += min(len(keywords) * 3, 15)

    # Penalty for error messages
    if translation.startswith("Error:"):
        score -= 50

    return max(score, 0)


def _measure_agreement(cloud: dict, local: dict) -> int:
    """
    Measures how much cloud and local agree on content (0-100%).
    High agreement = both are likely correct.
    Low agreement = one engine may be wrong.
    """
    try:
        from difflib import SequenceMatcher
        cloud_text = (cloud.get("transcript", "") + " " +
                      cloud.get("translation", "")).lower()
        local_text  = (local.get("transcript", "") + " " +
                       local.get("translation", "")).lower()

        ratio = SequenceMatcher(None, cloud_text, local_text).ratio()
        return int(ratio * 100)
    except Exception:
        return 0