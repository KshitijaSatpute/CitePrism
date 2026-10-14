"""Everyday wording for the assistant and the plain-language report.

Every finding category has three things in two registers:

* a short label, what it means, and what to do about it — in ``PLAIN``
  (standard) and ``SIMPLE`` (for someone with no background at all);
* the concrete steps that resolve it — in ``FIX_STEPS``.

The texts follow the audit policy: a finding is something worth checking,
never a verdict of error or misconduct. Keep them short, concrete and
consistent with the review workspace's buttons and tab names.
"""

from __future__ import annotations

PLAIN = {
    "potentially_irrelevant": (
        "May not fit the sentence citing it",
        "The cited work and the sentence citing it seem to be about different things.",
        "Read the citing sentence next to the source and decide whether the citation belongs there."),
    "low_topical_similarity": (
        "Different topic from the paper",
        "Its subject is far from the paper's. That is common for tools, data sets and methods, "
        "so it is only worth a look if the citing sentence also seems off.",
        "No action unless the citing sentence also seems unrelated."),
    "possible_contradiction": (
        "May contradict the claim",
        "The checker found text in the source that seems to say the opposite of the sentence citing it.",
        "Read the claim and the quoted source text side by side and confirm what the source says."),
    "limited_support": (
        "Does not clearly back up the claim",
        "The part of the source we could read (usually the abstract) does not show the claim. The full paper might.",
        "Check the full paper before concluding anything."),
    "metadata_difference": (
        "Details differ from the published record",
        "Something in the reference list — title, authors, year, journal or DOI — differs from the official "
        "record of the work.",
        "Compare the entry with the publication and correct the reference list if needed."),
    "identity_unresolved": (
        "Not found in scholarly databases",
        "We could not match this entry to a published work, so it could not be checked further.",
        "Look the work up yourself; check the entry for typos, or whether it is a book, thesis or website."),
    "database_unavailable": (
        "Not checked: a database did not respond",
        "A scholarly database was busy or unreachable while this reference was looked up, so it was not "
        "checked. This is a technical problem, not a problem with the reference.",
        "Run the audit again later."),
    "printed_doi_conflict": (
        "DOI leads to a different work",
        "The DOI printed in the reference list exists, but it belongs to a work with a different title or authors.",
        "Look the work up at the publisher and correct the DOI or the reference."),
    "retraction_reported": (
        "Reported as retracted",
        "A scholarly database lists this work as retracted.",
        "Read the retraction notice and decide whether the claim still stands."),
    "questionable_self_citation": (
        "Self-citation worth checking",
        "It shares an author with this paper and is either never cited in the text or may not fit its claim. "
        "Citing your own work is normal; this asks whether this particular one is needed.",
        "Confirm the citation is needed; otherwise suggest removing it."),
    "not_cited": (
        "Listed but never cited in the text",
        "It appears in the reference list, but we found no place in the text that cites it.",
        "Check whether the text cites it in a form CitePrism missed; otherwise it may be surplus."),
    "bibliography_review": (
        "Reference entry could not be read cleanly",
        "The entry could not be read cleanly from the PDF.",
        "Compare the entry with the PDF page."),
    "split_entry": (
        "One reference split into two entries",
        "Two numbered entries look like one reference broken in two — automatic numbering does this when an entry "
        "contains a line break.",
        "Merge the two entries, then check the reference numbers cited after them."),
    "assessment_error": (
        "Check did not finish (technical problem)",
        "The automatic check did not complete. This is a system issue, not a problem with the paper.",
        "Run the audit again."),
    "source_unavailable": (
        "No readable text available",
        "We found the work but could not get its abstract or text, so its support was not checked.",
        "Read the source yourself if this citation matters."),
    "doi_omitted_in_manuscript": (
        "No DOI printed",
        "Formatting only: the entry does not print a DOI.",
        "Optional: add the DOI when updating the reference list."),
    "identifier_unavailable": (
        "No DOI on record",
        "The databases have no DOI for this work. Many older works have none.",
        "No action needed."),
    "author_list_shorter": (
        "Database lists fewer authors",
        "The database record leaves out some of the printed authors, but every author it lists is printed. "
        "The record is most likely incomplete, not the entry.",
        "No action needed."),
    "citation_year_mismatch": (
        "Cited with a different year than listed",
        "The text cites this work with one year, but the reference list gives another.",
        "Check the publication and correct the year in the text or in the reference list."),
    "legacy_unverified": (
        "From an older audit",
        "This result predates the current checks.",
        "Run the audit again."),
}

