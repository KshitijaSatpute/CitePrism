"""Checks every model-written answer must pass before a reader sees it.

The assistant may use a language model to phrase answers the built-in topics do
not cover, and to answer in German. A model can still invent a count, name a
reference that does not exist, or use the language of accusation that the
audit policy forbids — on its own, or because text in a manuscript told it to.
These checks are deliberately strict: an answer that fails any of them is
discarded and the built-in answer is used instead.

* **Numbers.** Every number in the answer — digits, ordinals such as "47th",
  and spelled-out numbers in English or German — must occur in the audit facts
  given to the model. Numbers the reader typed do not count.
* **References.** Every bracketed marker ("[12]") and every "reference 12" must
  be a reference of this audit.
* **No accusations.** Words of misconduct, fabrication or deception, in English
  and German, are refused — after undoing the usual disguises: zero-width and
  soft-hyphen characters, look-alike letters from other scripts, and hyphens or
  spaces inside a word.
"""

from __future__ import annotations

import re
import unicodedata

# Letters from other scripts that look like Latin ones ("frаud" with a Cyrillic а).
CONFUSABLE_LETTERS = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ɡ": "g", "ӏ": "l", "ο": "o", "α": "a", "ε": "e", "ρ": "p", "τ": "t", "υ": "u", "ν": "v",
    "κ": "k", "ι": "i", "Α": "a", "Ε": "e", "Ο": "o", "Р": "p", "С": "c", "Т": "t", "Н": "h", "К": "k",
    "М": "m", "В": "b", "Х": "x", "А": "a", "Е": "e", "О": "o", "І": "i",
    "ƒ": "f", "Ь": "b", "ь": "b", "ɑ": "a", "ı": "i", "ł": "l", "ø": "o", "ɛ": "e", "ʀ": "r", "ꜱ": "s",
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k",
    "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y",
    "ᴢ": "z", "ԛ": "q", "ɩ": "i",
    "т": "t", "м": "m", "н": "h", "к": "k", "в": "b", "г": "r", "п": "n", "ш": "w", "һ": "h", "ԝ": "w", "ү": "y",
    "Ѕ": "s", "Ј": "j", "Ү": "y", "Β": "b", "Η": "h", "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n", "Τ": "t", "Χ": "x",
    "Υ": "y", "Ζ": "z", "β": "b", "η": "n", "μ": "u", "χ": "x", "ω": "w",
}
CONFUSABLES = str.maketrans(CONFUSABLE_LETTERS)
# Keeping case: a capital look-alike stands for a capital ("Веtrug" is the noun "Betrug").
CASED_CONFUSABLES = str.maketrans({key: value.upper() if key.isupper() else value
                                   for key, value in CONFUSABLE_LETTERS.items()})

