import evdev

# Find keyboard
devices = [evdev.InputDevice(p) for p in evdev.list_devices()]
kb = None
for dev in devices:
    if "Keyboard" in dev.name:
        kb = dev
        break

if not kb:
    print("No keyboard found")
else:
    print("Found:", kb.path, kb.name)
    print("Press keys (Ctrl+C to stop)...")
    try:
        kb.grab()
        print("Grabbed device OK")
    except Exception as e:
        print("Grab failed:", e)
    try:
        for event in kb.read_loop():
            if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                print("Key:", event.code, evdev.ecodes.KEY.get(event.code, "unknown"))
    except KeyboardInterrupt:
        print("Done")
    finally:
        try:
            kb.ungrab()
        except Exception:
            pass
