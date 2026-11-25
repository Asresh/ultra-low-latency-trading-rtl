"""Compare real multiclock report transfers with the attached monitor's alerts."""

import argparse
from pathlib import Path

from deadline_alert import DeadlineAlert, read_alerts
from latency_capture import read_event, read_words
from latency_reference import Status


# The live multiclock bench configures these two rule slots with a zero-tick
# threshold and single-report hysteresis. Derive each expected transition from
# both the exact transferred source stream and the installed path filter.
PATH_TO_RULE = {(1, 0, 1): 0, (0, 1, 2): 1}


def expected_alerts(events):
    alarmed = set()
    for event in events:
        if event.status != Status.MATCH:
            continue
        rule = PATH_TO_RULE.get((event.event_class, event.start_probe, event.end_probe))
        if rule is None:
            continue
        late = event.duration > 0
        if late == (rule in alarmed):
            continue
        if late:
            alarmed.add(rule)
        else:
            alarmed.remove(rule)
        yield DeadlineAlert(
            rule, 1, event.event_class, event.tag, event.start_probe, event.end_probe,
            1 if late else 2, 1, 0, event.duration, event.observed_tick,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    args = parser.parse_args()
    try:
        with (args.capture_dir / "events.hex").open() as capture:
            expected = list(expected_alerts(map(read_event, read_words(capture, 320))))
        with (args.capture_dir / "deadline-alerts.txt").open() as capture:
            actual = list(read_alerts(capture))
        if len(expected) != 2:
            raise ValueError(f"expected two independent path transitions, saw {expected}")
        if actual != expected:
            raise ValueError(f"actual alerts {actual} differ from {expected}")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(f"PASS actual multiclock deadline alerts: {len(actual)} complete records from two path rules")


if __name__ == "__main__":
    main()
