"""Caption spelling, exact four-star masks and the evidence saved with a render."""
import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path


VERSION = 2
MASK = "****"
DICTIONARY = json.loads(Path(__file__).with_name("caption_dictionary.json").read_text(encoding="utf-8"))
_TOKEN = re.compile(r"^([^\w']*)([\w'-]+?)(['’]s)?([^\w']*)$", re.I)


@lru_cache(maxsize=64)
def _aliases(groups):
    result = {}
    for table in [DICTIONARY["all"], *(DICTIONARY["contexts"][i]["terms"] for i in groups)]:
        for canonical, spellings in table.items():
            for spelling in [canonical, *spellings]:
                result[tuple(spelling.casefold().split())] = canonical
    return result, max(map(len, result))


def caption_words(words, context=""):
    context = context.casefold()
    groups = tuple(i for i, group in enumerate(DICTIONARY["contexts"])
                   if any(re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", context) for key in group["when"]))
    aliases, longest = _aliases(groups)
    parts = [_TOKEN.fullmatch(w["w"]) for w in words]
    result = []
    i = 0
    while i < len(words):
        for n in range(min(longest, len(words) - i), 0, -1):
            chunk = parts[i:i+n]
            if not all(chunk):
                continue
            if any(m[3] or m[4] or words[i+j+1]["s"] - words[i+j]["e"] > .55 for j, m in enumerate(chunk[:-1])):
                continue
            canonical = aliases.get(tuple(m[2].casefold() for m in chunk))
            if canonical:
                result.append(dict(words[i], w=chunk[0][1] + canonical + (chunk[-1][3] or "") + chunk[-1][4],
                                   e=words[i+n-1]["e"]))
                i += n
                break
        else:
            result.append(dict(words[i]))
            i += 1
    return result


_LEET = str.maketrans({"0":"o","1":"i","3":"e","4":"a","5":"s","7":"t","@":"a","$":"s","!":"i",
                       "а":"a","е":"e","і":"i","о":"o","с":"c","ѕ":"s","х":"x","р":"p","к":"k"})
_LEET_DIGITS = {key:value for key,value in _LEET.items() if chr(key) not in "!$@"}
_SEP = r"[\W_]*"


def _spelled(word):
    return _SEP.join(re.escape(c) + "+" for c in word)


def _pattern(roots, suffix):
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(_spelled(w) for w in sorted(roots.split(), key=len, reverse=True))
                      + ")(?:" + suffix + r")?(?![a-z0-9])", re.I)


_SWEAR = _pattern(
    "fuck fucker fucking fuckin motherfuck motherfucker motherfucking bullshit horseshit shit shitty shite "
    "shithead shitshow shitstorm shitting shitter bullshitting bullshitter dipshit batshit fuckup fuckboy "
    "dumbass badass jackass asshat asshole arsehole ass arse bitch bastard piss cunt "
    "dick dickhead cock cocksucker pussy twat wanker wank crap damn goddamn goddammit dammit hell whore slut "
    "bollocks prick douche douchebag wtf stfu gtfo lmfao ffs puta puto mierda cabron carajo chutiya bhenchod madarchod",
    r"s|es|ed|er|ers|in|ing|ings|y|ty|ier|iest|head|heads|hole|holes|bag|bags|face|wit|wits|less|inghell")
_SLUR = _pattern(
    "nigger nigga nigguh wigger coon darkie darky chink gook spic spick wetback beaner kike kyke "
    "raghead towelhead sandnigger paki jap zipperhead redskin halfbreed chinaman porchmonkey "
    "faggot fag dyke tranny retard retarded mongoloid gyppo kaffir kuffar",
    r"s|es|a|as|er|ers|z")
_CODED = re.compile(r"\b(?:white\s+(?:power|supremacy\s+forever)|heil\s+hitler|sieg\s+heil|"
                    r"(?:kill|exterminate|deport)\s+all\s+(?:the\s+)?(?:blacks?|jews?|muslims?|asians?|mexicans?))\b", re.I)
_UNKNOWN = re.compile(r"\[\s*(?:_+|\*+|bleep|censored|inaudible|unintelligible)\s*\]|"
                      r"\b[a-z]+\*+[a-z]+\b|\*{2,}", re.I)


def _normalized(text):
    chars, offsets = [], []
    for i, char in enumerate(text):
        if unicodedata.category(char) == "Cf":
            continue
        for c in unicodedata.normalize("NFKD", char.casefold()):
            if not unicodedata.combining(c):
                # A final ! or $ is often punctuation; the plain pass below catches that case.
                chars.append(c)
                offsets.append(i)
    return "".join(chars), offsets


def hits(text):
    normal, offsets = _normalized(text)
    found = set()
    # Punctuation can also look like a letter. Keep the punctuation interpretation
    # alongside the full leetspeak pass, e.g. a digit in a word ending with '!'.
    for value in {normal, normal.translate(_LEET), normal.translate(_LEET_DIGITS)}:
        for kind, pattern in (("slur", _SLUR), ("slur", _CODED), ("swear", _SWEAR)):
            for m in pattern.finditer(value):
                if kind == "swear" and m.group().replace("’", "'") == "he'll":
                    continue
                found.add((offsets[m.start()], offsets[m.end()-1]+1, kind))
    return sorted(found)


def mask(text):
    text = str(text or "")
    ranges = []
    for a, b, _ in hits(text):
        if ranges and a < ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a,b])
    for a,b in reversed(ranges):
        text = text[:a] + MASK + text[b:]
    return _UNKNOWN.sub(MASK, text)


def inspect_words(words, start, end):
    inside = [w for w in words if w["e"] > start and w["s"] < end]
    text = " ".join(w["w"] for w in inside)
    matches = hits(text)
    masked = []
    masked_count = 0
    offset = 0
    for w in inside:
        stop = offset + len(w["w"])
        sensitive = any(a < stop and b > offset for a,b,_ in matches) or _UNKNOWN.search(w["w"])
        masked.append(dict(w, w=MASK) if sensitive else dict(w))
        if sensitive:
            masked_count += 1
        offset = stop + 1
    return masked, masked_count, any(kind == "slur" for _,_,kind in matches), bool(_UNKNOWN.search(text))


def file_hash(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def tag_terms(tag):
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag).replace("_", " ")
