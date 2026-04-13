from copilot import get_copilot_response

r = get_copilot_response("What are the top urgent keywords today?")
print(r["text"])
print("Mode:", r["mode"])