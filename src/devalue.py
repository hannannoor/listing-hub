"""
Decoder for SvelteKit's "devalue" flat-JSON format.

SvelteKit serves every page's data at <route>/__data.json. To avoid repeating
values it flattens everything into one array where each entry references others
by index. This module turns that back into normal Python objects.

Reference: https://github.com/Rich-Harris/devalue
"""

UNDEFINED = -1
HOLE = -2
NAN = -3
POS_INF = -4
NEG_INF = -5
NEG_ZERO = -6

_SPECIALS = {
    UNDEFINED: None,
    HOLE: None,
    NAN: float("nan"),
    POS_INF: float("inf"),
    NEG_INF: float("-inf"),
    NEG_ZERO: -0.0,
}

# devalue encodes exotic types as ["TypeName", payload...]
_WRAPPERS = {"Date", "Set", "Map", "RegExp", "Object", "BigInt", "null", "URL"}


def unflatten(flat):
    """Rebuild the object graph from devalue's flat array."""
    if not isinstance(flat, list):
        return flat
    if len(flat) == 0:
        return None

    hydrated = {}

    def resolve(index):
        if index in _SPECIALS:
            return _SPECIALS[index]
        if index in hydrated:
            return hydrated[index]
        if index < 0 or index >= len(flat):
            return None

        value = flat[index]

        if isinstance(value, list):
            # Exotic type wrapper, e.g. ["Date", "2024-01-01T00:00:00Z"]
            if value and isinstance(value[0], str) and value[0] in _WRAPPERS:
                kind = value[0]
                if kind == "Date":
                    hydrated[index] = value[1]
                elif kind == "Set":
                    out = []
                    hydrated[index] = out
                    out.extend(resolve(i) for i in value[1:])
                elif kind == "Map":
                    out = {}
                    hydrated[index] = out
                    rest = value[1:]
                    for k, v in zip(rest[0::2], rest[1::2]):
                        out[str(resolve(k))] = resolve(v)
                elif kind == "BigInt":
                    hydrated[index] = int(value[1])
                elif kind == "null":
                    hydrated[index] = None
                elif kind == "Object":
                    hydrated[index] = resolve(value[1])
                else:  # RegExp, URL and anything else -> keep the raw payload
                    hydrated[index] = value[1] if len(value) == 2 else value[1:]
                return hydrated[index]

            out = []
            hydrated[index] = out          # set before recursing so cycles work
            out.extend(resolve(i) for i in value)
            return out

        if isinstance(value, dict):
            out = {}
            hydrated[index] = out          # set before recursing so cycles work
            for key, ref in value.items():
                out[key] = resolve(ref)
            return out

        hydrated[index] = value            # plain string / number / bool / None
        return value

    return resolve(0)


def parse_sveltekit_data(payload):
    """
    Take a parsed __data.json response and return the merged page data.

    The response looks like {"type":"data","nodes":[{...},{"type":"skip"},...]}.
    Layout nodes are usually skipped; the leaf node holds the page's own data.
    """
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object from __data.json")

    merged = {}
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "data":
            continue
        data = unflatten(node.get("data"))
        if isinstance(data, dict):
            merged.update(data)

    if not merged:
        raise ValueError("No usable data nodes in __data.json response")
    return merged