ACCUSATION = re.compile(
    r"misconduct|fraud|plagiar|plagiier|dishonest|manipul(?!ation\s+checks?\b)|fabricat|falsif|deceiv|decept|"
    r"unethical|misrepresent|doctored|counterfeit|"
    r"\bmade\s?up\b(?!\s+(?:of|for|about|around|nearly|almost|roughly|only|just|over|under|more|less|half|most|much|"
    r"the\s+majority|a\s+(?:third|quarter|fifth|tenth|half|large|small|significant|substantial)|\d))|"
    r"\bcooked\s+up\b|\binvent(?:ed|ing)\b|\bcheat|\bbogus\b|\bshams?\b|\bhoax|\bscams?\b|\blying\b|\bliars?\b|"
    r"\bfak(?:e|es|ed|ing|ery)\b|\bforg(?:ed|ery|eries)\b|\bstole(?:n)?\b|\bsteal(?:s|ing)?\b|\btheft\b|"
    r"\bfull\s+of\s+lies\b|\btold\s+(?:a\s+|many\s+|some\s+)?lies\b|\btelling\s+lies\b|"
    r"\b(?:authors?|they|he|she|writers?|researchers?)\s+(?:lie|lies)\b(?!\s+(?:in|within|between|outside|on|at|"
    r"behind|ahead|beyond|among|across|under|above|below|near|close|far)\b)|"
    r"\b(?:are|were|all|just|nothing\s+but|pure|outright)\s+lies\b(?!\s+(?:in|within|between|outside|at|on)\b)|"
    r"(?:^|[.!?]\s*)lies\s*[!.]|"
    r"\b(?:authors?|they|he|she|researchers?|writers?)\s+(?:mislead|misleads|are\s+misleading|were\s+misleading)\b|"
    r"\b(?:deliberately|intentionally|knowingly|purposely|wilfully|willfully)\s+mislead|"
    r"\bmislead\w*\b[^.!?]{0,40}\bon\s+purpose\b|"
    r"\bthie(?:f|ves|very|ving)\b|\bfudg(?:ed|ing)\b|\bfudge\s+(?:the|their)\b|\bout\s+of\s+thin\s+air\b|"
    r"\bcrooks?\b|\bphon(?:e)?y\b|\bfictitious\b(?!\s+play\b)|\bfib(?:bed|bing)\b|"
    r"\bfalsely\s+(?:claim|state|report|present|attribut|cit|assert)\w*|"
    r"\b(?:data|results?|numbers|figures|books|findings)\s+(?:were|was|are|is|been|got)\s+cooked\b|"
    r"\bcook(?:ed|ing)\s+(?:the\s+|their\s+)?(?:data|results|numbers|figures|books)\b|"
    r"\bcop(?:ied|ying)\b[^.!?]{0,80}\bwithout\s+(?:any\s+)?(?:credit|attribution|acknowledg\w*|citing|saying|"
    r"mention\w*|quot\w*|reference|permission)\b|"
    r"betrüg|betrog|plagiat|gefälscht|fälsch(?!lich)|fehlverhalten|(?<!ent)(?<!sinnes)(?<!optische )(?<!optischen )täusch(?!ungen der wahrnehmung)|"
    r"erfunden|erlogen|gelogen|\blüge|lügner|schwindel|unredlich|abgeschrieben|geschummelt|schummel|ausgedacht|"
    r"gestohlen|geklaut|getürkt|gemogelt|\bmogel|geflunkert|\bflunker|"
    r"frisiert\w*\s+(?:daten|zahlen|ergebnisse|statistik\w*)|\b(?:daten|zahlen|ergebnisse|statistik\w*)\b[^.!?]{0,30}"
    r"\bfrisiert"
)
# Read with the letters' case: "a Lie group" is Sophus Lie; "lies in the year" is not a lie;
# "misled by the missing year" blames no one.
LIE = re.compile(r"\ba\s+lie\b(?!\s+(?:group|algebra|bracket|derivative|superalgebra|theory|type)s?\b)|"
                 r"\b(?:outright|blatant|pure|complete|total|just)\s+lies?\b(?!\s+(?:in|within|between|outside|at|on|"
                 r"with|behind|ahead|beyond|among|across|under|above|below|near|there|here|elsewhere)\b)", re.IGNORECASE)
MISLED = re.compile(r"\b(?:authors?|they|he|she|researchers?|writers?)\b[^.!?]{0,40}\bmisled\b|"
                    r"\bmisled\b[^.!?]{0,30}\bby\s+(?:the\s+|these\s+|its\s+|their\s+)?(?:authors?|them|him|her|"
                    r"researchers?|writers?)\b|\bmisled\s+(?:the\s+|its\s+|their\s+)?(?:readers?|reviewers?|editors?|"
                    r"public|us|you)\b", re.IGNORECASE)
# "Betrug" (fraud), also inside compounds ("Wissenschaftsbetrug"); but "betrug"/"betrugen" are
# also the past tense of "betragen" (amounted to): "Der Anteil betrug 5 %", "Damit betrug der
# Anteil …". German capitalises nouns, so the lower-case word is the verb — unless an article
# or "begangen" shows it is the noun written small.
BETRUG = re.compile(r"betrug\w*", re.IGNORECASE)
BETRAGEN = re.compile(r"betrug(?:en)?\s+(?:(?:rund|etwa|ca\.?|circa|knapp|gut|nur|insgesamt|jeweils|zusammen|"
                      r"damit|dabei|somit|noch|bereits|mehr|weniger|als|über|unter|fast|genau)\s+){0,2}"
                      r"(?:\d|(?:ein|eine|einen|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf|"
                      r"zwanzig|dreißig|vierzig|fünfzig|hundert|tausend|die\s+hälfte|ein\s+drittel|ein\s+viertel)\b)",
                      re.IGNORECASE)
