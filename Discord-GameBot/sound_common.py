"""Audio limits and errors without importing the Discord or voice stack."""
MAX_BYTES = 25 * 1024 * 1024


class SoundError(Exception):
    pass
