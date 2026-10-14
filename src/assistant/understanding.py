"""What a reader is asking: topics, register, follow-ups and requested outputs.

Readers ask in their own words, often several things at once ("I got the
results and I don't know what to do with the actions — explain it like I'm
five and make me a PDF"). This module turns one message into:

* ``intents`` — every topic asked about, in the order the reader raised them;
* register modifiers — ``simple`` (no background), ``brief``, ``detailed``;
* ``wants_report`` — a document to download;
* ``followup`` — a reply that only makes sense with the previous answer
  ("why?", "tell me more", "the second one", "yes please");
* ``language`` — English or German.

It is deterministic and dependency-free, so it works without any model and
always gives the same reading of the same words.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from src.assistant.content import GLOSSARY_PATTERNS
from src.assistant.lexicon import REAL_WORDS

# Topic patterns. A pattern may stand for more than one topic: "what do I do
# with them?" after "I got the results" asks both what to check first and how
# the review's decision buttons work.
TOPICS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("actions_help",), (
        r"\bactions?\b", r"\bbuttons?\b", r"\bmark (it|them|this|an item|items)\b",
        r"\b(confirm(ed)?|dismiss(ed)?|escalat\w*)\b.*\b(mean|do|use|differ|choose|pick)\b",
        r"\bwhat (does|do|is) (confirm(ed)?|dismiss(ed)?|escalat\w*|needs? more evidence)\b",
        r"\bhow do i (mark|decide|review|resolve|close|accept|reject|approve|record)\b",
        r"\b(make|record|save) (a |my )?decisions?\b",
        r"\bwas (mache|tue) ich (damit|mit (den|diesen|ihnen))\b", r"\bknöpfe\b", r"\bschaltfläche", r"\baktionen\b")),
    (("next_steps", "actions_help"), (
        r"\bwhat (do|should|can|must) i do with (them|these|those|this|it|the (results?|items?|findings?|flags?|"
        r"warnings?|notes?))\b",
        r"\bdon'?t know what to do\b", r"\bnot sure what to do\b", r"\bwhat am i supposed to do\b",
        r"\bweiß nicht,? was ich (tun|machen) soll\b")),
    (("next_steps",), (
        r"\bwhat (should|do|must|can) i (do|check|look at|review|read)\b(?! with)",
        r"\bwhat (should|do|must|can|would) (i|we|you) (fix|correct|change|tackle|handle|address|work on)( first)?\b",
        r"\b(fix|correct|change|tackle|handle) first\b",
        r"\bnext steps?\b", r"\bwhere (do|should|can) i (start|begin)\b", r"\bpriorit",
        r"\bmost (important|urgent|serious|critical)\b", r"\bcheck first\b", r"\bfirst (thing|step)s?\b",
        r"\bstart with\b", r"\bwhat matters\b", r"\bto-?do\b", r"\bworth (a|a closer|another|a second) look\b",
        r"\bwhich (ones?|references?|refs?|sources?|citations?|entries|items?)\b[^.?!]{0,30}\b(wrong|bad|incorrect|"
        r"problem\w*|suspicious|questionable|flagged|broken|off|weird|fishy|dodgy|need\w* (checking|attention|a look|"
        r"fixing)|worth (checking|a look)|to check)\b",
        # "errors in the reference list" is about the entries' details (metadata), not this list.
        r"\b(problems?|issues?|errors?|mistakes?) (with|in) (the |my |this |its |our )?(references?|refs|sources?|"
        r"citations?|paper|manuscript|chapter)\b(?! list)",
        r"\bwhat (needs|need) (fixing|attention|checking|to be (fixed|checked))\b",
        r"\b(?:what|which|any|are there|were there|did you find) (?:errors?|mistakes?|problems?|issues?)\b"
        r"(?![^?.!]{0,30}\b(?:reference|bibliograph\w*|citation) list)",
        r"\bexplain (?:the |these |those |all )?(?:errors?|mistakes?|problems?|issues?|flags?)\b",
        r"\bwhich (?:references?|refs?|sources?|ones?|entries|items?) (?:have|has|contain|show|got) (?:any )?(?:errors?|"
        r"mistakes?|problems?|issues?|missing (?:data|details|information|fields|dois?))\b",
        r"\b(?:matters?|count) (?:the )?most\b",
        r"\bwelche (?:fehler|probleme)\b", r"^\W*(?:any\s+)?(?:errors?|mistakes?|problems?|issues?)\W*$",
        r"\bwelche (quellen|referenzen|zitate|einträge)\b.{0,30}\b(falsch|problematisch|fehlerhaft|prüfen)\b",
        r"\bwas (soll|muss|kann|sollte) ich\b.{0,30}\b(tun|machen|prüfen|ansehen|anschauen|beachten|korrigieren)\b",
        r"\bnächsten? schritte?\b", r"\bwomit (soll ich )?(anfangen|beginnen)\b", r"\bzuerst\b",
        r"\bwo (fange|soll) ich an\b")),
    (("improve",), (
        r"\bimprov", r"\brecommend", r"\bmake (it|the paper|my paper|this|the references?) better\b",
        r"\bhow (do|can|should|would) i (fix|correct|repair|solve|resolve|address)\b",
        r"\bfix(es)? (for|to)\b", r"\bsteps to (fix|improve|correct)\b", r"\bsuggest(ed|ions)? (fix|change)",
        r"\bverbesser", r"\bkorrigier", r"\bempfehl")),
    # "Is this paper any good?" asks for a judgement CitePrism does not make; the
    # answer says so and reports what the citation check does show.
    (("verdict",), (
        r"\b(is|was) (it|this|the|my|our|their)( \w+)? (paper|manuscript|chapter|book|article|thesis|work|submission)?"
        r" ?(any good|good|ok|okay|fine|alright|solid|sound|trustworthy|reliable|credible|publishable|acceptable|ready|"
        r"bad|poor|weak|strong|worth (it|reading|publishing))\b",
        r"\b(is|was) (it|this|that) (any good|good|ok|okay|fine|alright|bad|publishable|acceptable)\b",
        r"\b(should|can|could|would) (i|we|you) (accept|reject|publish|approve|recommend|trust|believe) "
        r"(it|this|the (paper|manuscript|chapter|book|article|submission))\b",
        r"\b(rate|grade|judge|assess|evaluate) (the|this|my|our) (paper|manuscript|chapter|book|quality|work)\b",
        r"\bquality of (the|this|my|our) (paper|manuscript|chapter|book|work|research)\b",
        r"\bhow (good|bad|strong|weak|solid) (is|are) (the|this|my|our|its) (paper|manuscript|chapter|book|work|"
        r"references|citations|bibliography|reference list)\b",
        r"\b(overall )?verdict\b", r"\bpass or fail\b", r"\bthumbs (up|down)\b",
        r"\bist (das|die arbeit|der artikel|das kapitel|das paper) (gut|in ordnung|brauchbar|seriös)\b")),
    (("overview",), (
        r"\bsummar", r"\boverall\b", r"\boverview\b",
        r"\bhow (did|is|was) (it|the paper|this paper|my paper|the check|the audit)\b",
        r"\bany (problems|issues)\b",
        r"\btl;?dr\b", r"\bwhat (did you|does it|did it|did citeprism) find\b", r"\bwhat'?s (wrong|going on)\b",
        r"\bwhat went wrong\b",
        r"\bwhich (?:references?|sources?|ones?|works?) (?:were|are|got|have been) (?:resolved|matched|found|identified)\b",
        r"\bwhat (happened|came out)\b",
        r"\bzusammenfass", r"\büberblick\b", r"\bwas (hast du|wurde) gefunden\b")),
    # Generic words for the results: a summary only when nothing more specific is asked.
    (("overview_weak",), (
        r"\bresults?\b", r"\bfindings?\b", r"\bfinds\b", r"\bexplain (everything|all|the results)\b",
        r"\beverything\b", r"\bergebnis")),
    (("letter",), (
        r"\b(write|draft|compose|prepare|create|make|give)\b[^.?!]{0,40}\b(e-?mails?|letters?|messages?|notes?|"
        r"comments?|feedback|responses?|replies|reply)\b[^.?!]{0,40}\b(to|for) (the |my |our )?(authors?|editors?|"
        r"writers?|students?|colleagues?|co-?authors?|team|supervisor|professor)\b",
        r"\b(e-?mail|letter|message|note|comments?|feedback) (to|for) (the |my |our )?(authors?|editors?|writers?|"
        r"students?|co-?authors?)\b",
        r"\breviewer'?s? (comments|report|letter|feedback)\b", r"\bwhat (should|do|can) i (tell|write to|say to) "
        r"(the )?(authors?|editors?|students?)\b", r"\b(tell|inform|notify) the authors?\b",
        r"\b(schreib|entwirf|verfass|formulier)\w*\b.{0,40}\b(e-?mail|brief|nachricht|gutachten)\b",
        r"\b(e-?mail|brief|nachricht) an (die )?(autor\w*|herausgeber\w*)\b")),
    # Requests to call the authors dishonest. CitePrism points to what is worth
    # checking; it never decides why something happened.
    (("boundaries",), (
        r"\b(say|declare|state|confirm|prove|conclude|write|tell (me|them|us)|accuse|report|admit|call (them|it|this|that))\b"
        r"[^.?!]{0,50}\b(fraud\w*|misconduct|cheat\w*|plagiar\w*|dishonest\w*|manipulat\w*|lying|lied|liars?|"
        r"scam\w*|unethical|corrupt\w*|deceiv\w*|deceptive)\b",
        r"\b(did|do|does|are|is|were|was|have|has) (the |these |those )?(authors?|they|he|she|writers?|researchers?)"
        r"\b[^.?!]{0,40}\b(fraud\w*|misconduct|cheat\w*|plagiar\w*|dishonest\w*|lie|lying|lied|unethical|deceiv\w*|"
        r"fake (data|results|citations|references)|manipulat\w* (the |their |these |those |any |some )?(data|results|"
        r"findings|figures|images|numbers|references|citations|sources|quotes|evidence|statistics|analysis|peer review))\b",
        r"\b(plagiari[sz]ed|copied|stolen|taken) from (ours|us|our \w+|my \w+|me|mine)\b",
        r"\b(did|do|does|have|has) (the |these |those )?(authors?|they|he|she|writers?|researchers?) (make|made) (it|this|"
        r"that|them|everything|all of it|all this|(the|their|these|those) \w+)( all)? up\b",
        r"\b(authors?|they|he|she|writers?|researchers?) (made|make) (it|this|that|them|everything)( all)? up\b",
        r"\b(authors?|they|he|she) (committed|commit|commits|are guilty|is guilty|must be punished)\b",
        r"\b(fraud\w*|misconduct|plagiar\w*|cheat\w*|dishonest\w*|unethical)\b(?!\s+(?:detection|literature|"
        r"research|stud\w+|prevention|analysis|methods?|models?))[^.?!]{0,30}\b(authors?|they)\b",
        r"\bis (this|it|that|there) (a case of |any )?(research |academic |scientific )?(misconduct|fraud\w*|plagiar\w*|"
        r"cheat\w*|dishonest\w*|manipulat\w*|unethical)\b(?!\s+(?:detection|literature|research|studies|study|"
        r"prevention|analysis|methods?|models?|data|cases?|papers?|theory|software|tools?|missing|covered|cited))",
        r"^\W*(?:any\s+)?(?:fraud|plagiarism|misconduct|cheating|dishonesty)\s+(?:in|by)\s+(?:the|this|my)\s+"
        r"(?:paper|manuscript|chapter|article|thesis|authors?)\b",
        r"\b(?:(?:this|it|that) (?:is|was|counts as|amounts to|constitutes)|(?:is|was) (?:this|it|that)|"
        r"call (?:this|it|that))\s+(?:a case of )?(?:academic|research|scientific) (?:dishonesty|misconduct|fraud)\b",
        r"\b(did|has|have|is) (someone|anyone|somebody|any ?one) (cheat\w*|lie|lied|lying|fake\w*|fabricat\w*|plagiar\w*)\b",
        r"\b(authors?|they|he|she|writers?) (are|is|were|was) (lying|liars?|cheat\w*|dishonest|frauds?|crooks?)\b",
        r"\b(authors?|they|he|she|writers?) (faked|fabricated|plagiari[sz]ed|cooked)\b",
        r"\b(authors?|they|he|she|writers?) (invented|made up|manipulated|doctored|altered) (the |their |some |these |"
        r"those )?(data|results|findings|figures|images|numbers|references|citations|sources|quotes|evidence)\b",
        r"\b(did|do|does) (the |these |those )?(authors?|they|he|she|writers?|researchers?) (invent|make up|fabricate|"
        r"fake|falsify|manipulate|doctor|forge|plagiari[sz]e|copy|steal|cook) (the |their |some |these |those |any )?"
        r"(data|results|findings|figures|images|numbers|references|citations|sources|quotes|evidence|text|work)\b",
        r"\b(did|do|does) (the |these |those )?(authors?|they|he|she|writers?|researchers?) (fake|fabricate|falsify|"
        r"forge|doctor|plagiari[sz]e|steal|make up|cook|invent)\s*(?:(the |their |some |these |those |any |this |that )?"
        r"(data|results|findings|figures|images|numbers|references?|citations?|sources?|quotes|evidence|text|work|paper|"
        r"study|manuscript|chapter|it|this|that|them|reference \d+|source \d+|ideas?|words|sentences|paragraphs)\b|"
        r"\[\d+\]|\W*$)",
        r"\b(did|does|has) (?!the\b|this\b|that\b|it\b|anyone\b|someone\b)[a-zäöüß'-]{2,} (plagiari[sz]ed?|cheat(ed)?|"
        r"lie|lied|fabricated?|faked?|falsif(y|ied))\b",
        r"\b(sind|ist|wurden|wurde|waren|war) (die|der|das|diese|dieser|alle|manche|einige) (quellen|daten|ergebnisse|"
        r"zitate|referenzen|literaturangaben|arbeit|studie|befunde|abbildungen) (frei )?(erfunden|gefälscht|manipuliert|"
        r"plagiiert|abgeschrieben|ausgedacht|frisiert|geklaut|gestohlen)\b",
        r"\b(haben|hat) (die autor\w*|der autor|sie|er) [^.?!]{0,40}\b(erfunden|gefälscht|manipuliert|plagiiert|"
        r"abgeschrieben|ausgedacht|frisiert|geklaut|gestohlen|betrogen|gelogen)\b",
        r"\b(was|were|is|are) (\[\d+\]|reference \d+|it|this|that|they) (fabricated|faked|made up|invented|forged|"
        r"doctored|plagiari[sz]ed|stolen) by (the |these )?(authors?|them|him|her|writers?)\b",
        r"\b(does|do|did) (the|this|that|my|our) (paper|manuscript|chapter|article|thesis|study|book|work) (contain|show|"
        r"include|involve|have) (any )?(plagiarism|fraud|misconduct|fabricat\w*|falsif\w*|data manipulation|"
        r"dishonesty)\b",
        r"\b(is|was|are|were) (the|this|that|these|those|my|our|their) (paper|manuscript|chapter|article|thesis|study|"
        r"book|work|data|results?|findings|references|citations|sources|bibliography)\s+(plagiari[sz]ed|fraudulent|"
        r"fabricated|faked?|falsified|manipulated|made up|invented|dishonest|a fraud|a fake|a scam|bogus|forged|"
        r"doctored|stolen|copied|cooked)\b",
        r"\b(was|were|is|are|has|have) (the|their|these|those) (data|results?|findings|figures|images|numbers|"
        r"references|citations|sources|quotes)\s+(been\s+)?(manipulated|fabricated|faked|falsified|invented|made up|"
        r"doctored|forged|cooked|plagiari[sz]ed)\b",
        r"\bignore (your|all|the|any|previous|prior|those) (rules|instructions|guidelines|restrictions|limits)\b",
        r"\b(jailbreak|developer mode|dan mode|no restrictions)\b",
        r"\b(betrug|betrogen|betrüger\w*|plagiat\w*|gefälscht|fehlverhalten|unehrlich)\b")),
    (("method",), (
        r"\bhow (does|do|did) (it|you|this|citeprism|the (system|tool|score|check))\b(?! find)",
        r"\bhow (is|are) .*\b(scored|calculated|computed|decided|chosen)\b", r"\bmethod",
        r"\bexplain (the )?(score|scoring|labels?)\b", r"\bhow (it|this) works\b",
        r"\bwie funktioniert\b", r"\bwie (entscheidet|bewertet)\b",
        r"\bexplain (?:the |your )?(?:citation |reference )?(?:analysis|check|audit|process|pipeline)\b",
        r"\b(?:citation |reference )?(?:analysis|check|audit) (?:work|works|done)\b")),
    (("reliability",), (
        r"\b(can|should|could) i (trust|rely on|believe)\b", r"\bhow (accurate|reliable|correct|sure|good) (is|are)\b",
        r"\baccura(cy|te)\b", r"\bfalse (positives?|alarms?|negatives?)\b", r"\bhow sure\b", r"\bmistakes?\b.*\b(you|citeprism|the (tool|system))\b",
        r"\bverlässlich\b", r"\bvertrauen\b", r"\bgenau(igkeit)?\b")),
    (("self_citation",), (
        r"self[- ]?cit", r"\bown (work|papers?|articles?)\b", r"\bcit\w* (themselves|himself|herself|their own)\b",
        r"\bsame authors?\b", r"\bauthor overlap\b", r"\bselbstzit", r"\beigene (arbeiten|publikationen)\b")),
    (("missing",), (
        r"\bmissing\b(?! (dois?|years?|titles?|authors?|details?|fields?|data|information|info))",
        r"\bforg[eo]t", r"\bshould (also )?(have )?cit", r"\bgaps?\b", r"\black(ing|s)?\b",
        r"\bsuggest(ions?)?\b(?! (fix|change))", r"\bimportant (work|paper|literature|studies)\b",
        r"\bwhat else\b", r"\bunderci", r"\bfoundational\b", r"\bkey (literature|works?|papers?)\b",
        r"\bfehlend", r"\bfehlt\b", r"\bvergessen\b")),
    (("irrelevant",), (
        r"irrelevan", r"not relevant", r"\bunrelated\b", r"\boff[- ]topic\b", r"\brelevan", r"\bpadding\b",
        r"\bdon'?t (belong|fit)\b", r"\bwrong sources?\b", r"\bpass(t|en) nicht\b", r"\bnicht relevant\b", r"\bunpassend",
        r"\bmay not fit\b", r"\bdoes(n'?t| not) fit\b")),
    (("support",), (
        r"\bsupport", r"\bback(s|ed)? (it |them |this |that )?up\b", r"\bcontradict", r"\bclaims?\b", r"\bquotes?\b",
        r"\bevidence\b(?! (button|option))", r"\bmisrepresent", r"\bmisquot", r"\bbeleg", r"\bwiderspr")),
    (("metadata",), (
        r"\bmetadata\b(?!.*\bmean)", r"\bdois?\b(?!.*\b(mean|stand for)\b)", r"\btypos?\b",
        r"\bwrong (years?|titles?|authors?|journals?|details?|dois?)\b", r"\bmismatch",
        r"\berrors? in (the )?(reference|bibliograph|citation) list", r"\bformat(ting)?\b", r"\bretract",
        r"\b(?:errors?|mistakes?)\b[^.?!]{0,25}\bin (?:the |my |its )?(?:reference|bibliograph\w*|citation) list",
        r"\bfake\b", r"\bexists?\b", r"\bfabricat", r"\bdifferent years?\b", r"\byear(s)? (differ|conflict|wrong)",
        r"\bmissing (dois?|years?|titles?|authors?|details?)\b", r"\bfalsche[sn]? (jahr|titel|doi)", r"\btippfehler\b")),
    (("unresolved",), (
        r"\bnot found\b", r"\bcould(n'?t| not) (be )?(find|found|identify|match)\b", r"\bunresolved\b",
        r"\bunknown (references?|sources?)\b", r"\bunverified\b", r"\bnicht gefunden\b")),
    (("technical",), (
        # "technical" alone is also a kind of report ("the internal technical report").
        r"\btechnical (?:problems?|issues?|errors?|failures?|reasons?|gaps?|notes?|items?|side|faults?)\b",
        r"\b(?:marked|labell?ed|tagged|says?) (?:as )?[\"“']?technical\b", r"\bwhat does [\"“']?technical\b",
        r"\b(could|did)(n'?t| not) (finish|complete|run)\b", r"\bfail(s|ed|ing|ure)?\b(?! to (support|back|cite|mention))",
        r"\b(technical|system|server|provider|processing|database) (errors?|problems?|issues?|failures?)\b",
        r"\berror (messages?|codes?)\b", r"\brepairs?\b", r"\bnot checked\b",
        r"\b(?:which|what) (?:findings|items|notes|ones|checks) (?:are|were) technical\b", r"\bthe technical ones\b",
        r"^\W*technical\W*$",
        r"\bchecks? (?:did|do|could|would)(?:n'?t| not) (?:finish|complete|run)\b",
        r"\bwhy (?:did|do|does|would|could) (?:some |the |these |any |a few )?checks? (?:not|fail|never)\b",
        r"\bre-?run\b", r"\brun (it |the check )?again\b", r"\bprovider unavailable\b", r"\btimed? ?out\b",
        r"\btechnisch", r"\bfehlgeschlagen\b")),
    (("stats",), (
        r"\bhow many\b", r"\bmost (often |frequently )?cited\b", r"\bcited (the )?most\b",
        r"\b(oldest|newest|most recent|latest)\b", r"\bhow old\b", r"\bage of (the )?(references|sources)\b",
        r"\boutdated\b", r"\bold (references|sources)\b", r"\bnever cited\b", r"\buncited\b",
        r"\bwie viele\b", r"\bam häufigsten\b", r"\bälteste")),
]

REPORT_PATTERNS = (
    r"\bpdf\b", r"^\W*(?:a |the |my )?(?:full |plain |simple |short )?report(?: please)?\W*$",
    r"\b(?:want|need|like|get) (?:a|my|me a) (?:\w+ )?(?:report|write-?up)\b(?!,? (?:says|said|shows))",
    r"\b(?:want|need|like|get) (?:the|your) (?:report|write-?up)\b(?! (?:in (?!german\b|english\b|deutsch\b|pdf\b|"
    r"plain\b|simple\b|easy\b)|from|cited|listed|on|about))",
    r"\b(?:can|could|will|would) you (?:make|do|give|produce|create|prepare|generate|write) (?:me |us )?(?:a|any|the|my) "
    r"(?:pdf |plain |full |simple |short )?report\b",
    r"\bdo you have (?:a|any|my|the) (?:pdf |plain |full |simple |short )?report\b(?! (?:in|on|about|by|from|of|for|"
    r"cited|listed|mentioned))",
    r"\bwhere (?:can|do|could|would) i (?:find|get|see|download|access) (?:a|my|the|this) (?:pdf |plain |full |simple )?"
    r"report\b(?! (?:cited|listed|in|on|by|from|of|about|for|that|which|mentioned))",
    r"\b(?:is|are) (?:a|the|there a|there any|any) (?:pdf |plain |full |simple )?reports? available\b"
    r"(?! (?:in|for|on|about|from)\b(?! (?:download|me|us)\b))",
    r"\bwrite[- ]?up\b",
    r"\b(?:in|into|as) (?:a|one|the form of a) (?:pdf |short |full |simple |plain )?report\b",
    r"\b(?:a|the|my) report (?:that )?(?:i|we) (?:can|could)(?!n'?t| not) (?:download|print|send|share|keep|save|give)\b",
    r"\bdo (?:a|me a|us a) (?:\w+ )?report\b",
    r"\b(?:is there|where(?:'s| is)|can i (?:get|have|download)) (?:a|my|the) (?:pdf |plain |full |simple |"
    r"plain[- ]language |short )?report\b"
    r"(?! (?:in|among|cited|listed|from|on|about|of))(?:\s*(?:i can|to|for|please|\?|$))",
    r"\ba (?:\w+ )?report would\b",
    r"\b(write|draft|make|create|generate|prepare|produce|build|give|send|export|download|print)\b[^.?!]{0,50}"
    r"\b(report|pdf|document|file|handout|write-?up)\b",
    r"\bsomething (i can )?(print|send|download|share)\b",
    r"\b(to|for) (my|the|our) (supervisor|professor|editor|boss|co-?authors?|advisor|tutor)\b",
    r"\bwrite (it|this|that|everything|them|the results) up\b",
    r"\bbericht\b", r"\bdokument erstell", r"\bherunterladen\b",
)

SIMPLE_PATTERNS = (
    r"\b(like|as if|as though) (i'?m|i am|you'?re talking to|to|for) (a |an |my )?(\d+|five|six|seven|eight|ten|"
    r"twelve|kid|child|baby|beginner|layman|lay person|grandma|grandmother|grandpa|non[- ]?expert|toddler|student)",
    r"\beli\d+\b", r"\b(\d{1,2}|five|six|seven|eight|ten)[- ]?(year|yr)s?[- ]?old\b", r"\bsimpl(e|y|er|ify|ified)\b",
    r"\bplain (english|language|words)\b", r"\blay(man|men|person|people)?'?s? terms\b", r"\blayman\b",
    r"\bnon[- ]?(technical|expert|scientist)\b", r"\beasy (words|language|to understand|way)\b", r"\beasier\b",
    r"\bdumb (it )?down\b", r"\bi('m| am) (not an? (expert|scientist|researcher|academic)|new to (this|it)|"
    r"confused|lost|a beginner|no expert)\b", r"\bdon'?t understand\b", r"\bno (technical )?background\b",
    r"\bin easy\b", r"\bnot (very )?technical\b", r"\beinfach", r"\bverständlich", r"\bwie (einem|für ein) (kind|"
    r"fünfjährig)", r"\bfür laien\b", r"\bfünfjährig",
)
BRIEF_PATTERNS = (r"\b(short|shorter|briefly|brief|quick(ly)?|in a nutshell|one sentence|tl;?dr|in short|"
                  r"keep it short)\b", r"\bkurz\b")
DETAILED_PATTERNS = (r"\b(in (more )?detail|detailed|more detail|elaborate|full list|complete list|each one|every one|"
                     r"all of them|list (them )?all|show (me )?all|everything)\b", r"\bausführlich\b", r"\balle\b")
STANDARD_PATTERNS = (r"\b(technical|in depth|for an expert|for experts|full detail|precise(ly)?|exact)\b",)

FOLLOWUPS: list[tuple[str, str]] = [
    ("yes", r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|please( do)?|go ahead|do it|sounds good|why not|ja|gerne|bitte|"
            r"klar|mach (das|es))\b[\s!.,]*(please|thanks?|thank you|do it|go ahead|bitte)?[\s!.]*$"),
    ("no", r"^\s*(no|nope|nah|not now|no thanks|no thank you|nein|nicht nötig)\b[\s!.,]*$"),
    ("why", r"^\s*(why|how come|wieso|warum|weshalb|why so|why\s+not)\b|\bwhy (is|was|did|does|are|were) "
            r"(it|that|this|they|these|those|the (first|second|third|last) one)\b|\bwhy\s*\?+\s*$"),
    ("simpler", r"\b(simpler|more simply|in simpler (words|terms)|i don'?t (get|understand) (it|that|this)|"
                r"what does (that|this) mean|easier|confusing|"
                r"too (technical|complicated|complex)|^\s*huh\b|^\s*what\?*\s*$|versteh(e)? (ich )?nicht)\b"),
    ("fix", r"\bhow (do|can|should|would) i (fix|correct|solve|resolve|handle|deal with|repair) (it|this|that|them|"
            r"these|those)\b|\bwhat (should|do|can|must) i do (about|with) (it|this|that|this one|that one)\b|"
            r"\bwas (soll|kann) ich (da|dagegen) tun\b"),
    ("more", r"^\s*(more|tell me more|go on|continue|and\s*\?|details?|elaborate|explain more|keep going|weiter|mehr)"
             r"\b|\btell me more\b|\bmore (about|details?|info)\b|\bexplain (it |that |this )?(further|more)\b"),
    ("others", r"\b(the )?(others|rest|remaining ones?|other ones)\b|\bwhat about the rest\b|\bshow (me )?(all|the rest)\b|"
               r"\b(and )?the (other|remaining) \d+\b"),
]

ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
            "fifth": 4, "5th": 4, "sixth": 5, "6th": 5, "seventh": 6, "eighth": 7, "last": -1,
            "erste": 0, "ersten": 0, "zweite": 1, "zweiten": 1, "dritte": 2, "dritten": 2, "letzte": -1}
ORDINAL_PATTERN = re.compile(
    r"\b(?:the |die |den |das )?(" + "|".join(ORDINALS) + r")(?: one| reference| item| source| entry| paper|"
    r" referenz| quelle| eintrag)?\b")

PRONOUN_PATTERN = re.compile(
    r"\b(it|this one|that one|this reference|that reference|this source|that source|this entry|that entry|"
    r"this item|that item|them|those|these|they|es|diese|dieser|diesen)\b")

REFERENCE_PATTERN = re.compile(
    r"(?:\b(?:ref(?:erence)?s?|referenz|quelle|citation|source|item|entry|number|nummer|no\.?|nr\.?)\s*#?\s*\[?"
    r"(\d{1,4})\]?)|\[(\d{1,4})\]|#(\d{1,4})\b",
    re.IGNORECASE)

GREETING = re.compile(r"^\s*(hi|hello|hey|hallo|moin|servus|good (morning|afternoon|evening)|guten (morgen|tag|abend)"
                      r"|greetings|hiya)\b", re.IGNORECASE)
THANKS = re.compile(r"\b(thanks|thank you|thx|ty|danke|cheers|great|perfect|awesome|helpful|got it|understood|"
                    r"super|toll|alles klar)\b", re.IGNORECASE)
CAPABILITIES = re.compile(r"\bwhat can (you|i) (do|ask)\b|\bwho are you\b|\bwhat are you\b|^\s*help\s*[?!.]*$|"
                          r"\bhow (do|can) i use (you|this chat)\b|\bwas kannst du\b", re.IGNORECASE)
DEFINITION_CUE = re.compile(r"\b(what(?:'s| is| are| does| do)|define|definition|meaning|stand for|means?|"
                            r"was (ist|sind|bedeutet|heißt))\b", re.IGNORECASE)

GERMAN_WORDS = {"der", "die", "das", "und", "ist", "nicht", "ich", "mir", "mich", "bitte", "was", "wie", "welche",
                "welcher", "warum", "kannst", "du", "ein", "eine", "einen", "mit", "für", "zu", "auf", "den", "dem",
                "sind", "habe", "hast", "gibt", "es", "erklär", "erkläre", "erklären", "noch", "auch", "oder",
                "wurde", "werden", "soll", "muss", "mein", "meine", "meinem", "über", "bericht", "quellen",
                "literatur", "ergebnisse", "zusammenfassung", "zitate", "zitiert", "dass", "wo", "fehlt",
                "wichtige", "wichtig", "zuerst", "prüfen", "gib", "mach", "einem", "als", "kann", "können", "sollte",
                "quelle", "literaturverzeichnis", "referenz", "referenzen", "passt", "passen", "gut", "sehr", "viele",
                "alle", "diese", "dieser", "dieses", "jahr", "titel", "autor", "autoren", "danke", "hallo", "einfach",
                "kind", "fünfjährigen", "selbstzitate", "wer", "wann", "nur", "schon", "jetzt", "hier", "dort"}
ENGLISH_WORDS = {"the", "and", "is", "not", "i", "me", "my", "please", "what", "how", "which", "why", "can", "you",
                 "a", "an", "with", "for", "to", "on", "are", "have", "has", "there", "it", "explain", "also", "or",
                 "was", "were", "should", "must", "about", "report", "sources", "results", "summary", "cited", "that",
                 "where", "do", "does", "of", "in", "this"}


# Words readers mistype often enough to matter. Topic matching runs on a copy of
# the message with these corrected; names and titles are always matched on the
# message exactly as typed, so a surname is never "corrected".
VOCABULARY = (
    "reference", "references", "citation", "citations", "relevant", "irrelevant", "relevance", "summary",
    "summarise", "summarize", "missing", "report", "explain", "explanation", "problem", "problems", "mistake",
    "mistakes", "metadata", "bibliography", "recommend", "recommended", "recommendations", "improve", "overview",
    "results", "findings", "simple", "simply", "technical", "decision", "decisions", "priority", "important",
    "authors", "journal", "sources", "literature", "suggestions", "buttons", "actions", "evidence", "support",
    "contradict", "unresolved", "database", "databases", "accurate", "reliable", "checklist", "download",
    "understand", "quality", "publishable", "letter", "irrelevant", "suspicious", "questionable", "incorrect",
    "self-citation", "citing", "cited", "explained", "everything", "something", "summarize", "difference",
    "different", "methodology", "verdict", "assessment", "confirmed", "dismissed", "escalated", "plagiarism",
    "simpler",
)
# Real words one letter away from a vocabulary word: never "corrected".
PROTECTED = {
    "sample", "samples", "single", "people", "resort", "repost", "record", "records", "recent", "repeat", "repair",
    "course", "courses", "cause", "causes", "force", "forces", "station", "nation", "mission", "notion", "action",
    "simile", "dimple", "pimple", "ripple", "temple", "simplex", "sources", "source", "rejects", "reject", "relevance",
    "elegant", "letters", "litter", "latter", "better", "setter", "summer", "authority", "author",
    "suspicion", "support", "import", "export", "deport", "report", "results", "result", "insult", "consult",
    "decisive", "decision", "precision", "revision", "division", "citing", "sitting", "biting", "exiting", "cities",
    "cited", "sited", "listed", "posted", "costed", "differences", "deference", "reverence", "inference", "evidence",
    "residence", "providence", "confined", "confirms", "dismissal", "dismiss", "database",
}


def _distance(left: str, right: str, limit: int) -> int:
    """Optimal-string-alignment edit distance, stopping once it exceeds `limit`."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous2: list[int] = []
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i] + [0] * len(right)
        for j, b in enumerate(right, 1):
            cost = 0 if a == b else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a == right[j - 2] and left[i - 2] == b:
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