NOUN_BEFORE = re.compile(r"(?:\b(?:der|den|dem|des|ein|einen|einem|eines|kein|keinen|keinem|keines|vom|zum|beim|"
                         r"wegen|als|um|ist|sei|war|wäre|nach|für|durch|gegen|mit|von|zu|im|auf|ihr|ihren|ihrem|"
                         r"sein|seinen|seinem|dieser|diesen|diesem|dieses|solcher|solchen|reiner|reinen|glatter|"
                         r"glatten|schwerer|schweren|haben|hat|hatten|hätten|wurde|wird|nennen|nennt|ist\s+das|"
                         r"das\s+ist)\s+)$", re.IGNORECASE)
NOUN_AFTER = re.compile(r"\s+(?:begangen|begehen|begeht|beging|vorliegt|vorliegen|vor\b|handelt|gehandelt|"
                        r"nachgewiesen|vorgeworfen|im\s+spiel)", re.IGNORECASE)
# "lied" is the English past tense; "Lied" is a German song (or a surname: "K. Lied").
LIED = re.compile(r"\blied\b", re.IGNORECASE)
GERMAN = re.compile(r"\b(?:der|die|das|und|ist|nicht|eine?|mit|von|zu|auf|für|sich|wird|wurde|sind|dem|den|des|"
                    r"im|wie|auch|wird|einem|einer)\b", re.IGNORECASE)
# A surname written with an initial ("Lied, K.", "Steal, J.", "M. Fraude") is a name.
NAME_WITH_INITIAL = re.compile(r"\b[A-ZÄÖÜ][a-zäöüß]+,\s*[A-Z]\.|\b[A-Z]\.\s*[A-ZÄÖÜ][a-zäöüß]+\b(?!\s+(?:[a-z]))")
# "made the data up", "made [3] up" — but not "made a follow up", "made up of", "made it up to date".
MADE_UP = re.compile(r"\bmade\s+((?:(?:\[\d+\]|\d+|[^\W\d_]+)\s+){1,4}?)up\b(?!\s+(?:of|to|for|with|on)\b)",
                     re.IGNORECASE)
MADE_UP_OBJECTS = {"it", "this", "that", "them", "these", "those", "everything", "things", "stuff", "the", "their",
                   "his", "her", "its", "some", "all", "data", "results", "numbers", "references", "sources",
                   "quotes", "citations", "figures", "findings", "reference", "source"}
PHRASAL = {"follow", "back", "set", "sign", "look", "clean", "catch", "pick", "mix", "wrap", "break", "start",
           "make", "build", "line", "pop", "check", "round", "write", "warm", "speed", "top", "mock", "hook"}
# Characters that reorder text on screen: never in an honest answer. The right-to-left and
# left-to-right marks are allowed only where Hebrew or Arabic script needs them.
BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
DIRECTION_MARKS = re.compile(r"[\u200e\u200f\u061c]")
RIGHT_TO_LEFT = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefe]")
# Stems long enough that adjacent words joining into one ("fab ricated") is no accident.
# Stems that are whole words may end where the joined words end; the others need an ending
# ("fabric at" is not "fabricat-ed").
LONG_STEMS = re.compile(r"misconduct|fabricat|plagiar|dishonest|manipul|falsif|fehlverhalten|gefälscht|unethical|"
                        r"betrug|betrogen|erfunden|gelogen|erlogen")
WHOLE_STEMS = {"misconduct", "dishonest", "fehlverhalten", "gefälscht", "unethical", "betrug", "betrogen", "erfunden",
               "gelogen", "erlogen"}
ENDINGS = {"e", "ed", "es", "er", "ers", "ing", "ion", "ions", "ism", "isms", "ist", "ists", "ize", "ized", "ise",
           "ised", "ate", "ated", "ates", "ating", "ation", "ations", "ative", "y", "ied", "ies", "ier", "iers",
           "ication", "ications", "ung", "ungen", "en", "t", "te", "ten", "ly", "s", "ity", "or", "ors"}
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
LEET_L = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
CHARGE = re.compile(r"(?i)\b(?:commit\w*|guilty\s+of|engag\w*\s+in|accus\w*\s+of|resort\w*\s+to|is|are|was|were|"
                    r"be|been|(?:case|example|instance|piece|act|form)\s+(?:of|for)|case\s+study\s+(?:in|of|for)|"
                    r"known\s+for)\s+(?:(?:a|an|the|pure|plain|outright|textbook)\s+)?$")
# Whom a sentence is about: a subject named in a sentence about the authors or the manuscript
# ("Fraud detection tools would flag these authors") is not a subject but a charge.
PEOPLE = re.compile(r"(?i)\b(?:authors?|they|them|he|she|him|his|researchers?|writers?|"
                    r"(?:this|the)\s+(?:paper|manuscript|chapter|text|thesis|article|data|figures?|results))\b")