# The same, for a reader with no background: short sentences, everyday words,
# one picture per idea. Accuracy is not traded for simplicity.
SIMPLE = {
    "potentially_irrelevant": (
        "Might be the wrong source",
        "The sentence says one thing, but the source seems to talk about something else — like pointing to a "
        "cookbook to prove a fact about cars.",
        "Read the sentence and the source together. If they don't match, use a better source or remove it."),
    "low_topical_similarity": (
        "About a different subject",
        "This source is about a different subject from the paper. That is often fine — for example when it "
        "describes a tool or method the paper used.",
        "Nothing to do, unless the sentence citing it also looks odd."),
    "possible_contradiction": (
        "The source may say the opposite",
        "The paper says something, and the source seems to say the opposite. That's like saying "
        "'my friend agrees with me' when your friend actually disagrees.",
        "Read the sentence and the quoted source text side by side and see what the source really says."),
    "limited_support": (
        "The source doesn't clearly say it",
        "We could only read a short summary of the source, and the summary doesn't say what the paper "
        "claims. The full source might.",
        "Look in the full source. If you find the point there, all is fine."),
    "metadata_difference": (
        "Some details are written differently",
        "The reference list writes something about this source differently from the official record — "
        "for example the year, the title or the authors. Like an address with a wrong house number.",
        "Look the source up and copy the correct details into the reference list."),
    "identity_unresolved": (
        "Couldn't find this source",
        "We searched the big library catalogues for this source and couldn't find it, so we couldn't check it. "
        "This happens a lot with books, websites and reports — the source most likely exists.",
        "Search for it yourself and check the spelling of its title and authors."),
    "database_unavailable": (
        "Not checked yet — a library catalogue didn't answer",
        "One of the online catalogues we ask was busy, so this source wasn't checked. That's our problem, "
        "not the paper's.",
        "Run the check again a bit later."),
    "printed_doi_conflict": (
        "The link code points to a different source",
        "Every published work has a code called a DOI — like a tracking number. The code written here "
        "leads to a different work.",
        "Find the correct DOI for this source and replace it."),
    "retraction_reported": (
        "This source was taken back",
        "The people who published this source later withdrew it, usually because something in it was wrong.",
        "Check whether the paper's point still holds without it, and find another source."),
    "questionable_self_citation": (
        "Authors citing their own work — worth checking",
        "One of the paper's authors also wrote this source. That's normal, but here the source is either "
        "never mentioned in the text or doesn't seem to fit.",
        "Ask: does this sentence really need this source? If not, remove it."),
    "not_cited": (
        "Listed but never used",
        "The source is in the reference list, but the text never mentions it — like inviting someone to a "
        "party they never show up at.",
        "Mention it where it belongs, or remove it from the list."),
    "bibliography_review": (
        "Couldn't read this entry properly",
        "The entry in the reference list was hard to read from the PDF, so some parts may be missing.",
        "Look at the entry in the PDF and check it is complete."),
    "split_entry": (
        "One source broken into two",
        "One source in the list got cut in half, so it now has two numbers — and the numbers after it moved up by one.",
        "Join the two halves back together and check the numbers in the text."),
    "assessment_error": (
        "This check didn't finish",
        "Something went wrong on our side while checking this source. It says nothing about the paper.",
        "Run the check again."),
    "source_unavailable": (
        "We found the source but couldn't read it",
        "We found the source, but we couldn't get its text, so we couldn't compare it with the paper.",
        "If this source matters, read it yourself."),
    "doi_omitted_in_manuscript": (
        "No DOI written down",
        "The entry doesn't include its DOI (its tracking number). That's only a formatting detail.",
        "Optional: add the DOI."),
    "identifier_unavailable": (
        "No DOI exists for it",
        "This source has no DOI (tracking number). Many older works don't.",
        "Nothing to do."),
    "author_list_shorter": (
        "The library catalogue forgot some authors",
        "The catalogue lists fewer authors than the paper does, but everyone it lists is in the paper. "
        "The catalogue is probably just incomplete.",
        "Nothing to do."),
    "citation_year_mismatch": (
        "Two different years for the same source",
        "The text says the source is from one year, but the reference list says another year. "
        "Only one can be right.",
        "Look up the correct year and make both places say the same."),
    "legacy_unverified": (
        "From an older check",
        "This result comes from an older version of CitePrism.",
        "Run the check again."),
}