SUFFIXES = {"", "s", "es", "ed", "d", "ing", "er", "ers", "ly", "e", "y", "ies", "ied", "ion", "ions", "al", "ity",
            "ment", "ments", "ness", "ive", "ation", "ations", "able"}


def _variant(word: str, candidate: str) -> bool:
    """A different form of the same word ("reported"/"report") or its negation ("unimportant").

    A word cut short by a letter or two ("referenc", "relevan") is a typo, not a variant.
    """
    if candidate.startswith(word) and len(candidate) - len(word) <= 2:
        return False
    if word in candidate or candidate in word:
        return True
    shared = 0
    while shared < min(len(word), len(candidate)) and word[shared] == candidate[shared]:
        shared += 1
    return shared >= 4 and word[shared:] in SUFFIXES and candidate[shared:] in SUFFIXES


def correct_spelling(text: str) -> str:
    """The message with common misspellings of CitePrism's vocabulary fixed."""
    known = set(VOCABULARY) | PROTECTED | ENGLISH_WORDS | GERMAN_WORDS

    def fix(match: re.Match) -> str:
        word = match.group(0)
        if len(word) < 5 or word in known or word in REAL_WORDS:
            return word
        limit = 1 if len(word) < 9 else 2
        best: Optional[tuple[int, str]] = None
        for candidate in VOCABULARY:
            if candidate[0] != word[0] or _variant(word, candidate):
                continue                      # typos keep the first letter; "resolved" is not "unresolved"
            distance = _distance(word, candidate, limit)
            if distance <= limit and (best is None or distance < best[0]):
                best = (distance, candidate)
        return best[1] if best else word

    return re.sub(r"[a-zäöüß]+", fix, text)


