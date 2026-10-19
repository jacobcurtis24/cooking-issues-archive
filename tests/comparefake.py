"""A ready-to-run comparison setup in a tmp root: prompt file, episodes with transcripts."""

from __future__ import annotations

from pathlib import Path

import yaml
from conftest import write_config
from llmfake import TRANSCRIPT_TEXT
from webfake import add_episode, make_transcript, make_words

from ciw.config import Config, load_config

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "tags", "key_quotes"],
    "properties": {
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "key_quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["timestamp", "quote"],
                "properties": {"timestamp": {"type": "string"}, "quote": {"type": "string"}},
            },
        },
    },
}

MATRIX = [
    {"id": "haiku-medium", "model": "claude-haiku-5-5", "effort": "medium"},
    {"id": "sonnet-high", "model": "claude-sonnet-5-5", "effort": "high"},
    {"id": "opus-medium", "model": "claude-opus-5-5", "effort": "medium"},
]
EPISODES = ["ep1", "ep2", "ep3"]


def prompt_dict(**over) -> dict:
    base = {
        "system": "You summarise podcast transcripts.",
        "user": "Title: {{title}}\nDate: {{pub_date}} ({{duration}})\nNotes: {{show_notes}}\n\n"
        "{{transcript}}",
        "schema": SCHEMA,
        "checks": {
            "quote_fields": ["quote"],
            "timestamp_fields": ["timestamp"],
            "list_fields": ["tags"],
        },
        "max_tokens": 4096,
        "matrix": MATRIX,
        "reference": "opus-medium",
        "sample": EPISODES,
    }
    base.update(over)
    return base


def write_prompt(path: Path, **over) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(prompt_dict(**over), sort_keys=False), encoding="utf-8")
    return path


def setup(tmp_path: Path, *, extra_cfg: str = "", **prompt_over) -> Config:
    """Config rooted in ``tmp_path`` with ``prompts/compare.yaml`` and three transcribed
    episodes. (Call after the ``tmp_cfg`` fixture, which sets CIW_ROOT.)"""
    write_config(tmp_path, f"llm:\n  concurrency: 2\n{extra_cfg}")
    write_prompt(tmp_path / "prompts" / "compare.yaml", **prompt_over)
    cfg = load_config(tmp_path, use_dotenv=False)
    for i, eid in enumerate(EPISODES, 1):
        add_episode(
            cfg,
            eid,
            f"Episode {i}",
            notes=f"Show notes {i}",
            transcript=make_transcript(make_words(TRANSCRIPT_TEXT, step=0.5, speaker="SPEAKER_00")),
        )
    return cfg
