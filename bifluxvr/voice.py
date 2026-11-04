_serious = False


def set_serious(on):
    global _serious
    _serious = bool(on)


def is_serious():
    return _serious


def t(serious, funny):
    return serious if _serious else funny
