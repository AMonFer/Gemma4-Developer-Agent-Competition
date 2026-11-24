# Gemma 4 Developer Agent

An autonomous agent for automated bug fixing in real Python repositories, built for the Kaggle Gemma 4 Developer Agent Competition. Given an issue description, the agent locates the faulty code, edits it, verifies the change, and submits a patch. Patches are graded in a clean container against hidden tests, in the style of SWE-bench.

The repository documents the final state of the work and the full experiment history. Its main finding is quantitative: how much resolution accuracy is lost when the same agent design is moved from a 31B model to a 4B model under limited compute.

## Summary of results

Evaluation uses roughly 120 held-out tasks drawn from Python libraries such as FastAPI, Rich, Requests and HTTPX. A task counts as resolved only if the submitted patch passes the hidden tests. The score is the fraction of tasks resolved.

| Track | Base model | Method | Best resolution rate |
| :--- | :--- | :--- | :---: |
| General | `gemma-4-31b-it-qat-w4a16-ct` | Prompting and tool/budget design, no adapters | 0.15 |
| Small model | `gemma-4-e4b-it-qat-w4a16-ct` | Prompting and tool design, no adapters | 0.05 |

Both models run 4-bit quantized (W4A16). The E4B model reaches one third of the 31B score. Scores are Kaggle public leaderboard results and are provisional, because the competition evaluates on a subset of tasks until it closes.

## Key findings

1. **Orchestration complexity hurt performance.** Multi-agent designs (semantic scout, graph inspector, separate code editor) scored 0.05 to 0.08, against 0.12 for a single orchestrator that keeps its own `edit_file` tool. Sequential sub-agent chains consumed the 6-minute and 50-tool-call budgets (Experiments 03 to 06).
2. **Small, tightly specified prompts won.** The best 31B result (0.15) came from reducing turns per task to 7-12, scoping searches, and requiring regression checks on existing tests after each edit (Experiment 14). Adding more rules in the following iteration lowered the score to 0.13 and was reverted (Experiment 15).
3. **Fine-tuning the 31B model was not viable on the available hardware.** A QLoRA adapter (rank 8, trained on 2x T4 GPUs) scored 0.01 once packaged for the evaluation harness, and enabling LoRA reduced the usable KV cache from about 45.5k to 27.7k tokens. The adapter was removed and the project returned to prompt-level optimization, which yielded 0.13 immediately and 0.15 shortly after (Experiments 11 to 14).
4. **Small models need a different prompt, not a shorter one.** The first E4B attempt scored 0.0. A failure analysis found six model-specific failure modes, including markdown emission instead of tool calls, truncated reasoning from a small thinking budget, and corrupted edits at higher temperature. Redesigning the prompt around these modes (a linear 4-step workflow, under 3.4 KB, with concrete tool-call examples) reached 0.05 (Experiment 18).
5. **The E4B score plateaued.** Four different configurations produced the same 0.05 (about 6 of 120 tasks): changing the thinking budget, enabling thought retention, and tightening the edit-step instructions all had no effect. Removing three tools lowered the score to 0.03, and two prompt rewrites lowered it to 0.01. Taken together, this indicates the remaining failures come from model capacity rather than prompt wording (Experiments 18 to 24).

## Compute constraints

Training was limited to two NVIDIA T4 GPUs (30 GB total VRAM) on free-tier notebook quotas, with a maximum sequence length of 8,192 tokens. This ruled out full fine-tuning of the 31B model and shaped the work in three ways: the 31B agent was optimized at the prompt and workflow level only; the small-model track was approached with a 4-bit quantized E4B base; and a data pipeline for QLoRA on E4B was prepared (see below).

Inference during evaluation also has fixed budgets, set in `src/eval_config.yaml`: 6 minutes per task, 75 seconds per command, 50 tool calls and 60 turns. These were chosen so that all tasks complete within the platform's 12-hour GPU window. An early configuration with 10 minutes per task exceeded that window and produced a timeout (Experiment 09).

