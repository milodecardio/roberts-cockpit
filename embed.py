"""Vektoren über das lokale Ollama (bge-m3) erzeugen. Nur Python-Bordmittel."""
import array
import json
import math
import os
import urllib.request

OLLAMA_URL = os.environ.get("COCKPIT_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("COCKPIT_EMBED_MODEL", "bge-m3")


class Unavailable(Exception):
    pass


def embed(texts, timeout=600):
    """Liste von Texten → Liste von Vektoren (Listen von floats, normiert)."""
    if not texts:
        return []
    req = urllib.request.Request(
        OLLAMA_URL + "/api/embed",
        data=json.dumps({"model": MODEL, "input": texts, "truncate": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            vecs = json.load(r)["embeddings"]
    except OSError as e:
        raise Unavailable("Ollama nicht erreichbar: %s" % e)
    return [normalize(v) for v in vecs]


def normalize(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def to_blob(v):
    return array.array("f", v).tobytes()


def from_blob(b):
    a = array.array("f")
    a.frombytes(b)
    return a
