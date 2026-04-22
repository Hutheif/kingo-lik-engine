# hybrid_engine.py — smart validation + optional parallel mode
import threading, time, os
from schema_validator import validate_and_normalise

PARALLEL_MODE = False


def translate_with_confidence(audio_path: str, session_id: str,
                               cloud_fn, local_fn) -> dict:
    if PARALLEL_MODE:
        return _parallel_translate(audio_path, session_id, cloud_fn, local_fn)
    else:
        return _validated_translate(audio_path, session_id, cloud_fn, local_fn)


def _validated_translate(audio_path: str, session_id: str,
                          cloud_fn, local_fn) -> dict:
    cloud_result = None
    local_result  = None

    # ── Step 1: try cloud ────────────────────────────────────────────────────
    try:
        raw = cloud_fn(audio_path, session_id)
        if raw is None:
            print(f"[HYBRID] Cloud returned None [{session_id[-8:]}] — skipping to local")
        else:
            cloud_result = validate_and_normalise(raw, engine="cloud")
            print(f"[HYBRID] Cloud returned — validating quality...")

            if isinstance(cloud_result, dict):
                transcript = cloud_result.get("transcript", "")
                if len(transcript) < 3:
                    print(f"[HYBRID] Cloud result too short — running local...")
                else:
                    print(f"[HYBRID] Cloud result passed validation")
                    cloud_result["validation"] = "passed"
                    return cloud_result
            else:
                cloud_result = None

    except Exception as e:
        print(f"[HYBRID] Cloud failed: {e}")

    # ── Step 2: run local ────────────────────────────────────────────────────
    try:
        raw = local_fn(audio_path, session_id)
        if raw is None:
            raise ValueError("Local engine returned None")

        local_result = validate_and_normalise(raw, engine="local")

        if cloud_result and isinstance(cloud_result, dict):
            return _pick_better_result(cloud_result, local_result)
        else:
            if local_result:
                local_result["validation"] = "cloud_failed"
                return local_result

    except (KeyboardInterrupt, SystemExit):
        # ── FIX: Never let KeyboardInterrupt kill a translation thread ───────
        # When the main process gets Ctrl+C, Python propagates KeyboardInterrupt
        # into all threads. We catch it here so the session still gets saved.
        print(f"[HYBRID] KeyboardInterrupt caught in translation thread [{session_id[-8:]}] — saving partial result")
        fallback = cloud_result if isinstance(cloud_result, dict) else None
        if fallback:
            fallback["validation"] = "interrupted"
            return fallback
        return {
            "transcript": "",
            "translation": "Translation interrupted — server restarting",
            "detected_language": "unknown",
            "urgent_keywords": [],
            "confidence": "none",
            "engine": "interrupted",
            "requires_review": True,
        }

    except Exception as e:
        print(f"[HYBRID] Local also failed: {e}")
        if cloud_result and isinstance(cloud_result, dict):
            cloud_result["validation"] = "used_despite_issues"
            return cloud_result

        return {
            "transcript": "",
            "translation": "Error: Both engines failed",
            "detected_language": "unknown",
            "urgent_keywords": [],
            "confidence": "none",
            "engine": "none",
            "requires_review": True,
        }


def _pick_better_result(cloud: dict, local: dict) -> dict:
    """Pick the result with more content and higher confidence."""
    if not isinstance(cloud, dict):
        return local
    if not isinstance(local, dict):
        return cloud

    conf_score = {"high": 3, "medium": 2, "low": 1, "none": 0}
    cloud_score = (
        conf_score.get(cloud.get("confidence", "none"), 0) * 2
        + len(cloud.get("translation", ""))
    )
    local_score = (
        conf_score.get(local.get("confidence", "none"), 0) * 2
        + len(local.get("translation", ""))
    )
    winner = cloud if cloud_score >= local_score else local
    winner["validation"] = "hybrid_selected"
    return winner


def _parallel_translate(audio_path: str, session_id: str,
                         cloud_fn, local_fn) -> dict:
    cloud_box = [None]
    local_box  = [None]

    def run_cloud():
        try:
            raw = cloud_fn(audio_path, session_id)
            if raw is not None:
                cloud_box[0] = validate_and_normalise(raw, engine="cloud")
        except (KeyboardInterrupt, SystemExit):
            print(f"[PARALLEL] Cloud thread interrupted [{session_id[-8:]}]")
        except Exception as e:
            print(f"[PARALLEL] Cloud failed: {e}")

    def run_local():
        try:
            raw = local_fn(audio_path, session_id)
            if raw is not None:
                local_box[0] = validate_and_normalise(raw, engine="local")
        except (KeyboardInterrupt, SystemExit):
            print(f"[PARALLEL] Local thread interrupted [{session_id[-8:]}]")
        except Exception as e:
            print(f"[PARALLEL] Local failed: {e}")

    t_cloud = threading.Thread(target=run_cloud, daemon=True)
    t_local  = threading.Thread(target=run_local,  daemon=True)
    t_cloud.start()
    t_local.start()
    t_cloud.join(timeout=90)
    t_local.join(timeout=90)

    cloud_result = cloud_box[0]
    local_result  = local_box[0]

    if isinstance(cloud_result, dict) and isinstance(local_result, dict):
        return _pick_better_result(cloud_result, local_result)
    elif isinstance(cloud_result, dict):
        return cloud_result
    elif isinstance(local_result, dict):
        return local_result
    else:
        return {
            "translation": "Error: Parallel engines failed",
            "transcript": "",
            "detected_language": "unknown",
            "urgent_keywords": [],
            "confidence": "none",
            "engine": "none",
            "requires_review": True,
        }