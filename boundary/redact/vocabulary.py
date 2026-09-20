"""The default allow vocabulary for the policy's second pass: capitalised or number-bearing
terms that carry decision-relevant meaning and identify nobody.

The second pass masks anything proper-noun-shaped or identifier-shaped that no recogniser
claimed, unless it is on this list. The list is therefore the only thing standing between
"fail closed" and "unreadable", and every entry should be a word a reviewer would want to
see intact in a request about an access-to-information record: function words that start
sentences, calendar words, the words of the legislation and the workflow, and the names
of the jurisdiction itself. A project extends it with `Policy(..., allow=...)`; nothing
here is a person, a street or a file number.

Comparison is case-insensitive.
"""

from __future__ import annotations

_FUNCTION_WORDS = """
a an the this that these those it its is are was were be been being am
i you he she we they me him her us them my your his our their mine yours ours
and or but nor so yet if then than as of at by for from in into on onto to with
without within about above below under over between among through during before
after until while because although though unless whether where when why how what
which who whom whose here there now also only not no yes any all each every some
such very more most much many few less least own same other another both either
neither per via re cc bcc fw fwd
please thank thanks regards sincerely dear hello hi good morning afternoon evening
will would shall should can could may might must do does did done have has had
having get got make made take took see saw say said know knew think thought
one two three four five six seven eight nine ten eleven twelve twenty thirty hundred
thousand first second third fourth fifth last next previous
mr mrs ms mx miss dr prof hon sir madam rev sgt cst
""".split()  # noqa: SIM905

_CALENDAR = """
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec
monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun
am pm est nst ast adt ndt utc
""".split()  # noqa: SIM905

_WORKFLOW = """
access information protection privacy act atippa atipp foippa pipeda freedom
request requester applicant response record records document documents page pages
section subsection clause schedule part paragraph exemption exempt exception
severed sever severing severance redacted redaction redact released release withheld
withhold disclosed disclosure disclose decision decided approved approval denied denial
review reviewed reviewer analyst coordinator commissioner office ombudsman
department departmental division branch unit government provincial province federal
minister ministry deputy assistant director executive manager officer staff employee
memo memorandum email letter note notes meeting minutes agenda attachment attached
subject date time re draft final version copy original summary report analysis
briefing brief background recommendation recommendations conclusion policy procedure
public personal sensitive internal confidential third party parties
newfoundland labrador nl canada canadian
pdf docx xlsx csv txt html url http https www
covid covid-19 sars-cov-2 q1 q2 q3 q4 fy
name names id number no code contact address phone fax tel cell mobile postal
file case claim matter docket ticket reference ref refs mcp sin dob birth title position
signature signed attention attn from sent received forwarded
""".split()  # noqa: SIM905

DECISION_VOCABULARY: frozenset[str] = frozenset(
    w.casefold() for w in (*_FUNCTION_WORDS, *_CALENDAR, *_WORKFLOW)
)

__all__ = ["DECISION_VOCABULARY"]
