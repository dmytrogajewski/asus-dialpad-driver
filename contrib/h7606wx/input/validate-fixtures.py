"""Replay the reduced traces; this never opens devices or executes shortcuts."""
import json
from pathlib import Path
from reference_gestures import Gestures, TouchFrames
from libevdev import InputEvent, evbit

HERE = Path(__file__).resolve().parent
results = []
for path in sorted((HERE / 'fixtures').glob('*.json')):
    fixture = json.loads(path.read_text())
    expected = fixture['expected']
    actions, turns = [], []
    names = ['Volume', 'Scroll', 'Brightness']
    engine = Gestures(fixture['geometry'], names, lambda k, v: actions.append((k, v)))
    engine.set_enabled(expected['initially_enabled'])
    control = Gestures(fixture['geometry'], names, lambda k, v: turns.append((k, v)))
    control.set_enabled(True)
    control.control = True
    frames = TouchFrames()
    contacts = []
    previous = 0.0
    for microseconds, kind, code, value in fixture['events']:
        assert 0 <= microseconds < 10_000_000
        assert kind in (0, 1, 3)  # Touchpad keys/axes and synchronization only.
        if kind == 1:
            assert code >= 256  # No keyboard key events.
        now = microseconds / 1_000_000
        # A stationary hold is timer-driven, not dependent on scan-clock noise.
        tick = previous + 0.05
        while tick < now:
            engine.update(tick, contacts)
            control.update(tick, contacts)
            tick += 0.05
        event_code = evbit(kind, code)
        frame = frames.feed(InputEvent(event_code, value))
        if frame is not None:
            contacts = frame
            engine.update(now, contacts)
            control.update(now, contacts)
        previous = now
    if 'toggle' in expected:
        assert [v for k, v in actions if k == 'toggle'] == expected['toggle']
    if 'center_mode' in expected:
        assert engine.control == expected['center_mode']
    if 'menu' in expected:
        assert sorted({v[0] for k, v in actions if k == 'display'}) == expected['menu']
    if 'rotation_directions' in expected:
        assert sorted({v[1] for k, v in turns if k == 'rotate'}) == expected['rotation_directions']
    results.append({'fixture': path.name, 'events': len(fixture['events']), 'passed': True})
    print(f'{path.name}: {len(fixture["events"])} events, replay passed')
assert len(results) == 3