# What actually resolves each kind of finding, in the order a person would do it.
FIX_STEPS = {
    "citation_year_mismatch": [
        "Open the work's DOI link or the publisher's page and note its publication year.",
        "Make the year in the text and in the reference list the same.",
        "If there are two editions, cite the one you used and add the edition to the entry.",
    ],
    "metadata_difference": [
        "Open the DOI link or the publisher's page for the work.",
        "Compare the title, authors, year, journal and DOI with the reference entry.",
        "Correct what is wrong. Small differences such as an abbreviated journal name can stay.",
    ],
    "metadata_difference_high": [
        "Search the exact title in Google Scholar, the library catalogue or on the publisher's site.",
        "If the work exists, copy its correct details (especially the DOI) into the reference list.",
        "If you cannot find the work anywhere, remove the reference or replace it with one you can verify.",
    ],
    "printed_doi_conflict": [
        "Paste the printed DOI into https://doi.org and see which work it opens.",
        "Find the correct DOI for the work that was meant (publisher's page or search.crossref.org).",
        "Replace the DOI in the reference list.",
    ],
    "potentially_irrelevant": [
        "Read the sentence that cites this work (Review in detail shows it with the page number).",
        "Read the source's summary beside it.",
        "If the source does not address that point, cite one that does, or remove the citation.",
        "If it is cited for a method, tool or data set, that is fine: mark the item Dismissed and say so.",
    ],
    "possible_contradiction": [
        "Read the quoted source passage beside the claim.",
        "If the source really says the opposite, rewrite the claim or present the source as a contrasting view.",
        "If the checker misread it, mark the item Dismissed and note why.",
    ],
    "limited_support": [
        "Open the full text of the source.",
        "Look for the passage that supports the claim. If you find it, mark the item Dismissed.",
        "If it is not there, soften the claim or cite a source that states it.",
    ],
    "questionable_self_citation": [
        "Ask whether the sentence needs this particular work.",
        "If it does, keep it and mark the item Dismissed with one sentence of reason.",
        "If it does not, remove it. Citing one's own work is normal only where it adds something.",
    ],
    "not_cited": [
        "Search the text for the author's surname and the year.",
        "If the work is cited in a form CitePrism missed, mark the item Dismissed.",
        "If it is really never cited, cite it where it belongs or remove it from the reference list.",
    ],
    "identity_unresolved": [
        "Check the entry for typos in the title and the author names.",
        "Search the title in Google Scholar or the library catalogue.",
        "Books, websites and reports are often missing from scholarly databases. If the entry is complete "
        "and correct, it can stay.",
    ],
    "retraction_reported": [
        "Open the retraction notice from the publisher.",
        "Decide whether the claim still stands without this source.",
        "Replace the source, or say in the text that it was retracted.",
    ],
    "bibliography_review": [
        "Compare the entry with the reference list in the PDF.",
        "Fill in what is missing (authors, year, title, publisher or journal).",
    ],
    "split_entry": [
        "In the document, join the two entries into one (remove the line break inside the entry).",
        "Let the numbering update, then read every in-text number from that point on: a number that pointed to "
        "an entry before the merge may now point to the one before it.",
        "Check the merged entry is complete: all authors, year, title, journal and DOI.",
    ],
    "technical": [
        "Run the check again later: Review in detail → Run the check again, or More → Repairs.",
        "Nothing in the paper needs to change because of this item.",
    ],
    "missing_literature": [
        "Read the title and summary of each suggested work.",
        "Add it only if it supports a point the paper actually makes.",
        "Do not add a work just because it is popular. This list is a prompt to think, not a rule.",
    ],
}

