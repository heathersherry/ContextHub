# HyDRA-Bench: original-meaning review of the same 20 answers

The earlier **13/20 (65%) is core reference agreement, not a reliable estimate
of correctness under the original questions**. Some non-matching answers use
reasonable measures; some matching answers inherit unsupported identity or
geographic assumptions from the reference. This review keeps both observations
and does not manufacture a single improved accuracy.

This is a post-hoc coding-assistant review, not the official judge or an
independent human panel. The same 20 frozen Qwen thinking-enabled attempts are
reviewed. No agent was rerun, no question or source was repaired, and no answer
was replaced. Facts are evaluated in the historical database/corpus snapshot,
not as claims about current officeholders or current populations.

## Mutually exclusive answer outcomes

| Outcome | Count | Cases |
| --- | ---: | --- |
| Core answer supported under an ordinary reading | 9 | dev_43, dev_5, dev_72, dev_10, dev_68, dev_26, dev_31, dev_60, dev_66 |
| Defensible alternative measure, supported by data and text | 3 | dev_62, dev_13, dev_46 |
| Supported only with a remaining evidence/geography condition | 2 | dev_6, dev_42 |
| Central premise or cross-source identity not established | 4 | dev_32, dev_55, dev_0, dev_11 |
| Demonstrable answer error, independently of the reference | 1 | dev_20 |
| No final answer | 1 | dev_35 |
| Total | 20 | All selected cases retained |

The first two rows provide **12 answers supported under an explicit reasonable
reading**, including three cases whose selected measure differs from the
reference. This is not 12 unconditionally correct answers. The two conditional
cases have weaker evidence than those three alternatives. The four unresolved
cases are not certified correct and are not asserted to be factually false.
No accuracy is calculated by silently discarding difficult/ambiguous cases.

| Type | Supported | Alternative measure | Conditional | Unresolved premise | Demonstrable error | No answer |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A2S | 5 | 2 | 0 | 2 | 0 | 1 |
| S2A | 4 | 1 | 2 | 2 | 1 | 0 |

Judgment concerns the requested core answer, not exact reference wording or
optional extra facts. A fact expressly supplied in the question can be accepted
as a premise; a role/name/type match that silently changes the entity is not
treated as independent proof of identity. This distinction is particularly
important for dev_43 versus dev_11/dev_55. The latter identity concerns remain
reviewable qualifications, not findings that the people are definitely different.

## Problem attribution: overlapping counts

These rows describe different axes and **must not be added together**.

| Source of difficulty | Count | Cases / interpretation |
| --- | ---: | --- |
| Material question/reference qualification | 11 | dev_62, dev_13, dev_35, dev_6, dev_20, dev_32, dev_46, dev_42, dev_55, dev_0, dev_11 |
| Demonstrably misleading points descriptions | 2 | dev_62 and dev_13: cumulative standings described as points acquired in each race |
| Location definitions/normalization insufficient for the question | 3 | dev_6, dev_20: free-text Location; dev_42: district/branch linkage cannot establish town residence |
| Required reference table still missing after all searches | 0 | All 42 case-table occurrences retrieved across all 20 cases |
| Missing direct external fact despite text searches | 1 | dev_6: Indian Half-bred country evidence; no matching name found anywhere in the frozen passage fixture |
| Demonstrable SQL filter error | 1 | dev_20: knowingly counts a Hudson Valley location as New York City |
| Unsupported bridge in the delivered reasoning | 5 | dev_32, dev_42, dev_55, dev_0, dev_11; these overlap with question/reference defects |
| SQL execution/syntax errors | 0/56 | Semantic validity must still be judged separately |
| Generation/delivery failure | 1 | dev_35: empty final answer after the call's reasoning budget is exhausted |

The five unsupported-bridge flags mean that a required inference was not
established or qualified adequately. They are not five additional demonstrated
false numerical answers, nor five failures attributable solely to the agent.
In particular, dev_0's final answer describes the database event accurately but
never establishes the original question's university-specific organization.

The explicit schema mistakes come from original BIRD descriptions copied into
ContextHub, not generated descriptions invented by this run. Missing country
normalization and missing product attributes should not be relabeled as
demonstrably false metadata. Correctly described columns can still be used at
the wrong grain: dev_46's A2/A3 definitions are clear, but its reference uses A3
despite asking for a district.

## Why the five original mismatches are not five SQL failures

