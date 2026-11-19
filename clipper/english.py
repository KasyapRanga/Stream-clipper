"""Keep everything the viewer reads in English.

- Captions are shown only for clips whose speech is really English (Whisper says English AND enough of
  the words are common English words). Telugu, Hindi or mixed speech gets no captions rather than
  garbled ones.
- Titles and post text must be English: words in other scripts and romanized Telugu/Hindi filler are
  removed; if too little English is left, a clean English hook is used instead.
"""
from __future__ import annotations

import random
import re

COMMON = set("""
a about above after again against all almost alone already also always am an and another any anyone
anything are around as ask at away back bad be because been before behind being best better between big
bit both bro brother but by call came can cannot cant car come could crazy damn day did didnt do does
doesnt doing done dont down dude each easy else end enough even ever every everyone everything eye face
fact fall far fast feel few find fine first for found free friend from full fun funny game games gave
get gets getting girl give go god goes going gone good got great guy guys had happen happened hard has
have he head hear hell hello help her here hey hi him his hit hold home hope how i if im in inside into
is it its ive just keep kill killed kind knew know last late least leave left let lets life like literally
little live long look looking lost lot love made make man many may maybe me mean men might mind mine
miss money more most move much must my myself name need never new next nice night no nobody not nothing
now of off oh ok okay old on once one only open or other our out over own people play played player
playing please point pretty put quick quite ready real really right round run said same saw say says
second see seen shot should show shut side since so some someone something sorry start still stop such
sure take talk team tell than thank thanks that thats the their them then there these they thing things
think this those though thought three through time to today together too took top try trying turn two
under until up us use very wait want wanted was watch way we well went were what whats when where which
while who why will win with without wont work world would wow yeah yes yet you your youre
bruh lol lmao gg ez clutch ace noob pro pog insane epic fail wins won lose loss kills dead die died
headshot snipe sniper gun shoot boss level stream chat clip guys broke rage scared jump scare play
match ball bowl bowled bat batting six four out wicket catch goal score scored over overs run runs
debate squad pick ultimate controls forgot special needed mic ready setup struggles wild crazy unreal
""".split())

# Romanized filler from Telugu / Hindi / Tamil speech that Whisper or an agent may leave in a title.
NOT_ENGLISH = set("""
ra ri enti endi emi em ela idi adi nenu nuvvu meeru vadu vaadu avunu ledu kadu cheppu chesa chesav chesanu
babu bava mama anna akka arey orey oye ayyo baboi abba chala bagundi bagaledu kani inka ippudu ikkada
akkada ante antey entra emaindi aithe ayithe kya hai nahi nahin haan bhai yaar acha accha kaise kaisa kyu
kyun mera tera wala wali bas chalo arre arrey matlab samjha dekh dekho kuch bahut abhi
""".split())

_SCRIPT = re.compile(r"[^\x00-\x7F‘-‟…]")  # anything outside basic Latin punctuation
_WORD = re.compile(r"[A-Za-z']+")


def is_latin(text: str) -> bool:
    return not _SCRIPT.search(text)


def english_ratio(text: str) -> float:
    toks = [t.lower().strip("'") for t in _WORD.findall(text)]
    toks = [t for t in toks if t]
    if not toks:
        return 0.0
    return sum(t in COMMON for t in toks) / len(toks)


def caption_words(words: list[dict], cfg: dict | None = None) -> list[dict]:
    """The words to show as captions: only when the clip's speech is clearly English, else []."""
    cc = ((cfg or {}).get("render") or {}).get("captions") or {}
    if cc.get("english_only", True) is False:
        return words
    en = [w for w in words if w.get("en", True) and is_latin(w["w"])]
    if len(en) < 3 or len(en) < 0.8 * len(words):
        return []
    text = " ".join(w["w"] for w in en)
    if english_ratio(text) < float(cc.get("min_english_ratio", 0.45)):
        return []   # "English" by script only (e.g. Telugu heard as English): skip rather than show gibberish
    return en


def english_transcript(words: list[dict]) -> list[dict]:
    """Words fit to show an agent / use for a title (same test as captions)."""
    return caption_words(words, None)


FALLBACK = {
    "funny": ["THIS WAS TOO FUNNY", "I CAN'T STOP LAUGHING", "WHAT JUST HAPPENED", "THIS FAIL THOUGH"],
    "hype": ["WHAT A PLAY", "THIS WAS INSANE", "CLUTCH MOMENT", "NO WAY THAT WORKED"],
    "tense": ["THAT WAS TOO CLOSE", "HEART RATE: MAX", "I WAS NOT READY"],
    "chill": ["GOOD VIBES ONLY", "JUST CHILLING", "LATE NIGHT STREAM MOOD"],
    "none": ["STREAM HIGHLIGHT", "YOU HAVE TO SEE THIS", "WAIT FOR IT"],
}


def clean_title(title: str, mood: str = "none", seed: int = 0) -> str:
    """An English, upper-case hook title. Removes other scripts and romanized Telugu/Hindi words;
    falls back to an English hook that fits the clip's mood when too little English is left."""
    title = _SCRIPT.sub(" ", title or "").replace("#", " ")
    toks = [t for t in title.split() if t.lower().strip("!?.,'\"") not in NOT_ENGLISH]
    text = " ".join(toks).strip(" -:,.")
    words = _WORD.findall(text)
    ok = len(words) >= 2 and any(w.lower() in COMMON for w in words)
    if not ok:
        text = random.Random(seed).choice(FALLBACK.get(mood, FALLBACK["none"]))
    words = text.split()
    if len(words) > 8:
        text = " ".join(words[:8])
    return text.upper()


def clean_text(text: str) -> str:
    """Strip characters of other scripts from post text (description/caption lines)."""
    return re.sub(r"[ \t]+", " ", _SCRIPT.sub("", text or "")).strip()