# A subject named, not a charge made: "covers fraud detection", "compares fake news classifiers",
# "market manipulation", "the term 'fake news'".
TOPICAL = re.compile(r"\b(?:(?:fraud|deception|plagiarism|manipulation|cheating|fake\s+news|misinformation|"
                     r"disinformation)\s+(?:detection|prevention|research|literature|studies|study|checkers?|software|"
                     r"tools?|checks?|classifiers?|models?|datasets?|corpus|corpora|campaigns?|policy|policies)|"
                     r"(?:market|price|stock|currency|vote|voter|electoral|election|image|photo|genetic|experimental|"
                     r"social|media|opinion|search\s+engine)\s+manipulation|accounting\s+fraud|tax\s+fraud|"
                     r"(?:academic|research)\s+(?:integrity|misconduct)\s+(?:policy|policies|research|literature|"
                     r"education|training))\b", re.IGNORECASE)
# Bare subject words, named only after a cue ("studies the theft of …", "the term 'fake news'").
TOPIC_WORDS = re.compile(r"\b(?:fake\s+news|misinformation|disinformation|theft|plagiarism|fraud)\b", re.IGNORECASE)
TOPIC_CUE = re.compile(r"(?i)\b(?:on|about|of|covers?|covering|stud(?:y|ies|ied|ying)|reviews?|reviewed|"
                       r"discuss(?:es|ed)?|address(?:es|ed)?|examin(?:es|ed)|investigat(?:es|ed)|analy[sz](?:es|ed)|"
                       r"titled|called|named|surveys?|term|terms|concept|phrase|topic|field|area|compar(?:es|ed)|"
                       r"evaluat(?:es|ed)|describ(?:es|ed)|explor(?:es|ed)|measur(?:es|ed)|detect(?:s|ed|ing)|"
                       r"literature|research)\s+(?:the\s+|a\s+|an\s+)?[\"'“‘„«»]?\s*(?:[\w-]+\s+){0,2}$")
# Names of technical subjects that are never charges.
TECHNICAL = re.compile(r"(?i)\b(?:work|cycle|task|page|job|time)[- ]?stealing\b|\bstolen\s+generations?\b|"
                       r"\bcounterfeit(?:ing)?\s+(?:drugs?|medicines?|goods|products|banknotes?|currency|coins?|"
                       r"detection|parts)\b|\b(?:identity|electricity|energy|power|wage|cattle|art|car|bike|bicycle|"
                       r"cargo|fuel|water)\s+theft\b|\btheft\s+(?:detection|prevention)\b|\bfictitious\s+play\b|"
                       r"\bfudge\s+factors?\b")

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
         "null": 0, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8,
         "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15,
         "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90, "zwanzig": 20, "dreißig": 30, "vierzig": 40, "fünfzig": 50, "sechzig": 60, "siebzig": 70,
        "achtzig": 80, "neunzig": 90}
SCALES = {"hundred": 100, "thousand": 1000, "hundert": 100, "tausend": 1000, "dozen": 12, "dutzend": 12}
# "one"/"eins" double as pronouns and articles ("the first one", "eine Quelle"); "elf" is a
# German number but also an English word. They are left out of the spelled-number check.
AMBIGUOUS_WORDS = {"one", "eins", "elf", "null", "acht"}

DIGITS = re.compile(r"\d+(?:[.,]\d+)?")
MARKER = re.compile(r"\[(\d{1,4})\]")
REFERENCE_WORD = re.compile(r"\b(?:reference|ref\.?|referenz|quelle|eintrag|entry)\s*#?\s*(\d{1,4})\b", re.IGNORECASE)


