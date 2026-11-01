from datetime import datetime, timedelta


class FakeClock:
    """A Clock whose time only moves when told to; sleeping jumps straight to the target."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.sleeps: list[datetime] = []

    def now(self) -> datetime:
        return self.current

    def sleep_until(self, when: datetime) -> None:
        self.sleeps.append(when)
        self.current = max(self.current, when)

    def advance(self, delta: timedelta) -> None:
        self.current += delta
