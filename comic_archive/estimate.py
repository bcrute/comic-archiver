"""How much is left to save, and roughly how long it will take and how much room it needs.

A guess for the person watching, from counts and times the inventory already
keeps. Nothing here contacts a site or changes a file.
"""

# The pace is that of the most recent five minutes of downloading: the newest batches that add up to that much
# work, so it follows a change of speed within minutes and one quick or slow batch does not swing it.
PACE_SECONDS = 300
# A wait this short between two batches is the hand-over and part of the work. A longer one is not downloading:
# a pause, a wait for the connection, or the app being closed.
JOINED_SECONDS = 30
ENOUGH_SECONDS, ENOUGH_EPISODES = PACE_SECONDS, 3
# What a library holds, measured on one large library saved in full (about 900 series, 12,000 episodes, 240 GB): the
# episodes a series has to save and the room one saved episode takes. They are fixed figures on purpose. The few
# series or episodes a download has got to say little about the rest: the first four series of that library held
# nine times the average.
# The per-series figure is the average of the series with more than three episodes to save. About half of that
# library's series held only their free first episodes, so the estimate comes out up to twice too high for a
# library like it. Too much room set aside is the better mistake, and a library with fewer such series is not
# put too low.
EPISODES_PER_SERIES = 24
EPISODE_BYTES = 20_000_000


def pace(runs):
    """Episodes saved and seconds worked over the most recent runs, newest first, up to PACE_SECONDS of work.

    Each run is (started, finished, episodes saved) with times in seconds.
    """
    episodes = seconds = 0
    later = None
    for started, finished, saved in runs:
        if finished < started or (later is not None and later < finished):
            continue
        seconds += finished - started
        if later is not None and later - finished <= JOINED_SECONDS:
            seconds += later - finished
        episodes += saved
        later = started
        if seconds >= PACE_SECONDS:
            break
    return {'episodes': episodes, 'seconds': int(seconds)}


def remaining(counts, series_count, unchecked_series, recent, saved_bytes=0):
    """What is left: the bytes it should add, and the seconds it should take at the recent pace (None while that cannot be told)."""
    number = lambda value: value if type(value) is int and value >= 0 else 0
    waiting = number(counts.get('queued')) + number(counts.get('failed'))
    unchecked = min(number(unchecked_series), number(series_count))
    # A series not looked at yet is taken to hold what a series usually holds.
    expected = waiting + unchecked * EPISODES_PER_SERIES
    result = {'waiting_episodes': waiting, 'unchecked_series': unchecked, 'seconds': None,
              'saved_bytes': number(saved_bytes), 'left_bytes': expected * EPISODE_BYTES}
    if (isinstance(recent, dict) and number(recent.get('seconds')) >= ENOUGH_SECONDS
            and number(recent.get('episodes')) >= ENOUGH_EPISODES):
        result['seconds'] = round(expected * recent['seconds'] / recent['episodes'])
    return result