def screening_form(text: str, *, fold: bool = True) -> str:
    """The text with disguises undone, for screening only (never shown)."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = "".join(character for character in value if unicodedata.category(character) != "Cf")  # zero-width, soft hyphen
    value = value.translate(CONFUSABLES if fold else CASED_CONFUSABLES)
    if fold:
        value = value.casefold()
    # Accents added to hide a word ("fabricáted", "fakë"); German umlauts (ä, ö, ü) are kept,
    # they are letters of their own.
    kept, previous = [], ""
    for character in unicodedata.normalize("NFKD", value):
        if unicodedata.combining(character):
            if character == "̈" and previous in "aouAOU":
                kept.append(character)
            continue
        kept.append(character)
        previous = character
    value = unicodedata.normalize("NFC", "".join(kept))
    value = re.sub(r"(?<=[^\W\d_])[\-‐‑‒–—_.·•*]+(?=[^\W\d_])", "", value)   # "pla-giarism", "f.r.a.u.d"
    return value


def _betrug(cased: str) -> list[str]:
    """"Betrug" and its compounds; not the verb "betrug"/"betrugen" ("Der Anteil betrug 5 %")."""
    found = []
    for match in BETRUG.finditer(cased):
        word = match.group(0)
        compound = match.start() > 0 and re.match(r"\w", cased[match.start() - 1])
        before = cased[max(0, match.start() - 30):match.start()]
        noun = NOUN_BEFORE.search(before) or NOUN_AFTER.match(cased, match.end())
        verb_form = word.casefold() in ("betrug", "betrugen")
        # A German sentence never starts with a small letter: "betrug der Autoren" there is the noun.
        opening = not cased[:match.start()].strip() or re.search(r"[.!?:]\s*$", cased[:match.start()])
        if not compound and verb_form and not noun and (
                (word.islower() and not opening) or (BETRAGEN.match(cased, match.start()) and not word.isupper())):
            continue
        found.append(word.casefold())
    return found


def _without_topics(value: str) -> str:
    """`value` without the names of subjects ("covers fraud detection", "work stealing") — unless
    used as a charge ("committed fraud …", "is fake news") or in a sentence about the authors or
    the manuscript ("Fraud detection tools flag these authors")."""
    def sentence(position: int) -> str:
        start = max(value.rfind(mark, 0, position) for mark in ".!?;\n") + 1
        ends = [index for index in (value.find(mark, position) for mark in ".!?;\n") if index >= 0]
        return value[start:min(ends) if ends else len(value)]

    def blank_technical(match):
        before = value[max(0, match.start() - 30):match.start()]
        return match.group(0) if CHARGE.search(before) else " "

    def blank_topic(match):
        before = value[max(0, match.start() - 60):match.start()]
        if CHARGE.search(before) or PEOPLE.search(sentence(match.start())):
            return match.group(0)
        return " "

    def blank_word(match):
        before = value[max(0, match.start() - 60):match.start()]
        if TOPIC_CUE.search(before) and not CHARGE.search(before) and not PEOPLE.search(sentence(match.start())):
            return " "
        return match.group(0)
    value = TECHNICAL.sub(blank_technical, value)
    value = TOPICAL.sub(blank_topic, value)
    return TOPIC_WORDS.sub(blank_word, value)


def _joined(cased: str) -> list[str]:
    """A word split into two to four pieces ("fab ricated", "fa bri cated", "pla giar ism").

    Pieces must be separated by spaces only ("Ravi Mani. Pulmonary" is two sentences), and
    capitalised names side by side ("Mani & Pulido", "Themis Conduct") are names.
    """
    words = [(match.group(0), match.start(), match.end()) for match in re.finditer(r"[^\W\d_]+", cased)]
    found = []
    for index in range(len(words)):
        for size in (2, 3, 4):
            window = words[index:index + size]
            if len(window) < size:
                break
            if re.search(r"[^\s\-‐‑‒–—]", cased[window[-2][2]:window[-1][1]]):
                break                                     # punctuation between: not one word ("fab - ricated" is)
            pieces = [word.casefold() for word, _, _ in window]
            if any(LONG_STEMS.search(piece) for piece in pieces):
                break
            joined = "".join(pieces)
            match = LONG_STEMS.search(joined)
            if not match or match.start() != 0:          # "Themis conduct" is not "misconduct"
                continue
            last_start = len(joined) - len(pieces[-1])
            if match.end() < last_start:                  # the last piece is not part of the word
                continue
            if match.end() == last_start and pieces[-1] not in ENDINGS:
                continue                                  # "fabricat scale": a word, not an ending
            if match.end() == len(joined) and match.group(0) not in WHOLE_STEMS:
                continue                                  # "fabric at": no ending
            originals = [word for word, _, _ in window]
            if originals[0].isupper() and len(originals[0]) <= 5 and not originals[1].isupper():
                continue                                  # "UN ethical": an acronym
            if all(word[:1].isupper() and word[1:].islower() for word in originals):
                continue                                  # "Mani Pulido": names
            found.append(match.group(0))
    return found


def _accusations(text: str) -> list[str]:
    raw = str(text or "")
    if BIDI.search(raw) or (DIRECTION_MARKS.search(raw) and not RIGHT_TO_LEFT.search(raw)):
        return ["text-direction control characters"]
    raw = NAME_WITH_INITIAL.sub(" ", raw)
    value = screening_form(raw)
    cased = screening_form(raw, fold=False)
    # "f r a u d": letters spaced apart are read as one word.
    value = re.sub(r"\b(\w)\s+(?=\w\b)", r"\1", value)
    cased = re.sub(r"\b(\w)\s+(?=\w\b)", r"\1", cased)
    value, cased = _without_topics(value), _without_topics(cased)
    german = len(GERMAN.findall(value)) >= 2
    found = [match.group(0) for match in ACCUSATION.finditer(value)]
    found += _betrug(cased)
    found += [match.group(0) for match in LIE.finditer(cased) if "Lie" not in match.group(0)]
    found += [match.group(0) for match in MISLED.finditer(value)]
    for match in LIED.finditer(cased):
        word = match.group(0)
        article = re.search(r"\b(?:das|ein|im|dem|des|vom|zum|kein|sein|ihr|dieses|jedes|ihrem|seinem)\s+$",
                            cased[max(0, match.start() - 10):match.start()], re.IGNORECASE)
        if word == "lied" or word.isupper() or not (german or article):
            found.append(word)
    for match in MADE_UP.finditer(value):
        between = match.group(1).split()
        first = between[0]
        if (first in MADE_UP_OBJECTS or first.startswith("[") or first.isdigit()) and between[-1] not in PHRASAL:
            found.append(match.group(0))
    # Digits standing in for letters inside a word ("misc0nduct", "fr4ud", "fabric8ed").
    eights = re.sub(r"(?<=[^\W\d_])8(?=[^\W\d_]|\b)", "ate", value)
    for table in (LEET, LEET_L):                      # "1" stands for "i" or "l" ("l1ed", "1ied")
        leet = re.sub(r"(?<=[^\W\d_])[0-9@$]+(?=[^\W\d_])|(?<=[^\W\d_])[0-9@$]+\b|\b[0-9@$]+(?=[^\W\d_])",
                      lambda match: match.group(0).translate(table), eights)
        if leet != value:
            found += [match.group(0) for match in ACCUSATION.finditer(leet)]
            # Only words the digits made: "Lied" and "betrug" already read above are not new.
            for pattern in (BETRUG, LIED):
                if len(pattern.findall(leet)) > len(pattern.findall(value)):
                    found.append(pattern.pattern)
    found += _joined(cased)
    return found


def accusation(text: str) -> str | None:
    """The accusatory word in `text`, if any."""
    found = _accusations(text)
    return found[0] if found else None


def numbers_in(text: str) -> set[str]:
    """Every number the text states: digit runs (also inside "47th", "12x") and spelled-out numbers."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    found = {number.replace(",", ".") for number in DIGITS.findall(value)}
    words = re.findall(r"[^\W\d_]+", value.casefold())
    index = 0
    while index < len(words):
        total, matched = 0, False
        while index < len(words) and (words[index] in UNITS or words[index] in TENS or words[index] in SCALES
                                      or re.fullmatch(r"(?:(\w+)und)?(\w+zig)", words[index] or "")):
            current = words[index]
            if current in AMBIGUOUS_WORDS and not matched:
                break
            if current in SCALES:
                total = max(total, 1) * SCALES[current]
            elif current in TENS:
                total += TENS[current]
            elif current in UNITS:
                total += UNITS[current]
            else:  # German compounds: "siebenundvierzig"
                compound = re.fullmatch(r"(?:(\w+)und)?(\w+zig)", current)
                if compound.group(2) not in TENS or (compound.group(1) and compound.group(1) not in UNITS):
                    break
                total += TENS[compound.group(2)] + UNITS.get(compound.group(1) or "", 0)
            matched = True
            index += 1
        if matched:
            found.add(str(total))
        else:
            index += 1
    return found


