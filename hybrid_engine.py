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
    """
    if PARALLEL_MODE:
        return _parallel_translate(audio_path, session_id, cloud_fn, local_fn)
    else:
        return _validated_translate(audio_path, session_id, cloud_fn, local_fn)


def _validated_translate(audio_path: str, session_id: str,
                          cloud_fn, local_fn) -> dict:
    """
    Option B — cloud first, validate result, fall back if suspicious.
    """
    cloud_result = None
    local_result = None

    # Step 1 — try cloud
    try:
        raw = cloud_fn(audio_path, session_id)
        
        # FIX: Guard against NoneType before validation
        if raw is None:
            print(f"[HYBRID] Cloud returned None for {session_id} - skipping to local")
        else:
            cloud_result = validate_and_normalise(raw, engine="cloud")
            print(f"[HYBRID] Cloud returned — validating quality...")

            # Step 2 — validate the result
            if isinstance(cloud_result, dict):
                # Using .get() to prevent crashes if keys are missing
                transcript = cloud_result.get("transcript", "")
                
                # Check for quality issues (hallucinations or empty data)
                if len(transcript) < 3:
                    print(f"[HYBRID] Cloud result too short/empty. Running local...")
                else:
                    print(f"[HYBRID] Cloud result passed validation")
                    cloud_result["validation"] = "passed"
                    return cloud_result
            else:
                cloud_result = None 

    except Exception as e:
        print(f"[HYBRID] Cloud failed with exception: {e}")

    # Step 3 — run local (either as fallback or verification)
    try:
        raw = local_fn(audio_path, session_id)
        
        if raw is None:
            raise ValueError("Local engine returned None")
            
        local_result = validate_and_normalise(raw, engine="local")

        if cloud_result and isinstance(cloud_result, dict):
            # Both ran — pick the better one (defined in your helper)
            from hybrid_engine import _pick_better_result
            winner = _pick_better_result(cloud_result, local_result)
            return winner
        else:
            # Cloud completely failed or was None — use local
            if local_result:
                local_result["validation"] = "cloud_failed"
                return local_result
            
    except Exception as e:
        print(f"[HYBRID] Local also failed: {e}")
        if cloud_result and isinstance(cloud_result, dict):
            cloud_result["validation"] = "used_despite_issues"
            return cloud_result
        
        return {
            "transcript": "", 
            "translation": "Error: Both engines failed", 
            "success": False,
            "engine": "none"
        }


def _parallel_translate(audio_path: str, session_id: str,
                         cloud_fn, local_fn) -> dict:
    """
    Option A — run both engines simultaneously.
    """
    cloud_box = [None]
    local_box  = [None]

    def run_cloud():
        try:
            raw = cloud_fn(audio_path, session_id)
            if raw is not None:
                cloud_box[0] = validate_and_normalise(raw, engine="cloud")
        except Exception as e:
            print(f"[PARALLEL] Cloud failed: {e}")

    def run_local():
        try:
            raw = local_fn(audio_path, session_id)
            if raw is not None:
                local_box[0] = validate_and_normalise(raw, engine="local")
        except Exception as e:
            print(f"[PARALLEL] Local failed: {e}")

    t_cloud = threading.Thread(target=run_cloud)
    t_local = threading.Thread(target=run_local)
    t_cloud.start()
    t_local.start()

    t_cloud.join(timeout=90) # Match your new patience level
    t_local.join(timeout=90)

    cloud_result = cloud_box[0]
    local_result  = local_box[0]

    if isinstance(cloud_result, dict) and isinstance(local_result, dict):
        from hybrid_engine import _pick_better_result
        return _pick_better_result(cloud_result, local_result)
    elif isinstance(cloud_result, dict):
        return cloud_result
    elif isinstance(local_result, dict):
        return local_result
    else:
        return {"translation": "Error: Parallel engines failed", "success": False, "engine": "none"}