SIMPLE_FIX_STEPS = {
    "citation_year_mismatch": [
        "Look up the source online and find the year it came out.",
        "Write that same year in both places: in the text and in the reference list.",
    ],
    "metadata_difference": [
        "Look up the source online (the DOI link is the quickest way).",
        "Copy its correct title, authors and year into the reference list.",
    ],
    "metadata_difference_high": [
        "Search for the exact title online.",
        "If you find it, copy its correct details. If you can't find it anywhere, remove it or use another source.",
    ],
    "printed_doi_conflict": [
        "Type the DOI into doi.org and see what opens.",
        "Find the right DOI on the publisher's page and replace it.",
    ],
    "potentially_irrelevant": [
        "Read the sentence and the source's summary side by side.",
        "If they don't match, find a better source or remove this one.",
    ],
    "possible_contradiction": [
        "Read what the source says next to what the paper says.",
        "If they really disagree, change the sentence or mention that the source disagrees.",
    ],
    "limited_support": [
        "Open the full source and look for the point.",
        "If it isn't there, change the sentence or use a source that says it.",
    ],
    "questionable_self_citation": [
        "Ask: does this sentence really need this source?",
        "If not, remove it.",
    ],
    "not_cited": [
        "Search the text for the author's name.",
        "If the source is never mentioned, mention it where it belongs or remove it from the list.",
    ],
    "identity_unresolved": [
        "Check the spelling of the title and authors.",
        "Search for it yourself. Books and websites are often not in the catalogues — that's okay.",
    ],
    "retraction_reported": [
        "Find out why it was taken back.",
        "Use another source for that point.",
    ],
    "bibliography_review": [
        "Look at the entry in the PDF.",
        "Fill in anything that's missing.",
    ],
    "split_entry": [
        "Join the two halves into one entry.",
        "Check the numbers in the text after that point — they may need to move down by one.",
    ],
    "technical": [
        "Run the check again later.",
        "Nothing in the paper needs to change for this.",
    ],
    "missing_literature": [
        "Read what each suggested work is about.",
        "Add it only if it really helps the paper make its point.",
    ],
}

TECHNICAL_CATEGORIES = {"assessment_error", "database_unavailable", "source_unavailable", "legacy_unverified"}

