# Injection screen data

`deepset-train.jsonl` and `deepset-test.jsonl` are the two splits of
[deepset/prompt-injections](https://huggingface.co/datasets/deepset/prompt-injections)
(Apache License 2.0), fetched 2026-09-27 through the Hugging Face datasets server, row order
kept, with an id added. 546 and 116 rows, 203 and 60 of them labelled injection.

The screen's rules (boundary/screen.py) were developed on the train split only. The test
split and project 03's red-team suites were first run after the rules were frozen in the
commit that added this file.
