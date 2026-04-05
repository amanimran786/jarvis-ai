import sys
import api
import agents

# Start API server in background (shared memory/model state with main process)
api.start()

if "--no-ui" in sys.argv:
    # Terminal-only fallback mode
    import re
    from router import route_stream, set_timer_callback
    from voice import speak, speak_stream, listen, wait_for_wake_word
    from brain import ask as ask_gpt
    import memory as mem
    import briefing
    import tools
    import google_services as gs

    END_CONVERSATION = {"that's all", "that's it", "done", "thank you", "thanks", "stop listening"}
    QUIT_PHRASES = {"quit", "exit", "goodbye", "bye", "shut down"}

    def on_timer_done(label):
        speak(f"Time's up. Your {label} timer is done.")

    def run_briefing(facts):
        try:
            speak(briefing.build_briefing(facts))
            speak(f"Weather: {tools.get_weather()}")
            speak(gs.get_todays_events())
            speak(gs.get_unread_emails(max_results=3))
        except Exception as e:
            print(f"[Briefing Error] {e}")

    def handle_memory(user_input):
        lower = user_input.lower().strip()
        if lower.startswith("remember "):
            fact = user_input[9:].strip()
            mem.add_fact(fact)
            speak(f"Got it. I'll remember that {fact}.")
            return True
        if lower.startswith("forget "):
            keyword = user_input[7:].strip()
            speak(f"Forgotten." if mem.forget(keyword) else f"Nothing saved about {keyword}.")
            return True
        if any(p in lower for p in ("give me a briefing", "catch me up", "what did i miss")):
            run_briefing(mem.list_facts())
            return True
        return False

    def conversation_loop():
        speak("Yes?")
        misses = 0
        exchanges = []
        while True:
            user_input = listen()
            if not user_input:
                misses += 1
                if misses >= 2:
                    return True
                speak("Still here.")
                continue
            misses = 0
            lower = user_input.lower().strip()
            if lower in QUIT_PHRASES:
                speak("Goodbye.")
                return False
            if lower in END_CONVERSATION:
                speak("Alright.")
                return True
            if handle_memory(user_input):
                continue
            try:
                stream, model = route_stream(user_input)
                print(f"[{model}]")
                speak_stream(stream)
            except Exception as e:
                speak("Sorry, something went wrong.")

    def main():
        set_timer_callback(on_timer_done)
        speak("Online.")
        while True:
            wait_for_wake_word()
            if not conversation_loop():
                break

    main()

else:
    # Default: launch the GUI
    from ui import run
    run()
