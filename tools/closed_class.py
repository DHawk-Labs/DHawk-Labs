"""
Authoritative closed-class (function word) lexicon seeds.

WordNet only covers the four open content classes (noun, verb, adjective,
adverb). The lexicon schema, however, defines fine-grained function-word
domains (PRON, DET, NUM, PREP, CONJ, AUX, INTERJ). These are *closed* classes:
their membership is small, finite and stable, so we enumerate them from
authoritative grammar references rather than scraping a dictionary.

Every entry is a lowercase canonical form. Forms that are also legitimate
content words (e.g. "can", "will", "one") still get their content-word entries
from WordNet under a different domain key, so no information is lost: the
entries are keyed by ``<DOM>.<word>`` and therefore coexist.

Each list is curated, de-duplicated and intentionally conservative — we would
rather omit a borderline item than admit noise.
"""

# ---------------------------------------------------------------------------
# PRON — pronouns
# ---------------------------------------------------------------------------
PRON = [
    # personal (subject / object)
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them",
    # possessive pronouns
    "mine", "yours", "his", "hers", "its", "ours", "theirs",
    # possessive determiners are listed under DET, not here
    # reflexive / intensive
    "myself", "yourself", "himself", "herself", "itself",
    "ourselves", "yourselves", "themselves", "oneself",
    # demonstrative (as pronouns)
    "this", "that", "these", "those",
    # interrogative / relative
    "who", "whom", "whose", "which", "what",
    "whoever", "whomever", "whichever", "whatever",
    # indefinite
    "anybody", "anyone", "anything", "everybody", "everyone", "everything",
    "nobody", "none", "nothing", "somebody", "someone", "something",
    "each", "either", "neither", "one", "ones", "other", "others",
    "both", "few", "many", "several", "all", "any", "some",
    # archaic / dialectal but standard
    "thou", "thee", "thy", "thine", "ye", "yourselves",
    "naught", "aught", "somewhat",
]

# ---------------------------------------------------------------------------
# DET — determiners (articles, demonstratives, possessive determiners,
#        quantifying determiners)
# ---------------------------------------------------------------------------
DET = [
    # articles
    "a", "an", "the",
    # possessive determiners
    "my", "your", "his", "her", "its", "our", "their", "whose",
    # demonstratives (determiner use)
    "this", "that", "these", "those",
    # quantifying / distributive
    "all", "another", "any", "both", "each", "either", "enough", "every",
    "few", "fewer", "less", "little", "many", "more", "most", "much",
    "neither", "no", "several", "some", "such", "what", "whatever",
    "which", "whichever",
]

# ---------------------------------------------------------------------------
# NUM — numerals (cardinal + ordinal + multiplicative roots)
# ---------------------------------------------------------------------------
NUM = [
    # cardinals
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
    "hundred", "thousand", "million", "billion", "trillion", "quadrillion",
    "quintillion", "sextillion", "septillion", "octillion", "nonillion",
    "decillion", "googol", "myriad", "score", "dozen", "gross",
    # ordinals
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
    "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
    "nineteenth", "twentieth", "thirtieth", "fortieth", "fiftieth",
    "sixtieth", "seventieth", "eightieth", "ninetieth", "hundredth",
    "thousandth", "millionth", "billionth",
    # fractional / multiplicative
    "half", "quarter", "third", "double", "triple", "quadruple",
    "quintuple", "single", "twofold", "threefold", "tenfold",
]

# ---------------------------------------------------------------------------
# PREP — prepositions (simple)
# ---------------------------------------------------------------------------
PREP = [
    "aboard", "about", "above", "across", "after", "against", "along",
    "alongside", "amid", "amidst", "among", "amongst", "around", "as", "at",
    "atop", "before", "behind", "below", "beneath", "beside", "besides",
    "between", "beyond", "but", "by", "concerning", "considering",
    "despite", "down", "during", "except", "excepting", "excluding",
    "following", "for", "from", "in", "inside", "into", "like", "minus",
    "near", "notwithstanding", "of", "off", "on", "onto", "opposite",
    "out", "outside", "over", "past", "per", "plus", "regarding",
    "round", "save", "since", "than", "through", "throughout", "till",
    "to", "toward", "towards", "under", "underneath", "unlike", "until",
    "unto", "up", "upon", "versus", "via", "with", "within", "without",
]

# ---------------------------------------------------------------------------
# CONJ — conjunctions (coordinating + subordinating + correlative parts)
# ---------------------------------------------------------------------------
CONJ = [
    # coordinating
    "and", "but", "or", "nor", "for", "yet", "so",
    # subordinating
    "after", "although", "as", "because", "before", "if", "lest", "once",
    "since", "than", "that", "though", "till", "unless", "until", "when",
    "whenever", "where", "whereas", "wherever", "whether", "while", "whilst",
    "albeit", "provided", "providing", "supposing", "considering",
    # correlative parts / connectives
    "both", "either", "neither", "whether", "however", "moreover",
    "nevertheless", "nonetheless", "therefore", "thus", "hence",
    "otherwise", "meanwhile", "furthermore", "consequently", "accordingly",
]

# ---------------------------------------------------------------------------
# AUX — auxiliary and modal verbs (all inflected forms)
# ---------------------------------------------------------------------------
AUX = [
    # be
    "be", "am", "is", "are", "was", "were", "been", "being",
    # have
    "have", "has", "had", "having",
    # do
    "do", "does", "did", "doing", "done",
    # modals
    "will", "would", "shall", "should", "can", "could", "may", "might",
    "must", "ought", "dare", "need", "used",
    # common contractions (canonicalized without apostrophe where standard)
    "wo", "ca", "sha",  # the stems in won't/can't/shan't tokenization
]

# ---------------------------------------------------------------------------
# INTERJ — interjections / exclamations
# ---------------------------------------------------------------------------
INTERJ = [
    "ah", "aha", "ahem", "ahoy", "alas", "amen", "argh", "aw", "aww",
    "bah", "bingo", "boo", "bravo", "brr", "duh", "eek", "eh", "encore",
    "eureka", "gee", "gosh", "ha", "hah", "hallelujah", "hello", "hey",
    "hi", "hmm", "hooray", "hurray", "huh", "hush", "ick", "jeez", "meh",
    "oh", "oho", "ooh", "oops", "ouch", "ow", "phew", "phooey", "pooh",
    "psst", "shh", "shoo", "tsk", "ugh", "uh", "um", "voila", "wahoo",
    "whoa", "whoops", "wow", "yay", "yikes", "yippee", "yo", "yuck", "yum",
    "bye", "goodbye", "farewell", "cheers", "congratulations", "ok", "okay",
    "please", "thanks", "welcome", "yes", "no", "nope", "yeah", "yep",
]


def all_closed_classes():
    """Return ``{DOMAIN: sorted_unique_lowercase_words}`` for every seed list."""
    raw = {
        "PRON": PRON,
        "DET": DET,
        "NUM": NUM,
        "PREP": PREP,
        "CONJ": CONJ,
        "AUX": AUX,
        "INTERJ": INTERJ,
    }
    out = {}
    for dom, words in raw.items():
        cleaned = sorted({w.strip().lower() for w in words if w and w.strip()})
        out[dom] = cleaned
    return out


if __name__ == "__main__":
    cc = all_closed_classes()
    total = 0
    for dom, words in cc.items():
        total += len(words)
        print(f"{dom:8s} {len(words):4d}  {', '.join(words[:8])} ...")
    print(f"TOTAL closed-class entries: {total}")
