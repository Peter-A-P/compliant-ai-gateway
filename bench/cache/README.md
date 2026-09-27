# Semantic cache data

`paraphrases.jsonl`: two paraphrases of each of project 03's 100 gold questions, written once
on 2026-09-27 by Claude Haiku 4.5 through this repository's gateway (US$0.024, run id
`cache-paraphrases`), with the prompt in `boundary.semcache_eval.PARAPHRASE_PROMPT`. Stored so
that `boundary cache eval` re-reads them for nothing. Read by hand before use: docs/cache.md
lists the ones that changed the meaning.

The threshold was chosen on the odd-numbered questions (`boundary cache eval --splits odd`:
chat 0.82, retrieval none) and committed before the even half was run.
