# Vocabulary for the Cooking Issues transcripts

Whisper hears "polydextrose" as "Polly Dexter" and "Soxhlet" as "sockslet". This directory is how
we fight that. **`vocabulary.txt` is the control knob: review it and edit it freely.**

| File | What it is | Edited by |
| --- | --- | --- |
| `vocabulary.txt` | ~125 curated terms, one per line, **most valuable first**. Used twice: as Whisper's `initial_prompt` (cut at a term boundary to 600 characters, i.e. roughly the first 47 lines) and by the post-ASR correction pass (the whole file). | you (hand-curated) |
| `aliases.txt` | `heard => correct` pairs for known recurring mis-hearings. The highest-confidence correction rule. | you |
| `seeds.txt` | Terms from the design doc and the show's regulars; `build` marks them as seeds so they always appear in the candidates. | you |
| `candidates.csv` | Ranked machine-mined candidates (top 2,500 of ~13,000). Columns: `term, variants, count, doc_freq, sources, score, zipf, flags`. | `ciw vocab build` |

## Provenance

`vocabulary.txt` was curated by reading `candidates.csv`, which is mined (no LLM, no network) from:

- all 694 episode descriptions in `data/episodes/*/meta.json` (241 Acast, 453 Heritage Radio Network);
- as a second, corroborating source, the 224 blog posts in `cooking_issues.db` (opened read-only):
  title, categories and tags give clean terms, and the body text is the best source for equipment
  and ingredient names;
- the design doc's seed terms (`seeds.txt`).

Heuristics (see `src/ciw/vocab.py`): capitalised words in mid-sentence, runs of capitalised words
("Dave Arnold"), CamelCase, hyphenated words, tokens with digits, acronyms, words that are rare in
everyday English, and lower-case 2-3 word phrases that recur in at least 3 documents. "Common
English" means `wordfreq`'s Zipf frequency >= 3.0 (or >= 2.0 and present in
`/usr/share/dict/american-english`). Paragraphs repeated verbatim in 4+ show notes (sign-off and
donation boilerplate) are dropped, as are URLs, phone numbers, dates, and a stop list (Patreon,
subscribe, sponsors, ...). The score rewards document frequency, appearing in several sources, rarity
and distinctive shape. Pruning was by hand: common words, boilerplate, advertiser names and one-off
guest names that Whisper already gets right were dropped.

## Regenerate

```
ciw vocab build            # rewrites candidates.csv only; never touches vocabulary.txt
ciw vocab build --no-blog  # show notes only
```

New show notes arrive with every `ciw poll`; re-run `build` now and then, skim the top of
`candidates.csv` for new names or equipment, and add what you want to `vocabulary.txt`.

## Editing rules of thumb

- Put terms Whisper gets wrong, and terms the scorer keys on, near the top: only the first ~47 lines
  reach the prompt. Everything in the file helps the correction pass.
- Changing the prompt part (the first ~600 characters) makes existing transcripts "vocab-stale":
  `ciw transcribe --stale` redoes them (about 2 minutes each on the 3080), and `ciw episodes list`
  flags them. Changing anything else only needs `ciw vocab correct EPISODE` (no ASR).
- Spaces and hyphens are interchangeable when matching (`sous vide` = `sous-vide`).
- A term spelled with capitals ("Searzall", "iSi") is written that way by the correction pass;
  all-lower-case terms ("polydextrose") only get a capital at the start of a sentence.

## Quality check

```
ciw vocab quality EPISODE [EPISODE ...] [--aggregate] [--json]
```

reports, for each episode, how many of the vocabulary terms named in its show notes appear in the
transcript (before and after correction), which were missed and what they were heard as, plus the ASR
runtime and real-time factor. `ciw vocab corrections EPISODE` lists every correction that was made.