def allowed_numbers(*texts: str) -> set[str]:
    """Numbers a model answer may state: those in the given texts, also rounded."""
    allowed = set()
    for text in texts:
        for number in DIGITS.findall(str(text or "")):
            value = number.replace(",", ".")
            allowed.add(value)
            try:
                allowed.add(str(round(float(value))))
                allowed.add(str(int(float(value))))
            except ValueError:
                pass
    return allowed


COUNT_NOUNS = (r"references?|sources?|citations?|items?|works?|entries|entry|findings?|checks?|self-citations?|"
               r"authors?|papers?|referenzen|quellen|zitate|einträge|eintrag|punkte|arbeiten|werke|selbstzitate")
REFERENCE_NOUNS = (r"references?|sources?|works?|entries|entry|papers?|items?|self-citations?|referenzen|quellen|"
                   r"einträge|eintrag|arbeiten|werke|selbstzitate")
PLURAL_NOUNS = (r"references|sources|works|entries|papers|items|citations|findings|checks|self-citations|authors|"
                r"referenzen|quellen|einträge|arbeiten|werke|zitate|selbstzitate")
SPELLED = {**{word: value for word, value in UNITS.items() if word not in AMBIGUOUS_WORDS}, **TENS}
NUMBER = r"\d+|" + "|".join(sorted(SPELLED, key=len, reverse=True))
OF = r"(?:out\s+of|of|von)"
DETERMINER = r"(?:the|these|those|its|den|die|diesen)"
NOT_A_COUNT = r"(?!\s*(?:%|percent|per\s*cent|prozent|years?|jahre|pages?|seiten|times|mal|[.,/]\d))"
# After "references are 35": a score or a list ("35 and 48 out of 100", "4, 5 and 6"), not a count.
NOT_A_TALLY = r"(?!\s*(?:(?:,|and|und|or|oder)\s*\d|out\s+of|of\s+100|von\s+100|/\s*\d))"
COUNTING_HEADS = {"number", "count", "total", "amount", "anzahl", "zahl", "gesamtzahl"}