# Words that tie a message to the audit. A message with none of them, and no
# follow-up, greeting or thanks, is about something else entirely.
DOMAIN = re.compile(
    r"\b(refer\w*|ref|refs|cit\w*|sourc\w*|papers?|manuscripts?|chapters?|books?|articles?|thesis|results?|reports?|"
    r"pdf|check\w*|audit\w*|authors?|dois?|years?|journals?|findings?|finds?|found|items?|relevan\w*|self|missing|"
    r"buttons?|decisions?|decide\w*|scores?|quot\w*|bibliograph\w*|lists?|explain\w*|help|evidence|claims?|fix\w*|"
    r"problems?|issues?|errors?|wrong|mistakes?|summar\w*|literature|publications?|published|works?|databases?|"
    r"titles?|flag\w*|notes?|citeprism|you|explan\w*|stud(?:y|ies)|research\w*|venues?|statistic\w*|methods?|tests?|topics?|quelle\w*|zitat\w*|literatur\w*|ergebnis\w*|autor\w*|bericht\w*|"
    r"prüf\w*|hilf\w*|erklär\w*)\b")


def fold(text: str) -> str:
    """Lower-case and strip accents, for matching names typed without them."""
    return "".join(character for character in unicodedata.normalize("NFKD", str(text or "").lower())
                   if not unicodedata.combining(character))