**dev_62 — highest points.** The question does not specify time scope or
aggregation. Summing all stored constructor race-result points gives Ferrari
7,081.5. The maximum standings snapshot gives Mercedes 765. The second value is
itself cumulative within a season, not a single-race score: Mercedes' 2016 first
two race results are 43 and 40, while standings are 43 and 83; the final standing
is 765, equal to the season's race-point sum. The agent computed both measures,
explained its race-total measure, and retrieved Ferrari name-origin evidence.
That is a defensible alternative. `constructorStandings.points` misleadingly
says `how many points acquired in each race`.

**dev_13 — average points scored.** The nickname and year are correct. The
agent averages race-result points: 4/16 = 0.25. The reference averages cumulative
standings snapshots: 34/15 = 2.2666667. The original question does not require
the latter, and the ordinary per-race interpretation is reasonable. The two
missing initial standing records and one absent race-result record explain why
the two row counts also differ. `driverStandings.points` has the same misleading
per-race description. This is not a failed AVG calculation or wrong driver.

**dev_6 — India users with Teacher badges.** Exact `Location = 'India'` gives
18; country strings including Bangalore, India and Chennai, India produce 45
distinct qualifying users. Counting users rather than badge rows does not
explain the difference: both counts are 18 on the exact-string subset. The
agent removes the Indianapolis false match from the loose 46-row result.
Its broader country interpretation is sensible, but free-text city-only and
ambiguous locations prevent a certified complete country count. Separately,
it never retrieves the horse's country fact and transparently uses the name as
a clue. This is conditional support plus a real text-grounding gap, not a wrong
country discovered by the ranker or a proven wrong aggregation.

**dev_20 — New York City users.** The text correctly identifies New York, NY.
Exact equality gives the reference's 15 but excludes entries explicitly named
New York City. The agent's substring count 295 includes one explicitly noticed
Hudson Valley/state location. Thus its city count is demonstrably overbroad even
though 15 is not a validated geographic ground truth either. Subtracting one
gives 294 string matches, not an independently normalized city population.

**dev_46 — district with highest active loan.** All of these are real results
on the same active status set C/D:

| Interpretation | Result | Amount |
| --- | --- | ---: |
| District containing the largest single active loan (agent) | Brno-mesto | 590,820 |
| District with the largest total active loan amount (A2 grouping) | Hl.m. Praha / Prague | 9,291,852 |
| Region with the largest total active loan amount (reference A3 grouping) | south Moravia | 15,776,220 |

The original wording does not require a sum and specifically says district.
The agent's choice and retrieved Brno history are supported under the first
reading. The reference both aggregates and changes administrative level.
The agent's USD label follows the supplied BIRD amount description; this
review does not independently certify that source metadata's currency unit.

## Matching references can still have defective evidence chains

**dev_11 — national vice president versus club vice president.** Original:
“What is the name of the major that the person holding the second highest
political position in Afghanistan has joined?” The historical text returns
Vice President of Afghanistan. The schema explicitly defines `member.position`
as `The position the member holds in the club`; its Vice President is Phillip
Cullen, major Communication Studies. The source's political officeholders are
different named people. A shared English title cannot establish the same
person. The reference and agent both perform this substitution. The SQL is
valid for the club officer, but does not answer the literal political-person
question. This is a central question-composition and reasoning defect.

**dev_55 — two mentions of Amy.** The database has exactly one Art and Design
student, Amy Firth. The Peter Firth passage lists Amy among his children. No
retrieved source connects that student record to that child. The daughter
answer is plausible under an extra identity assumption and matches the
reference, but name equality is insufficient to certify it. This review does
not claim to have disproved the relationship.

**dev_0 — a type of organization versus this organization.** The article says
AUST has student clubs; it does not identify the club represented by this
database. Its university is in Lebanon, and the database schema contains no
institution link. The address predicate is an event venue column, whereas the
question can also attach it to the organization. The event query correctly
returns Laugh Out Loud for this database's vice president. The literal
AUST-specific relationship remains unestablished.

**dev_42 — district versus town.** The surname Sokolov is correctly found.
Eight female pre-1950 clients link to district Sokolov, and all eight have
accounts. But `district.A2` is labeled district_name, and the district row
contains 15 + 13 + 8 + 2 = 38 municipalities. `client.district_id` is described
as branch location, not verified residential town. Therefore eight is a valid
district-linked result; residence of all eight in the particular town is not
established. Both reference and agent use the district as a town proxy.