def count_spans(text: str, nouns: str = COUNT_NOUNS, *, pronouns: bool = False) -> list[tuple[str, int, int, int]]:
    """(number, start, end, phrase end) for each number stated as a count of `nouns`; the
    phrase runs from the number to its noun ("7 matched references").

    "3 references", "three of the sources", "5 of the 8 references", "5 out of 8 references",
    "5 (of 8) references", "5/8 references", "Of the 8 references, 5 …", "Irrelevant references: 5",
    "The number of irrelevant references is 5"; with `pronouns`, also "5 of them". Not counts:
    a year before a noun ("the 2015 paper"), a share ("50 percent of the references", "1/3 of
    the references") or a score ("the scores of the references are 35 and 48 out of 100").
    """
    text = unicodedata.normalize("NFKC", str(text or ""))
    plural = PLURAL_NOUNS if nouns == COUNT_NOUNS else "|".join(
        noun for noun in PLURAL_NOUNS.split("|") if re.fullmatch(nouns, noun))
    share = r"(?!\s*(?:%|percent|per\s*cent|prozent))"
    patterns = [
        rf"(?<![\d/.,])\b({NUMBER}){share}(?:\s+\(?{OF}\s+(?:{DETERMINER}\s+)?({NUMBER})\)?)?"
        rf"(?:\s+{OF}\s+{DETERMINER})?(?:\s+[^\W\d_][\w-]*){{0,3}}?\s+(?:{nouns})\b",
        rf"\b({NUMBER})\s*/\s*({NUMBER})(?:\s+(?!of\b|von\b)[^\W\d_][\w-]*){{0,3}}?\s+(?:{nouns})\b",
        rf"\b{OF}\s+(?:{DETERMINER})?\s*({NUMBER})\s+(?:[^\W\d_][\w-]*\s+){{0,2}}?(?:{nouns}),?\s+({NUMBER})\b{share}",
    ]
    stated = re.compile(rf"\b(?:{plural})\s*(?::|=|\bis\b|\bare\b|\bwas\b|\bwere\b|\btotals?\b|\bcomes?\s+to\b|"
                        rf"\bstands?\s+at\b|\bbeträgt\b|\bsind\b|\bwaren\b)\s*({NUMBER})\b{NOT_A_COUNT}{NOT_A_TALLY}",
                        re.IGNORECASE)
    if pronouns:
        patterns.append(rf"\b({NUMBER})\s+(?:of|von)\s+(?:them|ihnen)\b")
        # "7 references were matched, and 3 could not be found": the noun is understood.
        if re.search(rf"\b(?:{nouns})\b", text, re.IGNORECASE):
            patterns.append(rf"(?:,|;|\band\b|\bbut\b|\bwhile\b|\bund\b|\baber\b)\s+(?:only\s+|just\s+|the\s+other\s+)?"
                            rf"({NUMBER})\s+(?=(?:was|were|is|are|could|can|may|might|did|do|does|have|has|had|need|"
                            rf"needs|should|seem|seems|look|looks|wurden|wurde|sind|ist|konnten|konnte|könnten|"
                            rf"sollten)\b)")
    spans = []

    def add(match, groups):
        for index in groups:
            group = match.group(index)
            if not group:
                continue
            value = str(SPELLED.get(group.casefold(), group))
            if re.fullmatch(r"1[5-9]\d\d|20\d\d", value):
                continue
            spans.append((value, match.start(index), match.end(index), max(match.end(), match.end(index))))

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            add(match, range(1, len(match.groups()) + 1))
    for match in stated.finditer(text):
        # "The scores of the matched references are 62": the number belongs to "scores";
        # "The number of irrelevant references is 5" is a count.
        head = re.search(r"\b([^\W\d_]+)\s+(?:of|der|des|von)\s+(?:the\s+|den\s+|die\s+|beiden\s+|two\s+)?"
                         r"(?:[^\W\d_][\w-]*\s+){0,3}$", text[max(0, match.start() - 80):match.start()], re.IGNORECASE)
        if head and head.group(1).casefold() not in COUNTING_HEADS:
            continue
        add(match, (1,))
    # One number read by two patterns is one count, with the longer phrase.
    longest: dict[tuple[str, int, int], int] = {}
    for value, start, end, phrase_end in spans:
        longest[(value, start, end)] = max(phrase_end, longest.get((value, start, end), end))
    return sorted(((value, start, end, phrase_end) for (value, start, end), phrase_end in longest.items()),
                  key=lambda span: span[1])


