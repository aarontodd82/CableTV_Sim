import evdev

devices = evdev.list_devices()
if not devices:
    print("No devices found")
else:
    for p in devices:
        d = evdev.InputDevice(p)
        print(d.path, d.name)