**dev_32 — junior versus prepaid.** This was previously marked partial. The
database proves the junior label, and the returned text explains prepaid
teen cards. The schema only distinguishes junior, standard/classic, and
high-level/gold classes. It never defines junior as prepaid. The reference
assumes that link; the agent notes the missing definition but still asserts a
young-user/entry-tier purpose. The product-specific functional question is not
settled by these sources. A missing reference feature list is not enough to
declare a fully answerable question incorrectly solved.

## Delivery and retrieval conclusions

**dev_35 remains a delivery failure.** Its question is ambiguous: 279 rows for
117 heroes share maximum attribute value 100, spanning 25 non-null race labels.
The reference's LIMIT 1 lacks a tie-break. The agent did not have to choose its
Cosmic Entity row uniquely, but it still needed to deliver a qualified answer
or explain the ambiguity. It delivered nothing. The last model call consumed
4,096 reasoning tokens and ended with `length`, not an episode timeout or SQL
exception. Its root-race searches are not proof that ContextHub missed a uniquely
required Cosmic Entity answer.

**Table discovery worked cumulatively in this fixture.** First searches found
33/42 reference table occurrences and all required tables in 13/20 cases.
After iterative queries these became 42/42 and 20/20. This measures schema
availability, not correct measure selection. It does not establish performance
on a substantially larger catalog.

**Text ranking is not the cause of every broken cross-source answer.** Ferrari
and Brno evidence supports the agent's alternative SQL branches. The junior-card
and identity examples return relevant passages but lack the central link; a
better ordering of the same passages would not prove that link. For dev_6,
ten searches fail to obtain direct horse evidence; scanning the frozen fixture
finds no occurrence of half-bred/half bred, consistent with a corpus-coverage
gap. This name scan is not a proof that no paraphrase could ever support it.
No isolated ranking-caused final failure is established by this review; this
is a limit of the evidence, not a zero-error claim about the ranker.

**dev_10 is a supported SQL-only answer.** The requested identity and weights
are already in one table, giving 441 - 90 = 351 kg. Skipping external search
does not make that answer wrong, although this episode does not demonstrate
the intended S2A text-retrieval capability. Similarly, dev_5 already names
Silverstone and dev_72 already names Brightest Day, reducing the difficulty of
their supposed cross-source dependency.

## Reproducibility and evidence

- Replayed all 56 actual agent SQL calls: identical columns, rows, truncation.
- Replayed all 20 reference queries and 15 additional diagnostic queries.
- Checked all 246 returned search-result occurrences (131 unique documents):
  actual L1 content is byte-identical to the frozen index content.
- All five queried SQLite file hashes still match the recorded episode hashes
  before and after this review; all 40 original trace hashes are unchanged.
- No model calls, new retrieval calls, corpus edits, or answer rewrites.

The 20 cases were nonrandom, and the 2,060-article fixture contains case-informed
additions. This review does not generalize to the full benchmark or remove those
limitations. The original reference-agreement evaluation remains preserved.

- [Structured judgments, rubric, and counts](artifacts/20261104-semantic-review/review.json)
- [Actual returned schemas and passages, grouped by case](artifacts/20261104-semantic-review/evidence-by-case.json)
- [All actual and reference SQL replays](artifacts/20261104-semantic-review/sql-replay.json)
- [Additional diagnostic SQL and results](artifacts/20261104-semantic-review/additional-data.json)
- [Integrity and replay checks](artifacts/20261104-semantic-review/checks.json)
- [Read-only replay script](artifacts/20261104-semantic-review/replay.py)
- [Previous reference-based report, preserved](2026-11-04-MIXED-QWEN.md)

