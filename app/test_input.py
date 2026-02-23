import evdev

for p in evdev.list_devices():
    dev = evdev.InputDevice(p)
    caps = dev.capabilities()
    key_caps = caps.get(evdev.ecodes.EV_KEY, [])
    has_a = evdev.ecodes.KEY_A in key_caps
    has_up = evdev.ecodes.KEY_UP in key_caps
    has_0 = evdev.ecodes.KEY_0 in key_caps
    print(dev.path, dev.name)
    print("  KEY_A:", has_a, " KEY_UP:", has_up, " KEY_0:", has_0, " total keys:", len(key_caps))
