import select
import evdev

devices = []
for p in evdev.list_devices():
    dev = evdev.InputDevice(p)
    devices.append(dev)
    print(dev.path, dev.name)

print("\nListening on ALL devices. Press keys on Pi keyboard...")
print("Will show which device produces events. Ctrl+C to stop.\n")

dev_map = {dev.fd: dev for dev in devices}

while True:
    r, _, _ = select.select(devices, [], [], 1.0)
    for dev in r:
        for event in dev.read():
            if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                name = evdev.ecodes.KEY.get(event.code, event.code)
                print(dev.path, dev.name, "->", name)