GLOSSARY = {
    "doi": ("DOI",
            "A Digital Object Identifier: a permanent code such as 10.1000/xyz123 that identifies one published "
            "work. Typing it after https://doi.org/ opens the work.",
            "A DOI is like a tracking number for a published work. Put it after doi.org/ and it takes you "
            "straight to the work."),
    "citation": ("Citation",
                 "A mention in the text, such as (Smith, 2020) or [12], that points to an entry in the reference list.",
                 "A citation is a little note in the text saying 'I got this idea from here'."),
    "reference": ("Reference list",
                  "The list of works at the end of the paper that the text cites.",
                  "The reference list is the list at the end of the paper of all the places the ideas came from."),
    "metadata": ("Metadata",
                 "The descriptive details of a work: title, authors, year, journal or publisher, DOI.",
                 "Metadata is the label on a book: its title, who wrote it and when."),
    "self_citation": ("Self-citation",
                      "Citing a work that one of the paper's own authors wrote. Normal in moderation; a high share "
                      "or citations that do not fit the claim are worth a look.",
                      "Self-citation is when authors mention their own earlier work. A little is normal; too much "
                      "can look like showing off."),
    "relevance": ("Relevance score",
                  "A number from 0 to 100 for how well a cited work fits the sentence citing it: 70 or more is "
                  "relevant, 40–69 borderline, under 40 possibly not relevant. It combines an AI model's reading "
                  "(60%) with a text-similarity measure (40%).",
                  "The relevance score says how well a source fits the sentence that uses it, from 0 (not at all) "
                  "to 100 (perfectly)."),
    "abstract": ("Abstract",
                 "The short summary at the start of a published work. Most checks read the abstract because the "
                 "full text is often behind a paywall.",
                 "The abstract is a short summary of a paper, like the blurb on the back of a book."),
    "database": ("Scholarly database",
                 "Online catalogues of published research — CitePrism uses OpenAlex, Crossref and Semantic "
                 "Scholar, plus dblp, Europe PMC and DataCite for harder cases.",
                 "Scholarly databases are giant online library catalogues of research."),
    "resolved": ("Matched / resolved",
                 "A reference is matched (resolved) when CitePrism found the published work it describes in a "
                 "scholarly database with enough certainty.",
                 "Matched means we found the real source in the library catalogue."),
    "cocitation": ("Possibly missing work (co-citation)",
                   "A work that several of the paper's own references cite, but the paper itself does not. It may be "
                   "important background — or reasonably left out.",
                   "If many of the paper's sources all mention the same other work, that work might be important "
                   "and worth a look."),
    "retraction": ("Retraction",
                   "A publisher's formal withdrawal of a work, usually because of serious errors.",
                   "A retraction is when a published work is taken back because something in it was wrong."),
    "orcid": ("ORCID iD",
              "A unique number for a researcher, such as 0000-0002-1825-0097. With it, self-citations are matched "
              "exactly instead of by name.",
              "An ORCID iD is a personal number for a researcher, so two people with the same name aren't mixed up."),
    "grounded": ("Quoted evidence",
                 "When the AI model judges a citation, it must quote the exact source text it relied on. CitePrism "
                 "checks the quote word for word, so the model cannot invent evidence.",
                 "The AI has to copy the exact words from the source that it used, and we check those words are "
                 "really there."),
}

GLOSSARY_PATTERNS = {
    "doi": r"\bdois?\b|digital object identifier",
    "citation": r"\bwhat(?:'s| is| are) (?:a |an |the )?(?:in-?text )?citations?\b",
    "reference": r"\bwhat(?:'s| is| are) (?:a |an |the )?(?:reference list|bibliography|references)\b",
    "metadata": r"\bmeta-?data\b",
    "self_citation": r"\bwhat(?:'s| is| are) (?:a |an |the )?self[- ]?citations?\b",
    "relevance": r"\b(?:relevance )?score\b.*\bmean|\bwhat(?:'s| is) (?:a |an |the )?relevance(?: score)?\b",
    "abstract": r"\bwhat(?:'s| is| are) (?:a |an |the )?abstracts?\b",
    "database": r"\b(?:scholarly |which |what )databases?\b|\bopenalex\b|\bcrossref\b|\bsemantic scholar\b",
    "resolved": r"\bwhat (?:does|do) (?:\"|“)?(?:resolved|matched|unresolved)\b",
    "cocitation": r"\bco-?citation",
    "retraction": r"\bwhat(?:'s| is| are) (?:a |an |the )?retract",
    "orcid": r"\borcid\b",
    "grounded": r"\bgrounded\b|\bquoted evidence\b",
}