def _counted(text: str, nouns: str, *, pronouns: bool = False) -> set[str]:
    return {value for value, _, _, _ in count_spans(text, nouns, pronouns=pronouns)}


def counted_numbers(text: str) -> set[str]:
    """Numbers stated as a count of something ("3 references", "two of the 8 sources")."""
    return _counted(text, COUNT_NOUNS)


def referenced_counts(text: str) -> set[str]:
    """Numbers stated as a count of references (not of citations or checks)."""
    return _counted(text, REFERENCE_NOUNS, pronouns=True)


def _normal(text: str) -> str:
    return " ".join(screening_form(text).split())


OPEN_QUOTES = "“„\"‘‚«‹»›'"            # »Titel« is the German way round
CLOSE_QUOTES = "”“\"’‘»›«‹'"


def _without_quoted_titles(text: str, titles: set[str]) -> str:
    """`text` with each whole title blanked where it stands in quotation marks — that exact span only."""
    for title in sorted(titles, key=len, reverse=True):
        words = str(title).split()
        if len(words) < 3:
            continue
        pattern = (f"[{re.escape(OPEN_QUOTES)}]\\s*" + r"\s+".join(re.escape(word) for word in words)
                   + f"[,.;:!?]?\\s*[{re.escape(CLOSE_QUOTES)}]")
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return text


def check(text: str, *, allowed: set[str], markers: set[str], reference_count: int,
          counts: set[str] | None = None, titles: set[str] | None = None, **_ignored) -> str | None:
    """None when `text` may be shown; otherwise why it may not.

    No word of accusation may appear in a model-written text, translations included —
    the built-in answers avoid that vocabulary, so a faithful translation has none either.
    The one exception is a whole reference or manuscript title of three or more words,
    quoted as such ("“Data manipulation in spreadsheets”"): it is data, not the model's
    words. `counts` are the real counts of this audit: a number stated as a count of
    references must be one of them.
    """
    screened = _without_quoted_titles(text, titles or set())
    words = _accusations(screened)
    if words:
        return f"accusatory language ({words[0]})"
    unknown = sorted(number for number in numbers_in(text) if number not in allowed)
    if unknown:
        return f"numbers not in the audit facts ({', '.join(unknown[:5])})"
    if counts is not None:
        wrong = sorted(number for number in counted_numbers(text) if number not in counts)
        if wrong:
            return f"counts that are not this audit's ({', '.join(wrong[:5])})"
    for number in MARKER.findall(text) + REFERENCE_WORD.findall(text):
        if number not in markers and not (number.isdigit() and 0 < int(number) <= reference_count and not markers):
            return f"a reference that does not exist ({number})"
    return None


__all__ = ["accusation", "allowed_numbers", "check", "count_spans", "counted_numbers", "numbers_in",
           "referenced_counts", "screening_form"]