## Repository layout

```text
src/                         Declarative agent submitted to the competition
  agent.yaml                 Model, tools and instruction reference
  eval_config.yaml           Per-task time, turn and tool-call budgets
  configs/sampling.yaml      Sampling and thinking configuration
  prompts/system.md          System prompt
scripts/
  validate.py                Checks the submission against the platform rules
  package.py                 Builds dist/submission.zip with a SHA256 check
  submit.py                  Submits to Kaggle through the CLI
  forensic_inspect.py        Failure analysis of agent runs
  sft/                       Data pipeline for QLoRA fine-tuning (E4B)
notebooks/                   QLoRA training notebooks (Unsloth, 2x T4)
.github/workflows/           CI: validates and packages on every push and pull request
EXPERIMENTS.md               Log of the evaluated submissions: hypothesis, change, score, conclusion
```

## Branches

| Branch | Contents |
| :--- | :--- |
| `main` | 31B agent at its best configuration (Experiment 14, score 0.15) |
| `track/small-model-e4b` | E4B agent (Experiments 17 to 24, best 0.05) and the SFT pipeline |
| `version/v5.6` | 31B prompt variant (v5.6) not recorded in the experiment log, kept for reference |

The tag `exp18-champion` marks the E4B configuration that established the small-model baseline.

## Experiment methodology

Each of the 23 logged submissions (numbered 01 to 24; number 16 was not recorded) changes one variable against a known baseline, so that score differences can be attributed to a single cause. Every entry in `EXPERIMENTS.md` records the hypothesis, the exact change, the local validation result, the Kaggle score and the conclusion, including regressions and the reasoning for reverting them. When a change reduced the score, the previous best configuration was restored before the next experiment. The log is the primary record of the project.

## Fine-tuning pipeline (prepared, not completed)

The `scripts/sft/` directory contains a pipeline for fine-tuning the E4B model on public agent trajectories:

- `curate_trajectories.py` selects resolved, pure-Python trajectories from SWE-smith, Nebius SWE-agent and SWE-Gym/OpenHands, oversamples the target repositories, bounds turn count and token length, remaps tools to the competition tool schema, and enforces patch hygiene.
- `build_gemma4_sft.py` converts them into multi-turn samples in the native Gemma 4 chat format, with loss computed on assistant turns only and prefix-stability assertions.
- `sanitize_adapter.py` converts trained adapters to a plain PEFT format that the evaluation harness can load.

A dataset of 2,000 samples (1,800 train, 200 validation) was built, balanced across four repositories and four step types (localization, reproduction, surgical edit, submission hygiene). Adapter training for E4B was not completed because of compute limits, so no fine-tuned adapter was evaluated, and the reported E4B score is prompt-only.

## Usage

```bash
pip install -r requirements.txt
python scripts/validate.py          # check the submission against the platform rules
python scripts/package.py           # build dist/submission.zip
python scripts/submit.py -m "message"   # submit through the Kaggle CLI (requires ~/.kaggle/kaggle.json)
```

## Platform constraints encoded in the project

- The system prompt must not contain curly braces, because the agent framework interpolates them as session variables.
- Prompt size limits used here: under 8,192 bytes for 31B and under 4,096 bytes for E4B.
- Agent scratch files must live in `/tmp`, so they do not appear in the submitted patch.
- Test files and project configuration files are reset before grading and must not be modified.
- The unpacked submission must stay under 3 GiB.

## Limitations

- Scores are provisional and based on a subset of tasks, and the task-level variance is high: re-running an identical configuration moved the score between 0.10 and 0.12 (Experiment 08). Differences of one or two tasks should not be read as real effects.
- The comparison between 31B and E4B changes the model, the prompt and the tool set together, so it measures the accuracy cost of the compact setup as a whole and does not isolate model size.
- No trained adapter was evaluated on E4B.

## Author

Adrian. Developed as an individual project.