def detect_language(text: str) -> str:
    words = re.findall(r"[a-zäöüß]+", str(text or "").lower())
    german = sum(word in GERMAN_WORDS for word in words)
    if german:                       # "Is Müller relevant?" is English with a German name
        german += 2 * bool(re.search(r"[äöüß]", str(text or "").lower()))
    english = sum(word in ENGLISH_WORDS for word in words)
    return "de" if german >= 2 and german > english else "en"


@dataclass
class Understanding:
    text: str
    intents: list[str] = field(default_factory=list)
    reference_numbers: list[str] = field(default_factory=list)
    glossary_terms: list[str] = field(default_factory=list)
    simple: bool = False
    standard: bool = False
    brief: bool = False
    detailed: bool = False
    wants_report: bool = False
    followup: Optional[str] = None
    ordinal: Optional[int] = None
    pronoun: bool = False
    greeting: bool = False
    thanks: bool = False
    capabilities: bool = False
    off_topic: bool = False
    language: str = "en"

    @property
    def has_request(self) -> bool:
        """Whether the message asks for anything by itself, without the previous answer."""
        return bool(self.intents or self.reference_numbers or self.glossary_terms or self.wants_report
                    or self.capabilities)


QUOTED_TITLE = re.compile(r"[“„\"«‘‚]\s*([^“”„\"«»‘’‚]+?\s[^“”„\"«»‘’‚]+?)[,.;:!?]?\s*[”“\"»’‘]")