# How the review's four decision buttons are meant to be used.
ACTIONS_GUIDE = {
    "standard": {
        "text": ("Each item to check comes with the same four choices. Open the item, look at the evidence "
                 "CitePrism shows, then pick the one that fits and write one sentence on why. That sentence is "
                 "saved with your name, so anyone can later see how each item was settled."),
        "points": [
            "Confirmed — you checked and the concern is real (for example, the year really is wrong). Then fix "
            "it in the paper, or use “Suggest a correction to this entry” so the author sees the exact change.",
            "Dismissed — you checked and it is fine (for example, a source on a different topic is cited for a "
            "method it describes).",
            "Needs more evidence — you cannot decide from what is shown, usually because only the abstract was "
            "available and you need the full paper.",
            "Escalated — someone with more subject knowledge (an editor or specialist) should decide.",
            "Where: Review in detail → Items to check → click an item → “Your decision”. Notes marked "
            "“for information” need no decision.",
            "Items marked “Technical” are problems on CitePrism's side. Use Run the check again (or More → "
            "Repairs) instead of deciding them.",
        ],
    },
    "simple": {
        "text": ("Think of each item as a sticky note CitePrism put on a reference saying “please look here”. "
                 "After you look, you tell CitePrism what you found by pressing one of four buttons, and you "
                 "write one short sentence saying why."),
        "points": [
            "Confirmed = “Yes, there's a real problem here.” Then fix it.",
            "Dismissed = “I looked — it's fine.”",
            "Needs more evidence = “I can't tell yet, I need to read more.”",
            "Escalated = “Someone who knows more should decide.”",
            "You find the buttons in Review in detail → Items to check → click an item.",
            "Notes marked “Technical” are our mistakes, not the paper's — just run the check again later.",
        ],
    },
}

HOW_IT_WORKS = {
    "standard": [
        "CitePrism reads the paper's reference list and finds every place in the text where each work is cited.",
        "It looks up each work in scholarly databases (OpenAlex, Crossref, Semantic Scholar and others) and "
        "compares the printed details — title, authors, year, DOI — with the official record.",
        "For each citing sentence, an AI model reads the source's abstract (or open full text) and must quote "
        "the exact passage it relies on; quotes are checked word for word. That reading is combined with a "
        "text-similarity measure into a relevance score from 0 to 100.",
        "It also looks for self-citation by ORCID iD or name, and for well-known works the paper's own "
        "references cite but the paper does not.",
        "Everything it raises is a pointer for a person to check, not a verdict.",
    ],
    "simple": [
        "First, CitePrism reads the list of sources at the end of the paper.",
        "Then it looks up every source in big online library catalogues, to check the source is real and its "
        "details are written correctly.",
        "Then a computer reader compares each sentence with the source it points to, to see if the source "
        "really talks about that. It has to copy the exact words it relied on, and we check them.",
        "Finally, it looks for authors citing themselves a lot, and for important works the paper might have "
        "forgotten.",
        "It never decides anything on its own. It only points: “a person should look here”.",
    ],
}


def plain(category: str, *, simple: bool = False, system: bool = False) -> tuple[str, str, str]:
    """Label, meaning and action for a category in the requested register."""
    if category == "identity_unresolved" and system:
        category = "database_unavailable"
    table = SIMPLE if simple else PLAIN
    return table.get(category) or PLAIN.get(category) or (category.replace("_", " ").capitalize(), "", "")


def fix_steps(category: str, *, priority: str = "moderate", simple: bool = False,
              system: bool = False) -> list[str]:
    """The concrete steps that resolve a finding of this category."""
    table = SIMPLE_FIX_STEPS if simple else FIX_STEPS
    if system or category in TECHNICAL_CATEGORIES:
        return list(table["technical"])
    if category == "metadata_difference" and priority == "high":
        return list(table["metadata_difference_high"])
    return list(table.get(category) or FIX_STEPS.get(category) or [])


__all__ = ["ACTIONS_GUIDE", "FIX_STEPS", "GLOSSARY", "GLOSSARY_PATTERNS", "HOW_IT_WORKS", "PLAIN", "SIMPLE",
           "SIMPLE_FIX_STEPS", "TECHNICAL_CATEGORIES", "fix_steps", "plain"]