To repeat mechanical evidence verification from this checkout, with the project
Python environment on PATH (no model or ContextHub service required):

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python docs/hydrabench/artifacts/20261104-semantic-review/replay.py /tmp/hydra-semantic-review-replay
```

The script verifies facts and file integrity; it does not independently reproduce
human-language judgments. Those judgments are explicitly recorded below and in
review.json for inspection.

## Every original question and adjudication

### dev_43 (A2S) — supported

**Original question:** What was the significance of the Joint Declaration on the Doctrine of Justification signed on the same date as the last round of the race in 1999?

**Review:** The final 1999 race is uniquely round 16, 1999-10-31. The retrieved declaration article supports the requested reconciliation significance. The question itself supplies the same-date premise; the agent explicitly says the retrieved passage does not independently verify the signing day.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_62 (A2S) — defensible alternative

**Original question:** Which constructor has the highest points, and what is the origin of the name of that constructor as it relates to the automotive brand?

**Review:** Ferrari leads the sum of recorded constructor race points with 7081.5. Mercedes leads the maximum season-standing snapshot with 765. Both SQL queries were executed correctly. The latter is season-to-date cumulative points, not points earned in one race. The question specifies neither aggregation nor time scope; Ferrari name-origin evidence supports the selected branch.

Previous reference assessment: `reference_mismatch`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_5 (S2A) — supported

**Original question:** What are the location coordinates of the racing circuit, Silverstone, which was built on the site of a World War II Royal Air Force bomber station and features runways in a classic WWII triangle format within its track outline?

**Review:** The passage identifies Silverstone and the airfield/runway premise; the unique database row gives latitude 52.0786 and longitude -1.01694. The question already names Silverstone, so the text step is verification, not indispensable discovery.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_13 (S2A) — defensible alternative

**Original question:** What was the average points scored in the year 2000 by the person nicknamed "Irv the Swerve" and "Fast Eddie"?

**Review:** The nickname passage identifies Eddie Irvine. In 2000 he has 4 points in 16 recorded race-result rows, giving 0.25 per race. The reference averages 15 cumulative standings snapshots, giving 34/15 = 2.2666667. Average points scored naturally permits the agent measure; the hidden reference does not establish that cumulative snapshots are the only intended measure.

Previous reference assessment: `reference_mismatch`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_35 (A2S) — no final answer

**Original question:** What is the role in Theosophical teachings of the race of the superhero with the maximum attribute value?

**Review:** The maximum attribute value 100 occurs in 279 rows for 117 heroes and 25 non-null race labels. The reference selects one tied row without a tie-break. The agent explored several interpretations, including root races in Theosophy, but delivered no answer: the last call used all 4096 output tokens for reasoning and ended with length. A specific mandatory Cosmic Entity branch cannot be inferred from this tied question.

Previous reference assessment: `no_answer`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_72 (A2S) — supported

**Original question:** What is the significance of the "Brightest Day" storyline in the publisher of Blue Beetle II?

**Review:** Database publisher is DC Comics. Retrieved Brightest Day text directly supports its aftermath-of-Blackest-Night significance and the additional character context. Reference-specific incidental details are not required to answer the open significance question.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_10 (S2A) — supported

**Original question:** What is the difference in weight between the alter ego of the comic supervillain Abomination and Charles Chandler?

**Review:** Both character identity and weights are present in the same database: Abomination/Emil Blonsky 441 kg and Charles Chandler 90 kg, difference 351 kg. SQL alone suffices for this question; absence of a text call is a hybrid-workflow observation, not an answer error.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_68 (S2A) — supported

**Original question:** What are the superpowers of the fictional character in "The Incredibles" who is married to Bob Parr and also known as Elastigirl?

**Review:** Retrieved cast text identifies Helen Parr/Elastigirl as Bob Parr's wife. The database identity and all seven power associations match the final answer.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_26 (A2S) — supported

**Original question:** What is the main advantage of the Holm–Bonferroni method compared to the simple Bonferroni correction, as discussed in the most-viewed post by Harvey Motulsky?

**Review:** The highest-view post is ID 109 with 3980 views. Retrieved method passages support greater power than simple Bonferroni while controlling family-wise error. The agent correctly notes that the actual post frames its direct comparison against Tukey/Dunnett; it does not silently attribute the specific Bonferroni comparison to that post.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_31 (A2S) — supported

**Original question:** What is the form of government in the country where the owner of the post "Eliciting priors from experts" is located?

**Review:** The unique post owner has location Newcastle, United Kingdom. Retrieved historical corpus passages support parliamentary democracy under constitutional monarchy. Current officeholders are outside this frozen-corpus review.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_6 (S2A) — conditional

**Original question:** How many users from the country where the Indian Half-bred horse type is primarily found have the teacher badges?

**Review:** 45 is reproducible for distinct Teacher badge holders with explicit India-country location variants. The reference exact string India yields 18 and excludes Bangalore, India and other explicit country variants. The broader SQL is defensible, but country-wide completeness is unproved for free-text locations. No direct Indian Half-bred country evidence was retrieved; the agent discloses inferring India from the name.

Previous reference assessment: `reference_mismatch`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_20 (S2A) — demonstrable error

**Original question:** How many users were from the city where Leonard Rosenfeld had the majority of his art showings?

**Review:** 295 is not a valid New York City count: the agent knowingly includes Hudson Valley, New York, USA. The reference exact string New York count 15 also excludes explicit New York City variants. Removing that one row gives 294 substring matches, not a validated city count. This is an actual overly broad SQL filter and a reasoning error despite the flawed reference scope.

Previous reference assessment: `reference_mismatch`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_32 (A2S) — unresolved premise

**Original question:** What type of credit card does the client with ID 13539 own, and how does this type of credit card promote financial management for its users, and what features distinguish it from other types of credit cards?

**Review:** The database proves card type junior for client 13539. Its schema only defines junior/classic/gold class labels. The retrieved paragraph is about prepaid cards and teenagers; neither it nor the table proves this junior class is prepaid or aimed at young users. The reference assumes that mapping; the agent acknowledges missing product-specific support but still infers a young/entry-tier purpose. Exact management features cannot be settled from these sources.

Previous reference assessment: `partial`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_46 (A2S) — defensible alternative

**Original question:** What is the historical significance of the district with the highest active loan in Czechoslovakia?

**Review:** Largest individual active loan: 590820 in Brno-mesto. Highest active-loan total by actual district A2: Prague, 9291852. Reference groups region A3: south Moravia, 15776220. The question says district and highest active loan, without a total; the agent single-loan reading is defensible and Brno history is supported. The reference also changes geographic level despite correct A2/A3 descriptions.

Previous reference assessment: `reference_mismatch`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_42 (S2A) — conditional

**Original question:** Among the accounts opened, how many female customers who were born before 1950 stayed in the town associated with the Soviet football player Nikolai Yevgrafovich's last name?

**Review:** The footballer text supplies Sokolov, and all eight qualifying clients linked to district Sokolov have actual accounts. But district.A2 is a district name, not a town address; this district contains 38 municipalities in the table. client.district_id is described as branch location. Eight is supported as a district-linked database count, not proven residence in the town requested by the question.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_60 (S2A) — supported

**Original question:** For the first client who opened his/her account in the capital of Czechia, what is his/her account ID?

**Review:** The retrieved text establishes Prague as the capital in the frozen corpus. Account district 1 is Hl.m. Praha/Prague. Earliest account 1539 on 1993-01-03 is unique; the next opening is 1993-01-08.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_55 (A2S) — unresolved premise

**Original question:** What is the relationship between the student from the Art and Design Department in the Student_Club and Peter Firth?

**Review:** The database identifies one Art and Design student, Amy Firth. The Peter Firth passage lists a child Amy. No retrieved record establishes that this student is that child. This is a same-name identity assumption in both reference and agent, not proof that the relationship is false; the unconditional daughter claim cannot be certified from the available evidence.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_66 (A2S) — supported

**Original question:** What is the most populous city in the hometown state of Sacha Harrison?

**Review:** The member hometown ZIP maps to New Jersey; an actually retrieved alternative passage identifies Newark as the state's largest city. Different cited population years do not change the requested city answer. Missing the frozen anchor does not imply retrieval failure.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_0 (S2A) — unresolved premise

**Original question:** What is the name of the social event attended by the vice president of an organization established at the American University of Science and Technology to promote student interests, located at 900 E. Washington St.?

**Review:** The SQL correctly identifies Laugh Out Loud as the social event at the address attended by this database's club vice president. The retrieved AUST passage says that the university has student clubs, but does not identify this database's club as one of them. The address column describes the event venue, not the organization. The benchmark substitutes a general organization type for an identified organization; a conditional database answer is available, but the literal AUST-specific premise is unproved.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

### dev_11 (S2A) — unresolved premise

**Original question:** What is the name of the major that the person holding the second highest political position in Afghanistan has joined?

**Review:** The historical text describes the Afghan political office Vice President and names its officeholders. The schema explicitly defines member.position as the position in the club; its Vice President is Phillip Cullen, major Communication Studies. Shared title does not establish shared person. Reference and agent both replace a national officeholder with a student club officer. The SQL is correct for the latter, not evidence of the former's major.

Previous reference assessment: `match`. All reference tables were retrieved. Actual schemas, passages, and final answer: [case evidence](artifacts/20261104-semantic-review/evidence-by-case.json).