def _first(patterns, text) -> Optional[int]:
    positions = [match.start() for pattern in patterns for match in [re.search(pattern, text)] if match]
    return min(positions) if positions else None


def understand(question: str) -> Understanding:
    raw = " ".join(str(question or "").split())
    text = correct_spelling(raw.lower())
    result = Understanding(text=raw, language=detect_language(raw))

    for match in REFERENCE_PATTERN.finditer(text):
        number = next(group for group in match.groups() if group)
        if number not in result.reference_numbers:
            result.reference_numbers.append(number)

    # A quoted title ("Tell me about “Fraud detection in financial statements”") names a
    # work: its words are not an accusation or a request for a report. Quoted labels from the
    # screen ("Why are references marked “possibly irrelevant”?") still name the topic.
    quoted = QUOTED_TITLE.sub(" “” ", text)
    found: list[tuple[int, str]] = []
    for intents, patterns in TOPICS:
        position = _first(patterns, quoted if intents == ("boundaries",) else text)
        if position is not None:
            found.extend((position, intent) for intent in intents)
    seen: set[str] = set()
    for _, intent in sorted(found, key=lambda item: item[0]):
        if intent not in seen:
            seen.add(intent)
            result.intents.append(intent)
    if "overview_weak" in result.intents:
        index = result.intents.index("overview_weak")
        specific = [intent for intent in result.intents if intent not in ("overview", "overview_weak")]
        # "Can I trust these results?" is about trust, not a request for the summary;
        # "I got the results … explain everything … make a report" wants the summary too.
        broad = len(specific) != 1 or re.search(r"\beverything\b|\ball\b", text) or \
            any(re.search(pattern, text) for pattern in REPORT_PATTERNS)
        if "overview" in result.intents or not broad:
            result.intents.pop(index)
        else:
            result.intents[index] = "overview"

    if DEFINITION_CUE.search(text):
        result.glossary_terms = [term for term, pattern in GLOSSARY_PATTERNS.items() if re.search(pattern, text)]
        if result.glossary_terms:
            # "What is a DOI?" asks for a definition, not for the DOI findings.
            definitional = {"doi": "metadata", "metadata": "metadata", "self_citation": "self_citation",
                            "relevance": "irrelevant", "retraction": "metadata", "cocitation": "missing"}
            for term in result.glossary_terms:
                topic = definitional.get(term)
                if topic in result.intents and not re.search(r"\b(any|which|my|this paper|the paper|wrong|are there)\b",
                                                             text):
                    result.intents.remove(topic)

    # A quoted title with "report" in it ("“Annual report 2020”") is not a request; a quoted
    # label is ("Can I get the “plain language” report?").
    titled = QUOTED_TITLE.sub(lambda match: " “” " if "report" in match.group(0) or "bericht" in match.group(0)
                              else match.group(1), text)
    result.wants_report = any(re.search(pattern, titled) for pattern in REPORT_PATTERNS)
    result.simple = any(re.search(pattern, text) for pattern in SIMPLE_PATTERNS)
    result.standard = not result.simple and any(re.search(pattern, text) for pattern in STANDARD_PATTERNS)
    result.brief = any(re.search(pattern, text) for pattern in BRIEF_PATTERNS)
    result.detailed = any(re.search(pattern, text) for pattern in DETAILED_PATTERNS)
    result.greeting = bool(GREETING.search(text))
    result.thanks = bool(THANKS.search(text)) and len(text.split()) <= 8
    result.capabilities = bool(CAPABILITIES.search(text))

    if "simpler" in [kind for kind, pattern in FOLLOWUPS if re.search(pattern, text)]:
        result.simple = True
    # "Explain it simply" is a register, not a topic of its own: drop the
    # overview it triggers when the message is only about the register.
    for kind, pattern in FOLLOWUPS:
        if re.search(pattern, text):
            result.followup = kind
            break
    ordinal = ORDINAL_PATTERN.search(text)
    if ordinal and not re.search(r"\b(check|look at|start with|do|fix|read) (it |this |that )?first\b|\bfirst (thing|step)",
                                 text):
        result.ordinal = ORDINALS[ordinal.group(1)]
    result.pronoun = bool(PRONOUN_PATTERN.search(text))
    result.off_topic = not (result.has_request or result.followup or result.greeting or result.thanks
                            or result.simple or result.brief or result.detailed or result.ordinal is not None
                            or DOMAIN.search(text))
    return result


def classify_question(question: str) -> tuple[str, Optional[str]]:
    """(first intent, reference number or None) — the single-topic view used by older callers."""
    reading = understand(question)
    if reading.reference_numbers:
        return "reference", reading.reference_numbers[0]
    if reading.intents:
        intent = reading.intents[0]
        return ("next_steps" if intent == "improve" else intent), None
    if reading.greeting:
        return "overview", None
    return "unknown", None


__all__ = ["Understanding", "classify_question", "correct_spelling", "detect_language", "fold", "understand"]
