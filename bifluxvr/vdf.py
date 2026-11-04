class VDFError(ValueError):
    pass


def _tokens(text):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\':
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            if i >= n:
                raise VDFError("unterminated string")
            i += 1
            yield ("s", "".join(buf))
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}"':
                j += 1
            yield ("s", text[i:j])
            i = j


def loads(text):
    toks = _tokens(text)

    def block(top):
        items = []
        for t in toks:
            if t == "}":
                if top:
                    raise VDFError("unbalanced '}'")
                return items
            if t == "{":
                raise VDFError("unexpected '{'")
            value = next(toks, None)
            if value is None:
                raise VDFError(f"missing value for key {t[1]!r}")
            if value == "{":
                items.append([t[1], block(False)])
            elif value == "}":
                raise VDFError(f"missing value for key {t[1]!r}")
            else:
                items.append([t[1], value[1]])
        if not top:
            raise VDFError("unexpected end of file")
        return items

    return block(True)


def _esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def dumps(items, indent=0):
    tab = "\t" * indent
    out = []
    for key, value in items:
        if isinstance(value, list):
            out.append(f'{tab}"{_esc(key)}"\n{tab}{{\n')
            out.append(dumps(value, indent + 1))
            out.append(f"{tab}}}\n")
        else:
            out.append(f'{tab}"{_esc(key)}"\t\t"{_esc(value)}"\n')
    return "".join(out)


def get(items, key, default=None):
    if not isinstance(items, list):
        return default
    low = key.lower()
    for k, v in items:
        if k.lower() == low:
            return v
    return default


def path(items, *keys):
    for key in keys:
        items = get(items, key)
        if items is None:
            return None
    return items


def ensure(items, key):
    low = key.lower()
    for pair in items:
        if pair[0].lower() == low and isinstance(pair[1], list):
            return pair[1]
    child = []
    items.append([key, child])
    return child


def put(items, key, value):
    low = key.lower()
    for pair in items:
        if pair[0].lower() == low and not isinstance(pair[1], list):
            pair[1] = value
            return
    items.append([key, value])


def remove(items, key):
    low = key.lower()
    items[:] = [p for p in items if p[0].lower() != low]
